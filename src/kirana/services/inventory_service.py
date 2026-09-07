"""Inventory: search, add, receive stock, low-stock reporting.

Guardrails enforced here:
* Products are deactivated, never deleted (audit trail stays intact).
* Selling price below cost requires an explicit ``allow_below_cost`` override.
* Every stock change writes a ``stock_moves`` audit row.
"""

from __future__ import annotations

from ..db.database import get_conn, transaction
from .errors import GuardrailError, NotFoundError


def _row_to_dict(r) -> dict:
    return dict(r) if r is not None else None


def search_products(query: str, include_inactive: bool = False, limit: int = 8) -> list[dict]:
    """Fuzzy search over name, brand and colloquial aliases."""
    conn = get_conn()
    q = f"%{query.strip().lower()}%"
    rows = conn.execute(
        """SELECT * FROM products
           WHERE (lower(name) LIKE ? OR lower(coalesce(brand,'')) LIKE ?
                  OR lower(aliases) LIKE ?)
                 AND (active = 1 OR ?)
           ORDER BY active DESC, qty DESC LIMIT ?""",
        (q, q, q, int(include_inactive), limit),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_product(product_id: int) -> dict:
    row = get_conn().execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not row:
        raise NotFoundError(f"No product with id {product_id}.")
    return _row_to_dict(row)


def add_product(name: str, unit: str, hsn: str, gst_rate: float, cost_price: float,
                sell_price: float, mrp: float | None = None, qty: float = 0,
                brand: str | None = None, is_loose: bool = False,
                reorder_level: float = 5, aliases: str = "",
                allow_below_cost: bool = False) -> dict:
    if sell_price < cost_price and not allow_below_cost:
        raise GuardrailError(
            f"Sell price ₹{sell_price} is below cost ₹{cost_price}. "
            "Refused — confirm explicitly if you really want to sell below cost."
        )
    if mrp is not None and sell_price > mrp:
        raise GuardrailError(f"Sell price ₹{sell_price} is above MRP ₹{mrp} — that is illegal. Refused.")
    with transaction() as tx:
        cur = tx.execute(
            """INSERT INTO products (name, brand, unit, is_loose, hsn, gst_rate, cost_price,
                                     sell_price, mrp, qty, reorder_level, aliases)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, brand, unit, int(is_loose), hsn, gst_rate, cost_price,
             sell_price, mrp, qty, reorder_level, aliases),
        )
        pid = cur.lastrowid
        if qty:
            tx.execute("INSERT INTO stock_moves (product_id, delta, reason) VALUES (?,?,'receive')",
                       (pid, qty))
    return get_product(pid)


def receive_stock(product_id: int, qty: float, cost_price: float | None = None,
                  sell_price: float | None = None, mrp: float | None = None) -> dict:
    if qty <= 0:
        raise GuardrailError("Received quantity must be positive.")
    with transaction() as tx:
        row = tx.execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not row:
            raise NotFoundError(f"No active product with id {product_id}.")
        new_cost = cost_price if cost_price is not None else row["cost_price"]
        new_sell = sell_price if sell_price is not None else row["sell_price"]
        new_mrp = mrp if mrp is not None else row["mrp"]
        if new_mrp is not None and new_sell > new_mrp:
            raise GuardrailError(f"Sell price ₹{new_sell} would exceed MRP ₹{new_mrp}. Refused.")
        tx.execute(
            """UPDATE products SET qty = qty + ?, cost_price=?, sell_price=?, mrp=?,
                                   updated_at=datetime('now') WHERE id=?""",
            (qty, new_cost, new_sell, new_mrp, product_id),
        )
        tx.execute("INSERT INTO stock_moves (product_id, delta, reason) VALUES (?,?,'receive')",
                   (product_id, qty))
    return get_product(product_id)


def update_product(product_id: int, sell_price: float | None = None, mrp: float | None = None,
                   reorder_level: float | None = None, aliases: str | None = None,
                   active: bool | None = None, allow_below_cost: bool = False) -> dict:
    p = get_product(product_id)
    new_sell = sell_price if sell_price is not None else p["sell_price"]
    new_mrp = mrp if mrp is not None else p["mrp"]
    if new_sell < p["cost_price"] and not allow_below_cost:
        raise GuardrailError(
            f"Sell price ₹{new_sell} is below cost ₹{p['cost_price']}. "
            "Refused — confirm explicitly to override."
        )
    if new_mrp is not None and new_sell > new_mrp:
        raise GuardrailError(f"Sell price ₹{new_sell} is above MRP ₹{new_mrp}. Refused.")
    with transaction() as tx:
        tx.execute(
            """UPDATE products SET sell_price=?, mrp=?,
                   reorder_level=coalesce(?, reorder_level),
                   aliases=coalesce(?, aliases),
                   active=coalesce(?, active),
                   updated_at=datetime('now')
               WHERE id=?""",
            (new_sell, new_mrp, reorder_level, aliases,
             None if active is None else int(active), product_id),
        )
    return get_product(product_id)


def adjust_stock(product_id: int, new_qty: float, reason: str = "adjust") -> dict:
    """Physical count correction. Direct deletion of stock is not exposed."""
    if new_qty < 0:
        raise GuardrailError("Stock cannot be set below zero.")
    with transaction() as tx:
        row = tx.execute("SELECT qty FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise NotFoundError(f"No product with id {product_id}.")
        delta = new_qty - row["qty"]
        tx.execute("UPDATE products SET qty=?, updated_at=datetime('now') WHERE id=?",
                   (new_qty, product_id))
        tx.execute("INSERT INTO stock_moves (product_id, delta, reason) VALUES (?,?,?)",
                   (product_id, delta, reason))
    return get_product(product_id)


def low_stock(limit: int = 20) -> list[dict]:
    rows = get_conn().execute(
        """SELECT * FROM products WHERE active=1 AND qty <= reorder_level
           ORDER BY (qty * 1.0 / NULLIF(reorder_level,0)) ASC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]
