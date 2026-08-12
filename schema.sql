-- Grocery database schema (SQLite)

CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store TEXT NOT NULL,
    purchase_date TEXT NOT NULL,        -- ISO format YYYY-MM-DD
    printed_total REAL,
    computed_total REAL,
    total_mismatch INTEGER DEFAULT 0,   -- 1 if printed vs computed differ
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity REAL DEFAULT 1,
    unit_price REAL,
    line_total REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_items_name ON items(name);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);
CREATE INDEX IF NOT EXISTS idx_receipts_date ON receipts(purchase_date);
