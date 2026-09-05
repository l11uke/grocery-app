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


def _compute_item_intervals(min_occurrences=3):
    """Shared helper: pulls every (name, category) item group, computes
    purchase intervals and CV, and splits into 'scored' (enough data to
    judge regularity) vs 'insufficient' (not enough purchases yet).

    Used by both get_recurring_items() and get_shopping_lists() so the
    interval math lives in exactly one place.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT i.name, i.category, r.purchase_date, i.line_total
        FROM items i
        JOIN receipts r ON r.id = i.receipt_id
        ORDER BY i.name, i.category, r.purchase_date
        """
    ).fetchall()
    conn.close()

    from collections import defaultdict
    from datetime import date
    from statistics import mean, stdev

    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["name"], row["category"])].append(row)

    scored, insufficient = [], []
    for (name, category), item_rows in grouped.items():
        # Skip rows with a missing/unparseable date rather than crashing
        # the whole request. A single bad receipt (e.g. date unreadable on
        # the photo, saved as an empty string) shouldn't take down every
        # other item's scoring.
        valid_dates = set()
        for r in item_rows:
            raw = r["purchase_date"]
            if not raw:
                continue
            try:
                valid_dates.add(date.fromisoformat(raw))
            except ValueError:
                continue

        if not valid_dates:
            # Every row for this item had a bad date — nothing to score.
            continue

        dates = sorted(valid_dates)
        # avg_spend should only reflect rows we could actually date, so it
        # stays consistent with purchase_count below.
        valid_rows = [r for r in item_rows if r["purchase_date"] in
                      {d.isoformat() for d in valid_dates}]
        purchase_count = len(dates)
        avg_spend = round(mean(r["line_total"] for r in valid_rows), 2)
        first_seen, last_seen = dates[0].isoformat(), dates[-1].isoformat()

        if purchase_count < min_occurrences:
            insufficient.append({
                "name": name,
                "category": category,
                "purchase_count": purchase_count,
                "avg_spend": avg_spend,
                "first_seen": first_seen,
                "last_seen": last_seen,
            })
            continue

        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        avg_interval_days = round(mean(gaps), 1)
        if len(gaps) >= 2 and avg_interval_days > 0:
            interval_cv = round(stdev(gaps) / avg_interval_days, 2)
        else:
            interval_cv = None  # only one gap — no variance computable

        scored.append({
            "name": name,
            "category": category,
            "purchase_count": purchase_count,
            "avg_spend": avg_spend,
            "avg_interval_days": avg_interval_days,
            "interval_cv": interval_cv,
            "first_seen": first_seen,
            "last_seen": last_seen,
        })

    return scored, insufficient


def get_recurring_items(min_occurrences=3, max_interval_cv=0.5):
    """Items with enough purchase history, scored by how *regular* the
    purchase interval is — not just how often. See _compute_item_intervals
    for the interval/CV math."""
    scored, _ = _compute_item_intervals(min_occurrences)
    for item in scored:
        item["is_replenish_candidate"] = (
            item["interval_cv"] is not None and item["interval_cv"] <= max_interval_cv
        )
    scored.sort(key=lambda r: (not r["is_replenish_candidate"], -r["purchase_count"]))
    return scored


# Interval bands, in days. An item's avg_interval_days falls into exactly
# one band based on the closest match; CV still has to clear max_interval_cv
# or it goes to "irregular" instead of a cadence list, regardless of how
# well the average lines up.
INTERVAL_BANDS = [
    ("weekly", 5, 9),
    ("fortnightly", 10, 20),
    ("monthly", 21, 45),
]


def get_shopping_lists(min_occurrences=3, max_interval_cv=0.5):
    """Buckets every item with enough history into weekly / fortnightly /
    monthly ordering cadences, based on average purchase interval and
    regularity (CV). No category is excluded — fresh produce/meat stay in
    the dataset since the in-store-vs-online call is made at order time,
    not baked into the list logic.

    Returns:
      weekly       — band-matched items, band-appropriate for every list
      fortnightly  — weekly ∪ items matched to the fortnightly band
      monthly      — fortnightly ∪ items matched to the monthly band
      irregular    — enough purchase history, but CV too high or interval
                     falls outside all bands (e.g. >45 days) — surfaced
                     separately so nothing is silently dropped
      insufficient_data — fewer than min_occurrences purchases so far
    """
    scored, insufficient = _compute_item_intervals(min_occurrences)

    weekly, fortnightly_only, monthly_only, irregular = [], [], [], []

    for item in scored:
        cv = item["interval_cv"]
        avg = item["avg_interval_days"]
        is_regular = cv is not None and cv <= max_interval_cv

        band = None
        if is_regular:
            for band_name, lo, hi in INTERVAL_BANDS:
                if lo <= avg <= hi:
                    band = band_name
                    break

        if band == "weekly":
            weekly.append(item)
        elif band == "fortnightly":
            fortnightly_only.append(item)
        elif band == "monthly":
            monthly_only.append(item)
        else:
            # Either irregular (high CV) or outside all bands (e.g. avg
            # interval > 45 days, or < 5 days which is unusually frequent).
            irregular.append(item)

    def _sort(items):
        return sorted(items, key=lambda r: r["avg_interval_days"])

    weekly = _sort(weekly)
    fortnightly = _sort(weekly + fortnightly_only)
    monthly = _sort(fortnightly + monthly_only)
    irregular = sorted(irregular, key=lambda r: -r["purchase_count"])
    insufficient_data = sorted(insufficient, key=lambda r: -r["purchase_count"])

    return {
        "weekly": weekly,
        "fortnightly": fortnightly,
        "monthly": monthly,
        "irregular": irregular,
        "insufficient_data": insufficient_data,
    }


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
