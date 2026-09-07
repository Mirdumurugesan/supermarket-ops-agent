"""Khata (customer credit ledger) — a first-class kirana concept.

Guardrails:
* A payment can only be settled against a customer that exists.
* Amounts must be positive; balances update atomically with the entry row.
* ``post_credit_tx`` runs inside the finalize transaction so a khata bill and
  its ledger entry commit (or roll back) together.
"""

from __future__ import annotations

from ..db.database import get_conn, transaction
from .errors import GuardrailError, NotFoundError


def get_or_create_customer_tx(tx, name: str) -> int:
    name = name.strip()
    if not name:
        raise GuardrailError("Customer name is required.")
    row = tx.execute("SELECT id FROM customers WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row:
        return row["id"]
    cur = tx.execute("INSERT INTO customers (name) VALUES (?)", (name,))
    return cur.lastrowid


def _find_customer(conn, name: str):
    return conn.execute("SELECT * FROM customers WHERE name=? COLLATE NOCASE",
                        (name.strip(),)).fetchone()


def post_credit_tx(tx, customer_id: int, amount: float, bill_id: int | None, note: str) -> None:
    if amount <= 0:
        raise GuardrailError("Credit amount must be positive.")
    tx.execute(
        """INSERT INTO khata_entries (customer_id, bill_id, amount, entry_type, note)
           VALUES (?,?,?,'credit',?)""",
        (customer_id, bill_id, amount, note),
    )
    tx.execute("UPDATE customers SET balance = balance + ? WHERE id=?", (amount, customer_id))


def add_credit(name: str, amount: float, note: str | None = None) -> dict:
    """"Put ₹500 on Ramesh's credit" — creates the customer if new."""
    with transaction() as tx:
        cid = get_or_create_customer_tx(tx, name)
        post_credit_tx(tx, cid, amount, None, note or "Manual credit")
    return balance(name)


def record_payment(name: str, amount: float, note: str | None = None) -> dict:
    """"Ramesh paid ₹300" — refuses if the customer has no khata."""
    if amount <= 0:
        raise GuardrailError("Payment amount must be positive.")
    with transaction() as tx:
        cust = tx.execute("SELECT * FROM customers WHERE name=? COLLATE NOCASE",
                          (name.strip(),)).fetchone()
        if not cust:
            raise NotFoundError(
                f"No khata exists for '{name}'. I can't settle a khata that doesn't exist — "
                "check the name or start a credit first."
            )
        if amount > cust["balance"] + 0.005:
            raise GuardrailError(
                f"{cust['name']}'s balance is ₹{cust['balance']:.2f} — "
                f"a payment of ₹{amount:.2f} would overshoot. Confirm the amount."
            )
        tx.execute(
            """INSERT INTO khata_entries (customer_id, amount, entry_type, note)
               VALUES (?,?,'payment',?)""",
            (cust["id"], amount, note or "Payment received"),
        )
        tx.execute("UPDATE customers SET balance = balance - ? WHERE id=?",
                   (amount, cust["id"]))
    return balance(name)


def balance(name: str) -> dict:
    conn = get_conn()
    cust = _find_customer(conn, name)
    if not cust:
        raise NotFoundError(f"No khata exists for '{name}'.")
    return {"customer": cust["name"], "balance": round(cust["balance"], 2)}


def statement(name: str, limit: int = 15) -> dict:
    conn = get_conn()
    cust = _find_customer(conn, name)
    if not cust:
        raise NotFoundError(f"No khata exists for '{name}'.")
    rows = conn.execute(
        """SELECT entry_type, amount, note, created_at FROM khata_entries
           WHERE customer_id=? ORDER BY id DESC LIMIT ?""",
        (cust["id"], limit),
    ).fetchall()
    return {
        "customer": cust["name"],
        "balance": round(cust["balance"], 2),
        "entries": [dict(r) for r in rows],
    }


def all_balances() -> list[dict]:
    rows = get_conn().execute(
        "SELECT name, balance FROM customers WHERE balance != 0 ORDER BY balance DESC"
    ).fetchall()
    return [dict(r) for r in rows]
