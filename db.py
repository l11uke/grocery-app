import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "grocery.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"
GENERIC_RULES_PATH = Path(__file__).parent / "generic_items.json"

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
    _ensure_item_aliases_table()


def _ensure_item_aliases_table():
    """Manual override table: raw receipt item name -> a name you've
    decided it should be grouped under. Checked FIRST in the grouping
    pipeline, ahead of both the generic rules and fuzzy matching, since
    a human decision should always win over a heuristic. Exists mainly
    for cases neither automated layer can handle — retailer-truncated
    codes like 'P-ITALN GRATE PARMSN' that aren't similar-looking text
    and don't share a clean keyword with their full name.

    Uses CREATE TABLE IF NOT EXISTS and runs standalone (not just via
    init_db) so it's safe to add on an existing deployed database
    without a full migration step.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS item_aliases (
            raw_name TEXT PRIMARY KEY,
            target_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


_ensure_item_aliases_table()


def get_item_aliases():
    conn = get_conn()
    rows = conn.execute(
        "SELECT raw_name, target_name, created_at FROM item_aliases ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_item_aliases(raw_names, target_name):
    """Merges every name in raw_names under target_name. Overwrites any
    existing alias for a given raw_name — the most recent manual
    decision wins."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    for raw_name in raw_names:
        conn.execute(
            "INSERT INTO item_aliases (raw_name, target_name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(raw_name) DO UPDATE SET target_name=excluded.target_name, created_at=excluded.created_at",
            (raw_name, target_name, now),
        )
    conn.commit()
    conn.close()


def remove_item_alias(raw_name):
    conn = get_conn()
    conn.execute("DELETE FROM item_aliases WHERE raw_name = ?", (raw_name,))
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


