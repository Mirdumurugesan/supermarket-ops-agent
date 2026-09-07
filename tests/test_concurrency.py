"""Concurrency: two bills, or a sale racing a stock-in, must not corrupt stock.

Each thread gets its own SQLite connection (the database module is
thread-local by design). ``BEGIN IMMEDIATE`` serializes writers, and the
guarded decrement plus the CHECK constraint make an interleaved oversell
impossible — the loser of the race gets a clean refusal, not bad data.
"""

from __future__ import annotations

import threading

from kirana.db import database
from kirana.services import billing_service as billing
from kirana.services import inventory_service as inv
from kirana.services.errors import OversellError


def _worker(fn, results, idx):
    try:
        results[idx] = ("ok", fn())
    except Exception as e:  # noqa: BLE001
        results[idx] = ("err", e)


def test_two_bills_racing_for_the_last_units(db, monkeypatch):
    """Maggi = 6. Two bills of 4 each finalize concurrently → exactly one wins."""
    monkeypatch.setattr(database, "DB_PATH", db)

    ids = []
    for chat in (111, 222):
        b = billing.create_draft(chat)
        billing.add_item(b["id"], 1, 4)
        billing.set_payment(b["id"], "cash")
        ids.append(b["id"])

    results: dict[int, tuple] = {}
    threads = [
        threading.Thread(target=_worker,
                         args=(lambda i=i, bid=bid: billing.finalize_bill(bid, f"race-{i}"),
                               results, i))
        for i, bid in enumerate(ids)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outcomes = [r[0] for r in results.values()]
    assert outcomes.count("ok") == 1, f"expected exactly one winner, got {results}"
    assert outcomes.count("err") == 1
    loser = next(r[1] for r in results.values() if r[0] == "err")
    assert isinstance(loser, OversellError)
    assert inv.get_product(1)["qty"] == 2          # 6 - 4, never negative


def test_sale_racing_a_stock_in_keeps_the_books_consistent(db, monkeypatch):
    """A finalize and a goods-receipt in flight together: final qty = 100 - 30 + 50."""
    monkeypatch.setattr(database, "DB_PATH", db)

    b = billing.create_draft(333)
    billing.add_item(b["id"], 2, 30)
    billing.set_payment(b["id"], "upi")

    results: dict[int, tuple] = {}
    t1 = threading.Thread(target=_worker,
                          args=(lambda: billing.finalize_bill(b["id"], "mix-1"), results, 0))
    t2 = threading.Thread(target=_worker,
                          args=(lambda: inv.receive_stock(2, 50), results, 1))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert all(r[0] == "ok" for r in results.values()), results
    assert inv.get_product(2)["qty"] == 120


def test_many_small_concurrent_sales_sum_exactly(db, monkeypatch):
    """10 threads × 5kg rice from 200kg → exactly 150kg left, no lost updates."""
    monkeypatch.setattr(database, "DB_PATH", db)

    bill_ids = []
    for i in range(10):
        b = billing.create_draft(1000 + i)
        billing.add_item(b["id"], 4, 5)
        billing.set_payment(b["id"], "cash")
        bill_ids.append(b["id"])

    results: dict[int, tuple] = {}
    threads = [
        threading.Thread(target=_worker,
                         args=(lambda bid=bid, i=i: billing.finalize_bill(bid, f"bulk-{i}"),
                               results, i))
        for i, bid in enumerate(bill_ids)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r[0] == "ok" for r in results.values()), results
    assert inv.get_product(4)["qty"] == 150
