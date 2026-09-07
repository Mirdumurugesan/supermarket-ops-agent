-- Kirana Ops Agent — SQLite schema
-- Business invariants are enforced HERE (constraints) and in the service layer
-- (guarded transactions), never in the LLM prompt.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    brand         TEXT,
    unit          TEXT    NOT NULL DEFAULT 'piece',      -- kg | g | litre | ml | packet | dozen | piece
    is_loose      INTEGER NOT NULL DEFAULT 0,            -- loose items sold by weight/volume (fractional qty ok)
    hsn           TEXT    NOT NULL,
    gst_rate      REAL    NOT NULL CHECK (gst_rate >= 0 AND gst_rate <= 40),  -- percent, data-driven per SKU
    cost_price    REAL    NOT NULL CHECK (cost_price >= 0),
    sell_price    REAL    NOT NULL CHECK (sell_price >= 0),   -- GST-inclusive selling price
    mrp           REAL    CHECK (mrp IS NULL OR mrp >= 0),
    qty           REAL    NOT NULL DEFAULT 0 CHECK (qty >= 0),  -- HARD oversell guard: stock can never go negative
    reorder_level REAL    NOT NULL DEFAULT 5,
    aliases       TEXT    NOT NULL DEFAULT '',           -- comma-separated colloquial names ("atta", "surf")
    active        INTEGER NOT NULL DEFAULT 1,            -- products are deactivated, never deleted
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_products_name ON products(name);

CREATE TABLE IF NOT EXISTS bills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','finalized','cancelled')),
    customer_name   TEXT,
    payment_mode    TEXT    CHECK (payment_mode IS NULL OR payment_mode IN ('cash','upi','card','khata')),
    payment_ref     TEXT,
    subtotal        REAL,           -- sum of taxable values
    tax_total       REAL,           -- CGST + SGST
    round_off       REAL,
    grand_total     REAL,           -- rounded, payable
    khata_customer_id INTEGER REFERENCES customers(id),
    idempotency_key TEXT UNIQUE,    -- set on finalize; retried finalize is a no-op returning the same bill
    invoice_no      TEXT UNIQUE,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finalized_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_bills_chat_status ON bills(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_bills_finalized_at ON bills(finalized_at);

CREATE TABLE IF NOT EXISTS bill_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id       INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products(id),
    qty           REAL    NOT NULL CHECK (qty > 0),
    unit_price    REAL    NOT NULL,        -- GST-inclusive price captured at sale time
    gst_rate      REAL    NOT NULL,
    hsn           TEXT    NOT NULL,
    taxable_value REAL    NOT NULL,
    cgst          REAL    NOT NULL,
    sgst          REAL    NOT NULL,
    line_total    REAL    NOT NULL,
    UNIQUE (bill_id, product_id)
);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone      TEXT,
    balance    REAL NOT NULL DEFAULT 0,     -- positive = customer owes the store
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS khata_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    bill_id     INTEGER REFERENCES bills(id),
    amount      REAL NOT NULL CHECK (amount > 0),
    entry_type  TEXT NOT NULL CHECK (entry_type IN ('credit','payment')),
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Full audit trail of every stock change (receive, sale, adjustment).
CREATE TABLE IF NOT EXISTS stock_moves (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id  INTEGER NOT NULL REFERENCES products(id),
    delta       REAL    NOT NULL,
    reason      TEXT    NOT NULL,           -- 'receive' | 'sale' | 'adjust'
    ref_bill_id INTEGER REFERENCES bills(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Owner preferences: durable memory that survives /new chats and restarts.
-- scope 'store' = applies to the whole shop (shop name, GSTIN, default payment mode...).
CREATE TABLE IF NOT EXISTS preferences (
    scope      TEXT NOT NULL DEFAULT 'store',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (scope, key)
);

-- Telegram delivers at-least-once; ingress dedup by update_id.
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id    INTEGER PRIMARY KEY,
    processed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Agent session per Telegram chat (cleared by /new; preferences are NOT).
CREATE TABLE IF NOT EXISTS chat_sessions (
    chat_id    INTEGER PRIMARY KEY,
    session_id TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Files generated by tools, delivered to Telegram after the agent turn.
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    file_path  TEXT    NOT NULL,
    caption    TEXT,
    sent       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
