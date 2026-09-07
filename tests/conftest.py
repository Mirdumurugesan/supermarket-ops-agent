"""Test fixtures: each test gets a fresh, isolated SQLite database.

The suite must run on a clean clone with no API keys and no network — that is
the point of testing the store rather than the model. Two things make that
true, and both have to happen *before* `kirana.config` is imported, because it
resolves the model chain at module level:
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 1. Placeholder provider keys. No test ever calls a provider — the model is
#    always a scripted FunctionModel — but *constructing* the model chain reads
#    each key from the environment. Without these, `pytest` on a fresh checkout
#    fails for a reason unrelated to the code under test. Tests that care about
#    a key being absent delete it themselves via monkeypatch.
for _var in ("GROQ_API_KEY", "GOOGLE_API_KEY"):
    os.environ.setdefault(_var, "placeholder-never-called")

# 2. Ignore any developer .env sitting in the repo. Otherwise the suite's
#    behaviour depends on whose machine it runs on, which is how a test ends up
#    passing in CI and failing for the person who cloned it.
os.environ.setdefault("KIRANA_MODELS",
                      "groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b")

from kirana.db import database  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Point every module at a temp DB, initialize schema, seed a few products."""
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", path)
    database._local = type(database._local)()  # drop cached thread connections
    database.init_db(path)

    with database.transaction(path) as tx:
        tx.executemany(
            """INSERT INTO products (id, name, unit, is_loose, hsn, gst_rate, cost_price,
                                     sell_price, mrp, qty, reorder_level, aliases)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                # id, name, unit, loose, hsn, gst, cost, sell, mrp, qty, reorder, aliases
                (1, "Maggi Noodles 70g", "packet", 0, "1902", 12, 12, 14, 14, 6, 30, "maggi"),
                (2, "Loose Sugar (per kg)", "kg", 1, "1701", 5, 40, 44, None, 100, 25, "sugar"),
                (3, "Aashirvaad Atta 5kg", "packet", 0, "1101", 5, 240, 265, 285, 10, 8, "atta"),
                (4, "Loose Rice (per kg)", "kg", 1, "1006", 0, 52, 62, None, 200, 50, "rice"),
                (5, "Surf Excel 1kg", "packet", 0, "3402", 18, 118, 135, 140, 4, 5, "surf"),
            ],
        )
        tx.execute("INSERT INTO customers (id, name, balance) VALUES (1,'Ramesh',0)")
        tx.executemany(
            "INSERT INTO preferences (scope,key,value) VALUES ('store',?,?)",
            [("shop_name", "Test Stores"), ("gstin", "33ABCDE1234F1Z5")],
        )
    yield path
