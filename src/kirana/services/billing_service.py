"""Billing: multi-turn draft bills, edits, and an atomic, idempotent finalize.

This module carries four of the task's "hard parts":

1. **Multi-turn bills** — a bill is a ``draft`` row that accumulates items
   across any number of chat messages. Stock is *not* touched while drafting.

2. **Oversell guard** — on finalize, each line decrements stock with
   ``UPDATE products SET qty = qty - :q WHERE id = :id AND qty >= :q``.
   A zero rowcount means insufficient stock → the whole transaction rolls
   back and the finalize is refused. Combined with the ``qty >= 0`` CHECK
   constraint, stock can never go negative — regardless of what the model asks.

3. **Idempotency** — Telegram redelivers updates (at-least-once). Finalize
   takes an ``idempotency_key`` (the bot passes the Telegram update id). If a
   bill is already finalized, or the key was already used, we return the
   existing finalized bill instead of double-billing / double-decrementing.
   The key is UNIQUE in the schema, so even a race between two retries
   resolves safely.

4. **Concurrency** — the entire finalize runs in one ``BEGIN IMMEDIATE``
   transaction (single writer in SQLite), so two bills in flight, or a sale
   racing a stock-in, serialize cleanly.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..db.database import get_conn, transaction
from . import khata_service
from .errors import GuardrailError, NotFoundError, OversellError
from .gst import compute_line, compute_totals

IST = ZoneInfo("Asia/Kolkata")

PAYMENT_MODES = {"cash", "upi", "card", "khata"}


# ---------------------------------------------------------------- draft bills

def get_open_draft(chat_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM bills WHERE chat_id=? AND status='draft' ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    return dict(row) if row else None


def create_draft(chat_id: int, customer_name: str | None = None) -> dict:
    existing = get_open_draft(chat_id)
    if existing:
        return existing  # one draft per chat keeps "the bill" unambiguous
    with transaction() as tx:
        cur = tx.execute(
            "INSERT INTO bills (chat_id, customer_name) VALUES (?,?)",
            (chat_id, customer_name),
        )
        bill_id = cur.lastrowid
    return get_bill(bill_id)


def _require_draft(tx, bill_id: int):
    row = tx.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not row:
        raise NotFoundError(f"No bill #{bill_id}.")
    if row["status"] != "draft":
        raise GuardrailError(f"Bill #{bill_id} is {row['status']} and can no longer be edited.")
    return row


def add_item(bill_id: int, product_id: int, qty: float) -> dict:
    """Add (or top up) a line. Checks *available* stock as an early warning —
    the hard guarantee still happens at finalize."""
    if qty <= 0:
        raise GuardrailError("Quantity must be positive.")
    with transaction() as tx:
        _require_draft(tx, bill_id)
        p = tx.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not p:
            raise NotFoundError(f"No active product with id {product_id}.")
        if not p["is_loose"] and abs(qty - round(qty)) > 1e-9:
            raise GuardrailError(f"{p['name']} is a packaged item — quantity must be a whole number.")
        existing = tx.execute(
            "SELECT qty FROM bill_items WHERE bill_id=? AND product_id=?",
            (bill_id, product_id),
        ).fetchone()
        new_qty = qty + (existing["qty"] if existing else 0)
        if new_qty > p["qty"]:
            raise OversellError(
                f"Only {p['qty']:g} {p['unit']} of {p['name']} in stock — can't bill {new_qty:g}."
            )
        line = compute_line(p["name"], p["hsn"], new_qty, p["unit"], p["sell_price"], p["gst_rate"])
        tx.execute(
            """INSERT INTO bill_items (bill_id, product_id, qty, unit_price, gst_rate, hsn,
                                       taxable_value, cgst, sgst, line_total)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bill_id, product_id) DO UPDATE SET
                   qty=excluded.qty, unit_price=excluded.unit_price,
                   taxable_value=excluded.taxable_value, cgst=excluded.cgst,
                   sgst=excluded.sgst, line_total=excluded.line_total""",
            (bill_id, product_id, new_qty, p["sell_price"], p["gst_rate"], p["hsn"],
             float(line.taxable_value), float(line.cgst), float(line.sgst), float(line.line_total)),
        )
    return get_bill(bill_id)


def set_item_qty(bill_id: int, product_id: int, qty: float) -> dict:
    """Set an exact quantity, or remove the line with qty=0 ("drop the butter")."""
    with transaction() as tx:
        _require_draft(tx, bill_id)
        if qty <= 0:
            tx.execute("DELETE FROM bill_items WHERE bill_id=? AND product_id=?",
                       (bill_id, product_id))
            return get_bill(bill_id)
        p = tx.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            raise NotFoundError(f"No product with id {product_id}.")
        if not p["is_loose"] and abs(qty - round(qty)) > 1e-9:
            raise GuardrailError(f"{p['name']} is a packaged item — quantity must be a whole number.")
        if qty > p["qty"]:
            raise OversellError(
                f"Only {p['qty']:g} {p['unit']} of {p['name']} in stock — can't bill {qty:g}."
            )
        line = compute_line(p["name"], p["hsn"], qty, p["unit"], p["sell_price"], p["gst_rate"])
        cur = tx.execute(
            """UPDATE bill_items SET qty=?, taxable_value=?, cgst=?, sgst=?, line_total=?
               WHERE bill_id=? AND product_id=?""",
            (qty, float(line.taxable_value), float(line.cgst), float(line.sgst),
             float(line.line_total), bill_id, product_id),
        )
        if cur.rowcount == 0:
            raise NotFoundError(f"{p['name']} is not on bill #{bill_id}. Add it first.")
    return get_bill(bill_id)


def set_payment(bill_id: int, mode: str, reference: str | None = None,
                khata_customer: str | None = None) -> dict:
    mode = mode.lower().strip()
    if mode not in PAYMENT_MODES:
        raise GuardrailError(f"Unknown payment mode '{mode}'. Use cash, upi, card or khata.")
    with transaction() as tx:
        _require_draft(tx, bill_id)
        customer_id = None
        if mode == "khata":
            if not khata_customer:
                raise GuardrailError("Khata payment needs a customer name.")
            customer_id = khata_service.get_or_create_customer_tx(tx, khata_customer)
        tx.execute(
            "UPDATE bills SET payment_mode=?, payment_ref=?, khata_customer_id=? WHERE id=?",
            (mode, reference, customer_id, bill_id),
        )
    return get_bill(bill_id)


def cancel_bill(bill_id: int) -> dict:
    with transaction() as tx:
        _require_draft(tx, bill_id)
        tx.execute("UPDATE bills SET status='cancelled' WHERE id=?", (bill_id,))
    return get_bill(bill_id)


# ------------------------------------------------------------------ finalize

def _next_invoice_no(tx) -> str:
    today = datetime.now(IST).strftime("%Y%m%d")
    row = tx.execute(
        "SELECT COUNT(*) c FROM bills WHERE invoice_no LIKE ?", (f"INV-{today}-%",)
    ).fetchone()
    return f"INV-{today}-{row['c'] + 1:03d}"


def finalize_bill(bill_id: int, idempotency_key: str) -> dict:
    """Atomically price, decrement stock, post khata, and number the invoice.

    Retry-safe: a redelivered finalize returns the already-finalized bill.
    """
    if not idempotency_key:
        raise GuardrailError("finalize requires an idempotency key.")

    with transaction() as tx:
        bill = tx.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
        if not bill:
            raise NotFoundError(f"No bill #{bill_id}.")

        # --- idempotency: same bill already finalized → return it, no side effects
        if bill["status"] == "finalized":
            return get_bill(bill_id)
        # --- idempotency: key already consumed by another row (paranoia case)
        dup = tx.execute(
            "SELECT id FROM bills WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if dup:
            return get_bill(dup["id"])
        if bill["status"] != "draft":
            raise GuardrailError(f"Bill #{bill_id} is {bill['status']}.")

        items = tx.execute(
            """SELECT bi.*, p.name, p.unit, p.is_loose FROM bill_items bi
               JOIN products p ON p.id = bi.product_id WHERE bi.bill_id=?""",
            (bill_id,),
        ).fetchall()
        if not items:
            raise GuardrailError("Bill is empty — add items before finalizing.")
        if not bill["payment_mode"]:
            raise GuardrailError("Set a payment mode (cash / upi / card / khata) before finalizing.")

        # --- oversell guard: guarded decrement, all-or-nothing
        for it in items:
            cur = tx.execute(
                "UPDATE products SET qty = qty - ? WHERE id = ? AND qty >= ?",
                (it["qty"], it["product_id"], it["qty"]),
            )
            if cur.rowcount == 0:
                p = tx.execute("SELECT name, qty, unit FROM products WHERE id=?",
                               (it["product_id"],)).fetchone()
                raise OversellError(
                    f"Only {p['qty']:g} {p['unit']} of {p['name']} left — "
                    f"can't sell {it['qty']:g}. Bill NOT finalized; adjust the quantity."
                )
            tx.execute(
                """INSERT INTO stock_moves (product_id, delta, reason, ref_bill_id)
                   VALUES (?,?, 'sale', ?)""",
                (it["product_id"], -it["qty"], bill_id),
            )

        # --- totals from the GST engine
        lines = [
            compute_line(it["name"], it["hsn"], it["qty"], it["unit"],
                         it["unit_price"], it["gst_rate"])
            for it in items
        ]
        totals = compute_totals(lines)
        invoice_no = _next_invoice_no(tx)
        tx.execute(
            """UPDATE bills SET status='finalized', subtotal=?, tax_total=?, round_off=?,
                   grand_total=?, idempotency_key=?, invoice_no=?,
                   finalized_at=datetime('now') WHERE id=?""",
            (float(totals.subtotal), float(totals.tax_total), float(totals.round_off),
             float(totals.grand_total), idempotency_key, invoice_no, bill_id),
        )

        # --- khata posting inside the same transaction
        if bill["payment_mode"] == "khata":
            khata_service.post_credit_tx(
                tx, bill["khata_customer_id"], float(totals.grand_total),
                bill_id, f"Bill {invoice_no}",
            )

    return get_bill(bill_id)


# ------------------------------------------------------------------- queries

def get_bill(bill_id: int) -> dict:
    conn = get_conn()
    bill = conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
    if not bill:
        raise NotFoundError(f"No bill #{bill_id}.")
    items = conn.execute(
        """SELECT bi.*, p.name, p.unit FROM bill_items bi
           JOIN products p ON p.id=bi.product_id WHERE bi.bill_id=? ORDER BY bi.id""",
        (bill_id,),
    ).fetchall()
    out = dict(bill)
    out["items"] = [dict(i) for i in items]
    if bill["status"] == "draft" and items:
        lines = [compute_line(i["name"], i["hsn"], i["qty"], i["unit"],
                              i["unit_price"], i["gst_rate"]) for i in items]
        t = compute_totals(lines)
        out.update(subtotal=float(t.subtotal), tax_total=float(t.tax_total),
                   round_off=float(t.round_off), grand_total=float(t.grand_total))
    if bill["khata_customer_id"]:
        c = conn.execute("SELECT name FROM customers WHERE id=?",
                         (bill["khata_customer_id"],)).fetchone()
        out["khata_customer"] = c["name"] if c else None
    return out


def latest_finalized_bill(chat_id: int | None = None) -> dict | None:
    conn = get_conn()
    if chat_id is not None:
        row = conn.execute(
            """SELECT id FROM bills WHERE status='finalized' AND chat_id=?
               ORDER BY finalized_at DESC, id DESC LIMIT 1""", (chat_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM bills WHERE status='finalized' ORDER BY finalized_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return get_bill(row["id"]) if row else None
