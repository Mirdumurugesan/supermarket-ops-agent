"""SQLite access layer.

Design notes (this is where several "hard parts" live):

* WAL mode + ``BEGIN IMMEDIATE`` transactions give us real write serialization,
  so two bills (or a sale + a stock-in) racing each other can never corrupt
  stock. SQLite takes a single writer lock per transaction; combined with the
  ``qty >= 0`` CHECK constraint and guarded UPDATEs in the billing service,
  oversell is impossible at the *database* layer — not merely discouraged in
  the prompt.

* Every mutating service function runs inside :func:`transaction`, which
  retries on ``SQLITE_BUSY`` and always commits or rolls back atomically.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DB_PATH = os.environ.get("KIRANA_DB_PATH", str(Path("data") / "kirana.db"))

_local = threading.local()


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)  # autocommit; we manage txns
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    """One connection per thread (sqlite3 objects are not thread-safe)."""
    path = db_path or DB_PATH
    key = f"conn_{path}"
    conn = getattr(_local, key, None)
    if conn is None:
        conn = _connect(path)
        setattr(_local, key, conn)
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA_PATH.read_text())


@contextmanager
def transaction(db_path: str | None = None, retries: int = 5):
    """Serialized write transaction with busy-retry.

    ``BEGIN IMMEDIATE`` acquires the write lock up front, so concurrent
    finalizes queue up instead of interleaving.
    """
    conn = get_conn(db_path)
    attempt = 0
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(0.2 * attempt)
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
