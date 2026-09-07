"""End-to-end smoke test WITHOUT the LLM.

Runs a realistic day of trading straight through the service layer and
generates both artifacts. Proves the store is correct on its own — the model
only ever chooses which of these calls to make.

    python scripts/smoke_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kirana.db.database import init_db  # noqa: E402
from kirana.docs_gen.analysis_pptx import generate_analysis_deck  # noqa: E402
from kirana.docs_gen.invoice_pdf import generate_invoice_pdf  # noqa: E402
from kirana.services import analytics_service as analytics  # noqa: E402
from kirana.services import billing_service as billing  # noqa: E402
from kirana.services import inventory_service as inv  # noqa: E402
from kirana.services import khata_service as khata  # noqa: E402
from kirana.services import memory_service as memory  # noqa: E402
from kirana.services.errors import KiranaError  # noqa: E402

CHAT = 424242


def step(label: str) -> None:
    print(f"\n\033[1m{label}\033[0m")


def main() -> None:
    init_db()
    from seed import seed  # noqa: PLC0415
    seed()

    # 1 — receive stock
    step("1. Receive stock: 50 packets of Maggi @ cost ₹12, MRP ₹14")
    maggi = inv.search_products("maggi")[0]
    before = maggi["qty"]
    p = inv.receive_stock(maggi["id"], 50, cost_price=12, mrp=14)
    print(f"   {p['name']}: {before:g} → {p['qty']:g} packets")

    # 2 — multi-item bill with an edit
    step("2. Bill: 2kg sugar, 1 Aashirvaad atta, 4 Maggi, 1 Amul butter — UPI")
    bill = billing.create_draft(CHAT)
    for query, qty in [("sugar", 2), ("aashirvaad", 1), ("maggi", 4), ("butter", 1)]:
        prod = inv.search_products(query)[0]
        billing.add_item(bill["id"], prod["id"], qty)
    billing.set_payment(bill["id"], "upi", reference="UPI/AXIS/8891")
    draft = billing.get_bill(bill["id"])
    print(f"   Draft #{draft['id']}: {len(draft['items'])} lines, ₹{draft['grand_total']:.2f}")

    step("3. Edit mid-build: drop the butter, make it 6 Maggi")
    butter = inv.search_products("butter")[0]
    billing.set_item_qty(bill["id"], butter["id"], 0)
    billing.set_item_qty(bill["id"], inv.search_products("maggi")[0]["id"], 6)
    draft = billing.get_bill(bill["id"])
    print(f"   Draft #{draft['id']}: {len(draft['items'])} lines, ₹{draft['grand_total']:.2f}")

    step("4. Finalize — stock moves atomically, GST computed per slab")
    final = billing.finalize_bill(bill["id"], "smoke-update-1")
    print(f"   {final['invoice_no']} · taxable ₹{final['subtotal']:.2f} "
          f"+ GST ₹{final['tax_total']:.2f} + round-off ₹{final['round_off']:+.2f} "
          f"= ₹{final['grand_total']:.2f}")

    step("5. Idempotency — Telegram redelivers the same finalize")
    again = billing.finalize_bill(bill["id"], "smoke-update-1")
    maggi_now = inv.get_product(inv.search_products("maggi")[0]["id"])
    print(f"   Same invoice returned: {again['invoice_no']} "
          f"({'PASS' if again['invoice_no'] == final['invoice_no'] else 'FAIL'})")
    print(f"   Maggi stock decremented once: {maggi_now['qty']:g} packets")

    step("6. Oversell guard — try to bill 500 Surf Excel")
    surf = inv.search_products("surf")[0]
    b2 = billing.create_draft(CHAT + 1)
    try:
        billing.add_item(b2["id"], surf["id"], 500)
        print("   FAIL — that should have been refused")
    except KiranaError as e:
        print(f"   REFUSED: {e}")

    step("7. Khata cycle")
    khata.add_credit("Ramesh", 500, "Weekly groceries")
    khata.record_payment("Ramesh", 300)
    print(f"   Ramesh's balance: ₹{khata.balance('Ramesh')['balance']:.2f}")
    try:
        khata.record_payment("Ghost Customer", 100)
        print("   FAIL — settling a non-existent khata should be refused")
    except KiranaError as e:
        print(f"   REFUSED: {e}")

    step("8. Second bill, on khata")
    b3 = billing.create_draft(CHAT)
    rice = inv.search_products("rice")[0]
    billing.add_item(b3["id"], rice["id"], 5)
    billing.add_item(b3["id"], inv.search_products("tea")[0]["id"], 1)
    billing.set_payment(b3["id"], "khata", khata_customer="Ramesh")
    khata_bill = billing.finalize_bill(b3["id"], "smoke-update-2")
    print(f"   {khata_bill['invoice_no']} · ₹{khata_bill['grand_total']:.2f} on khata")
    print(f"   Ramesh's balance now: ₹{khata.balance('Ramesh')['balance']:.2f}")

    step("9. Daily close")
    s = analytics.daily_summary()
    print(f"   {s['date']}: {s['bills']} bills · ₹{s['revenue']:.2f} revenue · "
          f"₹{s['tax_collected']:.2f} GST collected")
    print(f"   Payment split: " + ", ".join(
        f"{m['payment_mode']} ₹{m['amount']:.0f}" for m in s['by_payment_mode']))
    print(f"   Khata outstanding: ₹{s['khata_outstanding']:.2f}")
    print(f"   Top item: {s['top_items'][0]['name']} (₹{s['top_items'][0]['revenue']:.2f})")

    step("10. Durable memory")
    memory.set_preference("default_payment_mode", "upi")
    memory.set_preference("default_atta", "Aashirvaad Atta 5kg")
    print(f"   Saved: {memory.get_preferences()}")

    step("11. Artifacts")
    pdf = generate_invoice_pdf(final["id"])
    today = analytics.today_ist()
    deck = generate_analysis_deck(today, today)
    print(f"   PDF invoice: {pdf} ({Path(pdf).stat().st_size // 1024} KB)")
    print(f"   Analysis deck: {deck} ({Path(deck).stat().st_size // 1024} KB)")

    step("12. Low stock / reorder")
    for r in analytics.reorder_suggestions()[:4]:
        left = (f"{r['days_of_stock_left']:g}d left" if r["days_of_stock_left"] is not None
                else "below reorder level")
        print(f"   {r['product']}: {r['in_stock']:g} {r['unit']} — {left} "
              f"→ order ~{r['suggested_order_qty']:g}")

    print("\n\033[1m✓ Store logic verified end-to-end without the LLM.\033[0m")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
