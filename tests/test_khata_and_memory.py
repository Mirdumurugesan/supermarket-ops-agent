"""Khata guardrails, durable memory, and analytics."""

from __future__ import annotations

from datetime import datetime

import pytest

from kirana.services import analytics_service as analytics
from kirana.services import billing_service as billing
from kirana.services import inventory_service as inv
from kirana.services import khata_service as khata
from kirana.services import memory_service as memory
from kirana.services.errors import GuardrailError, NotFoundError

CHAT = 777


# ----------------------------------------------------------------- khata

def test_credit_then_payment_cycle(db):
    khata.add_credit("Ramesh", 500)
    assert khata.balance("Ramesh")["balance"] == 500
    khata.record_payment("Ramesh", 300)
    assert khata.balance("Ramesh")["balance"] == 200


def test_cannot_settle_a_khata_that_does_not_exist(db):
    with pytest.raises(NotFoundError) as e:
        khata.record_payment("Ghost", 100)
    assert "doesn't exist" in str(e.value) or "No khata" in str(e.value)


def test_payment_cannot_overshoot_the_balance(db):
    khata.add_credit("Ramesh", 200)
    with pytest.raises(GuardrailError):
        khata.record_payment("Ramesh", 500)
    assert khata.balance("Ramesh")["balance"] == 200


def test_negative_or_zero_amounts_are_refused(db):
    with pytest.raises(GuardrailError):
        khata.add_credit("Ramesh", 0)
    with pytest.raises(GuardrailError):
        khata.record_payment("Ramesh", -50)


def test_khata_bill_posts_to_the_ledger_atomically(db):
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 1, 4)                       # ₹56
    billing.set_payment(b["id"], "khata", khata_customer="Ramesh")
    bill = billing.finalize_bill(b["id"], "khata-key")
    assert khata.balance("Ramesh")["balance"] == bill["grand_total"] == 56.0
    entries = khata.statement("Ramesh")["entries"]
    assert entries[0]["entry_type"] == "credit"
    assert bill["invoice_no"] in entries[0]["note"]


def test_khata_customer_is_created_on_first_credit(db):
    khata.add_credit("Brand New Customer", 120)
    assert khata.balance("Brand New Customer")["balance"] == 120


# ---------------------------------------------------------------- memory

def test_preferences_persist_and_update(db):
    memory.set_preference("default_payment_mode", "upi")
    memory.set_preference("default atta", "Aashirvaad 5kg")   # normalized key
    prefs = memory.get_preferences()
    assert prefs["default_payment_mode"] == "upi"
    assert prefs["default_atta"] == "Aashirvaad 5kg"

    memory.set_preference("default_payment_mode", "cash")
    assert memory.get_preferences()["default_payment_mode"] == "cash"


def test_preferences_survive_a_simulated_restart(db):
    """Memory lives in SQLite, not the context window — reconnecting proves it."""
    from kirana.db import database
    memory.set_preference("shop_name", "Sri Murugan Stores")

    database._local = type(database._local)()   # simulate process restart
    database.init_db(db)
    assert memory.get_preferences()["shop_name"] == "Sri Murugan Stores"


def test_preference_can_be_deleted(db):
    memory.set_preference("temp_key", "x")
    memory.delete_preference("temp_key")
    assert "temp_key" not in memory.get_preferences()


# ------------------------------------------------------------- guardrails

def test_cannot_price_below_cost_without_explicit_override(db):
    with pytest.raises(GuardrailError):
        inv.update_product(1, sell_price=5)          # cost is ₹12
    p = inv.update_product(1, sell_price=5, allow_below_cost=True)
    assert p["sell_price"] == 5


def test_cannot_price_above_mrp(db):
    with pytest.raises(GuardrailError):
        inv.update_product(3, sell_price=300)        # MRP ₹285


def test_products_are_deactivated_not_deleted(db):
    inv.update_product(1, active=False)
    assert inv.get_product(1)["active"] == 0         # row still there
    assert not any(p["id"] == 1 for p in inv.search_products("maggi"))


def test_stock_cannot_be_adjusted_negative(db):
    with pytest.raises(GuardrailError):
        inv.adjust_stock(1, -5)


# -------------------------------------------------------------- analytics

def test_ist_timezone_is_available_on_this_platform():
    """Regression guard: Windows ships no system tz database.

    The store closes its day in IST, so ZoneInfo("Asia/Kolkata") must resolve
    everywhere the bot runs. Without the `tzdata` package this raises
    ZoneInfoNotFoundError on Windows at import time — the whole bot fails to
    start, and it passes silently on Linux CI.
    """
    from zoneinfo import ZoneInfo
    assert ZoneInfo("Asia/Kolkata").utcoffset(datetime(2026, 9, 6)).total_seconds() == 19800



def test_daily_summary_reflects_finalized_sales_only(db):
    draft = billing.create_draft(CHAT)
    billing.add_item(draft["id"], 1, 2)              # left as draft

    b = billing.create_draft(CHAT + 1)
    billing.add_item(b["id"], 1, 4)
    billing.set_payment(b["id"], "upi")
    billing.finalize_bill(b["id"], "an-1")

    s = analytics.daily_summary()
    assert s["bills"] == 1
    assert s["revenue"] == 56.0
    assert s["tax_collected"] == 6.0
    assert s["by_payment_mode"][0]["payment_mode"] == "upi"
    assert s["top_items"][0]["name"].startswith("Maggi")


def test_low_stock_report_flags_items_at_reorder_level(db):
    names = [p["name"] for p in inv.low_stock()]
    assert any("Maggi" in n for n in names)          # 6 in stock, reorder 30
    assert any("Surf" in n for n in names)           # 4 in stock, reorder 5


def test_reorder_suggestions_use_sales_velocity(db):
    """Rice: 200kg on hand, 180kg sold this week → days-of-stock-left is tiny."""
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 4, 180)
    billing.set_payment(b["id"], "cash")
    billing.finalize_bill(b["id"], "vel-1")

    sugg = analytics.reorder_suggestions(days=7)
    rice = next((s for s in sugg if "Rice" in s["product"]), None)
    assert rice is not None
    assert rice["in_stock"] == 20
    assert rice["velocity_per_day"] == pytest.approx(180 / 7, rel=0.01)
    assert rice["days_of_stock_left"] < 2             # flagged as urgent
    assert rice["suggested_order_qty"] >= 50


def test_slow_movers_are_not_flagged_for_reorder(db):
    """Sugar: 100kg on hand, 2kg sold → plenty of runway, no suggestion."""
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 2, 2)
    billing.set_payment(b["id"], "cash")
    billing.finalize_bill(b["id"], "vel-2")

    assert not any("Sugar" in s["product"] for s in analytics.reorder_suggestions(days=7))