def _normalize_name(raw: str) -> str:
    """Layer 1: mechanical normalization. Lowercases, strips receipt-
    specific parenthetical detail (e.g. '(0.591kg @ $4.50/kg)' — that's
    quantity/price noise, not part of the product's identity), strips
    punctuation, and collapses common unit-notation variants (225GRAM ->
    225g, 1LITRE -> 1l, PERKG -> per kg) so pure formatting/OCR-casing
    differences don't fragment the same product into separate items."""
    import re
    s = raw.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"(\d+)\s*gram(s)?\b", r"\1g", s)
    s = re.sub(r"(\d+)\s*kilogram(s)?\b", r"\1kg", s)
    s = re.sub(r"(\d+)\s*litre(s)?\b", r"\1l", s)
    s = re.sub(r"(\d+)\s*millilitre(s)?\b", r"\1ml", s)
    s = re.sub(r"\bperkg\b", "per kg", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_SIZE_RE = None


def _tokenize(normalized: str) -> set:
    """Splits into words, with light stemming (strip trailing 's' on
    words over 3 chars) so 'cracker'/'crackers' don't count as a
    mismatch."""
    tokens = set()
    for tok in normalized.split():
        if len(tok) > 3 and tok.endswith("s") and not tok[-2].isdigit():
            tok = tok[:-1]
        tokens.add(tok)
    return tokens


def _extract_size_token(tokens: set):
    """Pulls out a pack-size token (e.g. '225g', '1l', '500ml') if
    present. Used as a hard constraint: two names with DIFFERENT sizes
    never merge, even if everything else matches — a 1L and 2L bottle
    are genuinely different purchases, and collapsing them would corrupt
    the per-item price data roadmap item 1 depends on."""
    import re
    for tok in tokens:
        if re.fullmatch(r"\d+(\.\d+)?(g|kg|l|ml|pack|pk|each)", tok):
            return tok
    return None


def _cluster_names_by_category(name_category_counts):
    """Layer 2: fuzzy-merges name variants within the same category using
    token overlap (Jaccard similarity), gated by the size constraint
    above. Returns {(raw_name, category): canonical_name}.

    This catches word-order and phrasing differences (e.g. 'Coles Sour
    Cream CTN 300g' vs 'Coles Cream Sour Carton 300g') that survive
    normalization. It does NOT catch retailer-truncated abbreviations
    (e.g. 'P-ITALN' for 'Perfect Italiano', 'CLS' for 'Coles') — those
    aren't similar strings, they're abbreviations, and need a separate
    alias-table approach rather than similarity scoring.

    Threshold is set at 0.6 deliberately conservative: high enough to
    avoid merging genuinely different products that happen to share a
    brand and size (e.g. 'grated' vs 'shaved' parmesan), low enough to
    catch real formatting variance. Expect some names that should merge
    to be missed, and treat that as an OK tradeoff over false merges.
    """
    from collections import defaultdict

    by_category = defaultdict(list)
    for (name, category), count in name_category_counts.items():
        by_category[category].append(name)

    canonical_map = {}
    JACCARD_THRESHOLD = 0.6

    for category, names in by_category.items():
        norm = {n: _normalize_name(n) for n in names}
        tokens = {n: _tokenize(norm[n]) for n in names}
        sizes = {n: _extract_size_token(tokens[n]) for n in names}

        parent = {n: n for n in names}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                if sizes[a] and sizes[b] and sizes[a] != sizes[b]:
                    continue  # different pack sizes: never merge
                ta, tb = tokens[a], tokens[b]
                if not ta or not tb:
                    continue
                overlap = len(ta & tb)
                jaccard = overlap / len(ta | tb)
                # Containment: how much of the SMALLER token set is
                # covered by the larger one. Handles cases like
                # 'exlean' (1 token) vs 'extra lean' (2 tokens) — the
                # real words match, but Jaccard penalizes the mismatched
                # token count. Require overlap >= 2 so a single shared
                # word (e.g. just the brand) never triggers this alone.
                containment = overlap / min(len(ta), len(tb))
                is_match = jaccard >= JACCARD_THRESHOLD or (
                    overlap >= 2 and containment >= 0.75
                )
                if is_match:
                    union(a, b)

        clusters = defaultdict(list)
        for n in names:
            clusters[find(n)].append(n)

        for cluster_names in clusters.values():
            # Canonical display name: whichever raw spelling has the most
            # purchases behind it (most representative), tie-broken by
            # longest string (usually the least-abbreviated version).
            canonical = max(
                cluster_names,
                key=lambda n: (name_category_counts[(n, category)], len(n)),
            )
            for n in cluster_names:
                canonical_map[(n, category)] = canonical

    return canonical_map


_generic_rules_cache = None


def _load_generic_rules():
    """Loads generic_items.json, cached for the process lifetime. Returns
    an empty rule list if the file is missing rather than crashing — the
    app should still work with SKU-level fallback matching only."""
    global _generic_rules_cache
    if _generic_rules_cache is not None:
        return _generic_rules_cache
    import json
    try:
        with open(GENERIC_RULES_PATH) as f:
            data = json.load(f)
        _generic_rules_cache = data.get("rules", [])
    except (FileNotFoundError, json.JSONDecodeError):
        _generic_rules_cache = []
    return _generic_rules_cache


def _match_generic_name(raw_name: str):
    """Checks a raw item name against the curated generic-item rules
    (generic_items.json). Matching is deliberately brand- and size-blind
    — 'Coles Tasty Cheese 1kg' and 'Mainland Cheese 500g' should both
    become 'Cheese', since that's what actually goes on a shopping list.
    Returns the generic_name string on match, or None if nothing in the
    rule list covers this item yet (falls back to SKU-level matching).

    Keywords match as WHOLE WORDS OR WORD PREFIXES — a boundary is
    required before the keyword, but not after. This blocks the failure
    mode where a short keyword accidentally matches mid-word (e.g. 'mint'
    inside 'peppermint' — there's no boundary between 'pepper' and
    'mint', so it's correctly rejected), while still allowing deliberate
    prefix-style keywords that catch OCR truncation or pluralization
    (e.g. 'arrowr' matching both 'arrowroot' and the truncated 'arrowro';
    'strawberr' matching 'strawberry' and 'strawberries').
    """
    import re
    s = _normalize_name(raw_name)

    def contains_word(keyword: str) -> bool:
        return re.search(r"\b" + re.escape(keyword), s) is not None

    for rule in _load_generic_rules():
        if "all" in rule:
            # 'all' is a list of synonym-groups: each group is itself a
            # list of acceptable alternate spellings (OR within a group),
            # and every group must have at least one hit (AND across
            # groups). E.g. [["chicken","chkn"], ["thigh"]] matches both
            # 'Chicken Thigh' and the OCR-abbreviated 'Chkn Thigh'.
            if not all(any(contains_word(alt) for alt in group) for group in rule["all"]):
                continue
        if "any" in rule and not any(contains_word(kw) for kw in rule["any"]):
            continue
        if "none" in rule and any(contains_word(kw) for kw in rule["none"]):
            continue
        if "all" not in rule and "any" not in rule:
            continue  # malformed rule, skip rather than false-match everything
        return rule["generic_name"]
    return None


def _compute_item_intervals(min_occurrences=3):
    """Shared helper: pulls every item purchase, groups it into a
    shopping-list-appropriate item identity, computes purchase intervals
    and CV per group, and splits into 'scored' (enough data to judge
    regularity) vs 'insufficient' (not enough purchases yet).

    Grouping happens in two layers:
      1. Generic-item rules (generic_items.json) — brand- and size-blind,
         curated by hand. 'Coles Tasty Cheese 1kg' and 'Mainland Cheese
         500g' both become 'Cheese'. This is the primary path, since a
         shopping list is written in terms of 'cheese', not SKUs.
      2. SKU-level fuzzy matching (_cluster_names_by_category) — fallback
         for anything the generic rules don't cover yet. Conservative:
         merges formatting/OCR variance of the same product, but keeps
         different pack sizes and different products separate.

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

    # Tier 0: manual aliases — a human decision, always wins outright.
    alias_map = {a["raw_name"]: a["target_name"] for a in get_item_aliases()}

    # Tier 1: generic-rule matching, brand/size-blind, ignores category.
    # Only runs on names NOT already covered by a manual alias.
    generic_for_raw = {}
    unmatched_rows = []
    for row in rows:
        if row["name"] in alias_map:
            continue
        generic = _match_generic_name(row["name"])
        if generic:
            generic_for_raw[row["name"]] = generic
        else:
            unmatched_rows.append(row)

    # Tier 2: SKU-level fuzzy fallback, only for what tiers 0 and 1 missed.
    raw_counts = defaultdict(int)
    for row in unmatched_rows:
        raw_counts[(row["name"], row["category"])] += 1
    canonical_map = _cluster_names_by_category(raw_counts)

    # Build final grouping key per row. Alias and generic matches group
    # purely by their target/generic name (category is recomputed as the
    # most common category among the merged rows, since brand-specific
    # extraction sometimes disagrees on category for the same item).
    grouped = defaultdict(list)
    merged_categories = defaultdict(lambda: defaultdict(int))
    for row in rows:
        if row["name"] in alias_map:
            key = ("__alias__", alias_map[row["name"]])
            merged_categories[key][row["category"]] += 1
        elif row["name"] in generic_for_raw:
            key = ("__generic__", generic_for_raw[row["name"]])
            merged_categories[key][row["category"]] += 1
        else:
            canonical_name = canonical_map[(row["name"], row["category"])]
            key = (canonical_name, row["category"])
        grouped[key].append(row)

    scored, insufficient = [], []
    for key, item_rows in grouped.items():
        if key[0] in ("__alias__", "__generic__"):
            name = key[1]
            category = max(merged_categories[key].items(), key=lambda kv: kv[1])[0]
        else:
            name, category = key
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
        # Underlying raw item-name(s) that were merged into this group.
        # Exposed so the UI can offer aliasing on the exact strings
        # stored in items.name, not the display name shown here.
        raw_names = sorted({r["name"] for r in item_rows})

        if purchase_count < min_occurrences:
            insufficient.append({
                "name": name,
                "category": category,
                "purchase_count": purchase_count,
                "avg_spend": avg_spend,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "raw_names": raw_names,
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
            "raw_names": raw_names,
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
