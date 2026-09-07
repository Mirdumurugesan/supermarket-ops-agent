"""Analytics: daily close, sales ranges, top items, reorder suggestions.

All dates are computed in IST (Asia/Kolkata) — a kirana closes its day on
Indian time, not UTC. ``finalized_at`` is stored in UTC by SQLite, so queries
shift by +05:30 before bucketing into local dates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..db.database import get_conn

IST = ZoneInfo("Asia/Kolkata")
IST_SHIFT = "+330 minutes"  # applied to UTC timestamps to get IST calendar dates


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def daily_summary(date: str | None = None) -> dict:
    """Daily close: totals, tax, payment split, khata exposure, top items."""
    date = date or today_ist()
    conn = get_conn()
    head = conn.execute(
        f"""SELECT COUNT(*) bills, coalesce(SUM(grand_total),0) revenue,
                   coalesce(SUM(tax_total),0) tax_collected
            FROM bills WHERE status='finalized'
              AND date(finalized_at, '{IST_SHIFT}') = ?""",
        (date,),
    ).fetchone()
    modes = conn.execute(
        f"""SELECT payment_mode, COUNT(*) n, coalesce(SUM(grand_total),0) amount
            FROM bills WHERE status='finalized'
              AND date(finalized_at, '{IST_SHIFT}') = ?
            GROUP BY payment_mode""",
        (date,),
    ).fetchall()
    top = conn.execute(
        f"""SELECT p.name, SUM(bi.qty) qty, p.unit, SUM(bi.line_total) revenue
            FROM bill_items bi
            JOIN bills b ON b.id = bi.bill_id
            JOIN products p ON p.id = bi.product_id
            WHERE b.status='finalized' AND date(b.finalized_at, '{IST_SHIFT}') = ?
            GROUP BY bi.product_id ORDER BY revenue DESC LIMIT 5""",
        (date,),
    ).fetchall()
    khata_out = conn.execute(
        "SELECT coalesce(SUM(balance),0) total FROM customers WHERE balance > 0"
    ).fetchone()
    return {
        "date": date,
        "bills": head["bills"],
        "revenue": round(head["revenue"], 2),
        "tax_collected": round(head["tax_collected"], 2),
        "by_payment_mode": [dict(m) for m in modes],
        "top_items": [dict(t) for t in top],
        "khata_outstanding": round(khata_out["total"], 2),
    }


def sales_range(date_from: str, date_to: str) -> dict:
    """Aggregates for an inclusive IST date range (used by the analysis deck)."""
    conn = get_conn()
    daily = conn.execute(
        f"""SELECT date(finalized_at, '{IST_SHIFT}') d, COUNT(*) bills,
                   SUM(grand_total) revenue, SUM(tax_total) tax
            FROM bills WHERE status='finalized'
              AND date(finalized_at, '{IST_SHIFT}') BETWEEN ? AND ?
            GROUP BY d ORDER BY d""",
        (date_from, date_to),
    ).fetchall()
    top = conn.execute(
        f"""SELECT p.name, SUM(bi.qty) qty, p.unit, SUM(bi.line_total) revenue,
                   SUM((bi.unit_price - p.cost_price) * bi.qty) est_margin
            FROM bill_items bi
            JOIN bills b ON b.id=bi.bill_id
            JOIN products p ON p.id=bi.product_id
            WHERE b.status='finalized'
              AND date(b.finalized_at, '{IST_SHIFT}') BETWEEN ? AND ?
            GROUP BY bi.product_id ORDER BY revenue DESC LIMIT 10""",
        (date_from, date_to),
    ).fetchall()
    modes = conn.execute(
        f"""SELECT payment_mode, COUNT(*) n, SUM(grand_total) amount
            FROM bills WHERE status='finalized'
              AND date(finalized_at, '{IST_SHIFT}') BETWEEN ? AND ?
            GROUP BY payment_mode""",
        (date_from, date_to),
    ).fetchall()
    slabs = conn.execute(
        f"""SELECT bi.gst_rate, SUM(bi.taxable_value) taxable,
                   SUM(bi.cgst + bi.sgst) tax
            FROM bill_items bi JOIN bills b ON b.id=bi.bill_id
            WHERE b.status='finalized'
              AND date(b.finalized_at, '{IST_SHIFT}') BETWEEN ? AND ?
            GROUP BY bi.gst_rate ORDER BY bi.gst_rate""",
        (date_from, date_to),
    ).fetchall()
    return {
        "from": date_from,
        "to": date_to,
        "daily": [dict(r) for r in daily],
        "top_items": [dict(r) for r in top],
        "by_payment_mode": [dict(r) for r in modes],
        "by_gst_slab": [dict(r) for r in slabs],
        "totals": {
            "revenue": round(sum(r["revenue"] or 0 for r in daily), 2),
            "tax": round(sum(r["tax"] or 0 for r in daily), 2),
            "bills": sum(r["bills"] for r in daily),
        },
    }


def reorder_suggestions(days: int = 7) -> list[dict]:
    """Stretch goal: reorder based on sales velocity vs current stock."""
    since = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT p.id, p.name, p.unit, p.qty, p.reorder_level,
                   coalesce(SUM(bi.qty), 0) sold
            FROM products p
            LEFT JOIN bill_items bi ON bi.product_id = p.id
            LEFT JOIN bills b ON b.id = bi.bill_id AND b.status='finalized'
                 AND date(b.finalized_at, '{IST_SHIFT}') >= ?
            WHERE p.active=1
            GROUP BY p.id""",
        (since,),
    ).fetchall()
    out = []
    for r in rows:
        velocity = r["sold"] / days  # units/day
        days_left = (r["qty"] / velocity) if velocity > 0 else None
        if r["qty"] <= r["reorder_level"] or (days_left is not None and days_left < 4):
            out.append({
                "product": r["name"], "in_stock": r["qty"], "unit": r["unit"],
                "sold_last_n_days": r["sold"], "velocity_per_day": round(velocity, 2),
                "days_of_stock_left": round(days_left, 1) if days_left is not None else None,
                "suggested_order_qty": max(round(velocity * 10) or 0, r["reorder_level"]),
            })
    out.sort(key=lambda x: (x["days_of_stock_left"] is None, x["days_of_stock_left"] or 0))
    return out
