import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "grocery.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"

CATEGORIES = [
    "Produce", "Dairy & Eggs", "Meat & Seafood", "Bakery", "Pantry",
    "Frozen", "Snacks", "Beverages", "Household", "Personal Care",
    "Baby", "Pet", "Alcohol", "Other"
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    with open(SCHEMA_PATH) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


def save_receipt(store, purchase_date, printed_total, items):
    """items: list of dicts with name, category, quantity, unit_price, line_total"""
    computed_total = round(sum(i["line_total"] for i in items), 2)
    mismatch = 1 if printed_total is not None and abs(computed_total - printed_total) > 0.01 else 0

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO receipts (store, purchase_date, printed_total, computed_total, total_mismatch) "
        "VALUES (?, ?, ?, ?, ?)",
        (store, purchase_date, printed_total, computed_total, mismatch)
    )
    receipt_id = cur.lastrowid

    for item in items:
        cur.execute(
            "INSERT INTO items (receipt_id, name, category, quantity, unit_price, line_total) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (receipt_id, item["name"], item["category"], item.get("quantity", 1),
             item.get("unit_price"), item["line_total"])
        )
    conn.commit()
    conn.close()
    return receipt_id, computed_total, mismatch


def get_category_totals():
    conn = get_conn()
    rows = conn.execute(
        "SELECT category, ROUND(SUM(line_total), 2) AS total, COUNT(*) AS n "
        "FROM items GROUP BY category ORDER BY total DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recurring_items(min_occurrences=3):
    """Items purchased on >= min_occurrences distinct receipts, with frequency stats."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT
            i.name,
            i.category,
            COUNT(DISTINCT i.receipt_id) AS purchase_count,
            ROUND(AVG(i.line_total), 2) AS avg_spend,
            MIN(r.purchase_date) AS first_seen,
            MAX(r.purchase_date) AS last_seen
        FROM items i
        JOIN receipts r ON r.id = i.receipt_id
        GROUP BY i.name, i.category
        HAVING purchase_count >= ?
        ORDER BY purchase_count DESC
        """,
        (min_occurrences,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_receipts():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM receipts ORDER BY purchase_date DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_receipt_items(receipt_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM items WHERE receipt_id = ? ORDER BY id", (receipt_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
