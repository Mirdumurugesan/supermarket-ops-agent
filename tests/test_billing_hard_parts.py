"""The hard parts: oversell guard, idempotency, atomicity, guardrails.

These are the tests that prove the business rules live in the data layer and
not in the prompt — every one of them would pass with the LLM removed.
"""

from __future__ import annotations

import pytest

from kirana.db.database import get_conn
from kirana.services import billing_service as billing
from kirana.services import inventory_service as inv
from kirana.services.errors import GuardrailError, NotFoundError, OversellError

CHAT = 12345


def _bill_with(db, product_id: int, qty: float, mode: str = "cash"):
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], product_id, qty)
    billing.set_payment(b["id"], mode)
    return b["id"]


# --------------------------------------------------------------- oversell

def test_cannot_add_more_than_stock(db):
    """Maggi has 6 in stock; billing 10 is refused at the tool layer."""
    b = billing.create_draft(CHAT)
    with pytest.raises(OversellError) as e:
        billing.add_item(b["id"], 1, 10)
    assert "6" in str(e.value)


def test_cannot_finalize_past_stock_even_if_draft_slipped_through(db):
    """Stock drops after the line was added — finalize must still refuse."""
    bill_id = _bill_with(db, 1, 6)          # exactly all 6 Maggi
    inv.adjust_stock(1, 2, reason="breakage")  # someone else removed stock
    with pytest.raises(OversellError):
        billing.finalize_bill(bill_id, "key-1")
    assert inv.get_product(1)["qty"] == 2   # nothing was decremented
    assert billing.get_bill(bill_id)["status"] == "draft"


def test_stock_never_goes_negative_even_bypassing_the_services(db):
    """The floor is a CHECK constraint in the schema, not application code."""
    import sqlite3
    conn = get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE products SET qty = -1 WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE products SET qty = qty - 100 WHERE id = 1")


def test_partial_failure_rolls_back_the_whole_bill(db):
    """Line 1 is fine, line 2 oversells → NOTHING is decremented."""
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 2, 5)     # sugar, plenty
    billing.add_item(b["id"], 5, 4)     # surf, exactly all 4
    billing.set_payment(b["id"], "upi")
    inv.adjust_stock(5, 1)              # surf drops to 1
    with pytest.raises(OversellError):
        billing.finalize_bill(b["id"], "key-rollback")
    assert inv.get_product(2)["qty"] == 100   # sugar untouched
    assert inv.get_product(5)["qty"] == 1


# ------------------------------------------------------------ idempotency

def test_retried_finalize_does_not_double_bill(db):
    bill_id = _bill_with(db, 1, 3)
    first = billing.finalize_bill(bill_id, "tg-update-999")
    second = billing.finalize_bill(bill_id, "tg-update-999")   # Telegram redelivery
    assert first["invoice_no"] == second["invoice_no"]
    assert first["grand_total"] == second["grand_total"]
    assert inv.get_product(1)["qty"] == 3         # decremented exactly once


def test_retry_with_different_key_still_does_not_double_decrement(db):
    """Even a *different* key can't re-finalize an already finalized bill."""
    bill_id = _bill_with(db, 1, 2)
    billing.finalize_bill(bill_id, "key-a")
    billing.finalize_bill(bill_id, "key-b")
    assert inv.get_product(1)["qty"] == 4
    moves = get_conn().execute(
        "SELECT COUNT(*) c FROM stock_moves WHERE ref_bill_id=?", (bill_id,)
    ).fetchone()["c"]
    assert moves == 1


def test_finalized_bill_cannot_be_edited_or_cancelled(db):
    bill_id = _bill_with(db, 1, 1)
    billing.finalize_bill(bill_id, "k")
    with pytest.raises(GuardrailError):
        billing.add_item(bill_id, 2, 1)
    with pytest.raises(GuardrailError):
        billing.cancel_bill(bill_id)


# ------------------------------------------------------- multi-turn edits

def test_bill_builds_and_edits_across_turns(db):
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 2, 2)          # 2kg sugar
    billing.add_item(b["id"], 3, 1)          # 1 atta
    billing.add_item(b["id"], 1, 4)          # 4 maggi
    assert len(billing.get_bill(b["id"])["items"]) == 3

    billing.set_item_qty(b["id"], 3, 0)      # "drop the atta"
    billing.set_item_qty(b["id"], 1, 6)      # "make it 6 maggi"
    bill = billing.get_bill(b["id"])
    assert len(bill["items"]) == 2
    assert next(i for i in bill["items"] if i["product_id"] == 1)["qty"] == 6
    assert inv.get_product(1)["qty"] == 6    # draft edits never touch stock


def test_drafting_does_not_move_stock_until_finalize(db):
    b = billing.create_draft(CHAT)
    billing.add_item(b["id"], 2, 10)
    assert inv.get_product(2)["qty"] == 100
    billing.set_payment(b["id"], "cash")
    billing.finalize_bill(b["id"], "k2")
    assert inv.get_product(2)["qty"] == 90


def test_packaged_items_reject_fractional_quantity(db):
    b = billing.create_draft(CHAT)
    with pytest.raises(GuardrailError):
        billing.add_item(b["id"], 1, 2.5)      # 2.5 packets of Maggi
    billing.add_item(b["id"], 2, 2.5)          # loose sugar is fine
    assert billing.get_bill(b["id"])["items"][0]["qty"] == 2.5


def test_empty_or_unpaid_bill_cannot_finalize(db):
    b = billing.create_draft(CHAT)
    with pytest.raises(GuardrailError):
        billing.finalize_bill(b["id"], "k3")
    billing.add_item(b["id"], 1, 1)
    with pytest.raises(GuardrailError):        # no payment mode set
        billing.finalize_bill(b["id"], "k4")


# --------------------------------------------------------------- totals

def test_finalized_totals_match_the_gst_engine(db):
    bill_id = _bill_with(db, 1, 4)             # 4 Maggi @ ₹14 incl 12%
    bill = billing.finalize_bill(bill_id, "k5")
    assert bill["subtotal"] == 50.00
    assert bill["tax_total"] == 6.00
    assert bill["grand_total"] == 56.00
    assert bill["invoice_no"].startswith("INV-")


def test_invoice_numbers_are_unique_and_sequential(db):
    nums = []
    for i in range(3):
        bid = _bill_with(db, 2, 1)
        nums.append(billing.finalize_bill(bid, f"seq-{i}")["invoice_no"])
    assert len(set(nums)) == 3
    assert [n.split("-")[-1] for n in nums] == ["001", "002", "003"]
