# Grocery Receipt Tracker (standalone, Pi-hosted)

Runs entirely on the Pi. Phone hits it directly over wifi at `http://<PI_IP>:5000`.
No chat, no external client, no dependency on this conversation continuing to exist.

`<PI_IP>` throughout this file is a placeholder — swap in your Pi's actual LAN IP
(e.g. `192.168.1.x`) wherever it appears. Kept out of this repo intentionally.

## What it does

- Upload page: take/choose a receipt photo, Claude Sonnet extracts items, you review/edit, confirm to save.
- Insights page: category spend totals, recurring-item detection (items bought 3+ times = subscription candidates), computed live from SQLite on every request — not a manual snapshot.
- Data lives in `grocery.db` (SQLite) on the Pi. No dependency on Excel going forward.

## Deploy (on the Pi, via SSH or Claude Code)

```bash
# 1. Get the code onto the Pi
scp -r grocery-app pi@<PI_IP>:/home/pi/
ssh pi@<PI_IP>

# 2. Set up Python environment
cd /home/pi/grocery-app
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

## Using it day to day

1. On your phone, on your home wifi, visit `http://<PI_IP>:5000`.
2. Tap "Take / Choose Photo", photograph the receipt.
3. Wait a few seconds for extraction, review the parsed items (edit any mistakes inline), confirm the store/date/total.
4. Tap "Save Receipt".
5. Visit `/insights` any time to see category totals and recurring items.

No wifi = no access, since it's LAN-only. That's a deliberate tradeoff — nothing here is exposed to the internet.

## Migrating existing data

If you have prior receipts logged in a spreadsheet, carry them over by either:
- Manually insert it via a quick Python script using `db.save_receipt(...)`, or
- Just re-photograph the printed receipt through the new app if you kept it.

## Notes on what changed from the old design

- Chat is no longer part of the pipeline. It was only ever needed for vision extraction, which now happens as a direct API call from the Pi itself.
- Recurring-items detection is now a live SQL query (`db.get_recurring_items`), not a Python snapshot you had to regenerate per batch — this was previously listed as "not practical," but it's straightforward once the data lives in a real database instead of Excel.
- Category consistency is enforced by a fixed category list (`db.py: CATEGORIES`) passed into every extraction prompt and validated on save — anything the model mislabels falls back to "Other" instead of silently fragmenting your category totals.
