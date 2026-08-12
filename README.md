# Grocery Receipt Tracker (standalone, Pi-hosted)

Runs entirely on the Pi. Phone hits it directly over wifi at `http://<PI_IP>:5000`.
No chat, no external client, no dependency on this conversation continuing to exist.

`<PI_IP>` throughout this file is a placeholder — swap in your Pi's actual LAN IP
(e.g. `192.168.1.x`) wherever it appears. Kept out of this repo intentionally.

## Purpose

The idea: this app helps a household introduce online grocery shopping in a
more consistent, structured way, for situations where going to the store
regularly isn't possible or desirable.

Fresh produce and meat/seafood are treated as an in-store purchase, bought as
needed, since quality varies too much item-to-item to trust to an online order
sight-unseen. Packaged, interchangeable pantry and household items are the
target for moving online — but the app deliberately doesn't hard-exclude any
category. Fresh items stay in the dataset and can still appear on generated
lists; the in-store-vs-online call is made by hand at order time, not baked
into the logic.

The dataset — built from photographed receipts — exists to answer one practical
question: *what do we actually buy regularly enough to put on a recurring online
order, and how often?* That answer is surfaced as weekly / fortnightly / monthly
shopping lists (see below), not just a spend report.

## What it does

- **Upload page** (`/`) — take/choose a receipt photo, Claude extracts items via
  the Anthropic API, you review/edit, confirm to save.
- **Insights page** (`/insights`) — category spend totals, plus recurring-item
  detection scored by purchase *regularity* (not just count — see "Interval
  scoring" below), computed live from SQLite on every request.
- **Shopping Lists page** (`/shopping-lists`) — weekly, fortnightly, and monthly
  cadence lists built from purchase history, plus an "irregular" bucket (enough
  history, but timing too inconsistent to trust) and a "not enough data yet"
  bucket, so nothing is silently dropped.
- Data lives in `grocery.db` (SQLite) on the Pi. No dependency on Excel.

## History

This started as a chat-based pipeline: photograph a receipt, upload it to Claude
in conversation, Claude extracted structured JSON, rows were appended to an Excel
workbook (Items / Receipts / Insights sheets), and the updated file was returned
each time. An earlier attempt before that — a React artifact making in-browser
calls to the Anthropic API — failed outright due to artifact sandbox limitations
and mobile tooling constraints.

The chat+Excel approach worked but had two structural ceilings:

- **No live queries.** Excel can't do the kind of dynamic unique-item extraction
  this needed without array functions that weren't practical in that environment,
  so "recurring items" was a Python-generated snapshot, regenerated per batch —
  not a query you could just re-run as data grew.
- **Manual round-trip per batch.** Every update meant re-uploading the current
  file alongside new photos and waiting for Claude to return a new one. That
  doesn't scale as a daily habit.

This repo replaced that entirely: a standalone Flask app running as a systemd
service on a Raspberry Pi, backed by SQLite, reachable from a phone over LAN.
Chat is no longer part of the pipeline — it was only ever needed for vision
extraction, which now happens as a direct Anthropic API call from the Pi itself.
Recurring-item detection became a live SQL/Python query instead of a manual
snapshot, which is what made the shopping-lists feature (below) practical in the
first place.

### Interval scoring (what changed from a simple purchase count)

The first version of recurring-item detection flagged anything bought 3+ times,
with no sense of timing — "bought 3 times in 3 weeks" and "bought 3 times over
3 years" scored identically. That's not useful for an auto-replenish decision.

The current approach computes, per item, the gaps in days between consecutive
purchases, then the coefficient of variation (CV = stddev / mean) of those gaps.
Low CV means the purchase interval is consistent (a good candidate for a
recurring order); high CV means it's sporadic regardless of how many times it's
shown up. Items are then bucketed into weekly (~5–9 day average interval),
fortnightly (~10–20 days), or monthly (~21–45 days) bands — but only if CV stays
under the regularity threshold. Anything with enough history but inconsistent
timing lands in a separate "irregular" list instead of forcing itself into a
band it doesn't really belong to.

### Model selection

The extraction call resolves its model against the live `/v1/models` API at
startup rather than trusting a single hardcoded string, and falls back through
an ordered candidate list if the preferred model isn't available to the API key
— caching whichever one resolves so it isn't re-checked on every request. This
is a deliberate hedge against Anthropic shipping new model generations without
requiring a manual code change every time.

## Deploy (on the Pi, via SSH or Claude Code)

```bash
# 1. Get the code onto the Pi
git clone https://github.com/l11uke/grocery-app.git
cd grocery-app

# 2. Set up Python environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Add your API key
cp .env.example .env
nano .env   # paste your real ANTHROPIC_API_KEY

# 4. Test it manually first
export $(cat .env | xargs)
python app.py
# Visit http://<PI_IP>:5000 from your phone on the same wifi — confirm it works.
# Ctrl+C to stop once confirmed.

# 5. Install as a persistent service (survives reboots, restarts on crash)
sudo cp grocery-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable grocery-app
sudo systemctl start grocery-app

# 6. Check it's running
sudo systemctl status grocery-app
```

## Updating the deployed app

The Pi should be a `git clone`, not a one-off `scp` copy, so updates are a pull:

```bash
cd ~/grocery-app
git pull origin main
sudo systemctl restart grocery-app
```

Confirm the relevant routes are alive after any update:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/insights
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5000/shopping-lists
```

Both should return `200`.

## Using it day to day

1. On your phone, on your home wifi, visit `http://<PI_IP>:5000`.
2. Tap "Take / Choose Photo", photograph the receipt.
3. Wait a few seconds for extraction, review the parsed items (edit any mistakes inline), confirm the store/date/total.
4. Tap "Save Receipt".
5. Visit `/insights` any time to see category totals and recurring items.
6. Visit `/shopping-lists` to see the current weekly / fortnightly / monthly
   cadence lists, plus irregular and not-enough-data items.

No wifi = no access, since it's LAN-only. That's a deliberate tradeoff — nothing here is exposed to the internet.

## Migrating existing data

If you have prior receipts logged in a spreadsheet, carry them over by either:
- Manually insert it via a quick Python script using `db.save_receipt(...)`, or
- Just re-photograph the printed receipt through the new app if you kept it.

## Dev roadmap

1. **Unit price normalisation and deviation flagging.** Line totals currently get
   compared across purchases of "the same" item without accounting for pack size —
   a 1L vs 2L bottle of the same product will show up as a price swing that isn't
   real. This needs unit price (price per L/kg/unit) computed and stored
   consistently, plus a flag when the *genuine* unit price on a repeat purchase
   deviates meaningfully from the item's own history — surfacing real price
   creep or a good time to stock up, instead of noise from pack-size differences
   getting silently absorbed into `avg_spend`.

2. **Name matching improvement opportunities.** Recurring-item and shopping-list
   grouping currently relies on exact string match (`GROUP BY name, category`),
   so OCR variance or inconsistent phrasing across visits — "Coles Full Cream
   Milk 2L" one week, "CLS Milk 2L" or a slightly different OCR read the next —
   fragments what should be one recurring item into several, undercounting
   purchase frequency and breaking the interval-regularity scoring that the
   shopping lists depend on. Needs some form of fuzzy matching or canonicalisation
   (e.g. normalising known store abbreviations, a similarity threshold on name
   strings, or a manual alias table) so the same real-world product is recognised
   as the same item regardless of how the receipt happened to print or how Claude
   happened to transcribe it.
