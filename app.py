import os

from flask import Flask, request, jsonify, render_template

import db
from extraction import extract_receipt

app = Flask(__name__)
db.init_db()


@app.route("/")
def index():
    return render_template("upload.html")


@app.route("/insights")
def insights():
    return render_template("insights.html")


@app.route("/api/extract", methods=["POST"])
def api_extract():
    """Takes an uploaded photo, returns extracted JSON for the user to review (not yet saved)."""
    if "photo" not in request.files:
        return jsonify({"error": "no photo uploaded"}), 400

    photo = request.files["photo"]
    media_type = photo.mimetype or "image/jpeg"
    image_bytes = photo.read()

    try:
        data = extract_receipt(image_bytes, media_type)
    except Exception as e:
        return jsonify({"error": f"extraction failed: {e}"}), 500

    return jsonify(data)


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    """Takes reviewed/edited receipt JSON and saves it to the database."""
    data = request.get_json(force=True)

    store = data.get("store", "Unknown")
    purchase_date = data.get("purchase_date")
    printed_total = data.get("printed_total")
    items = data.get("items", [])

    if not items:
        return jsonify({"error": "no items to save"}), 400

    for item in items:
        if item.get("category") not in db.CATEGORIES:
            item["category"] = "Other"

    receipt_id, computed_total, mismatch = db.save_receipt(store, purchase_date, printed_total, items)

    return jsonify({
        "receipt_id": receipt_id,
        "computed_total": computed_total,
        "total_mismatch": bool(mismatch),
    })


@app.route("/shopping-lists")
def shopping_lists():
    return render_template("shopping_lists.html")


@app.route("/api/shopping-lists")
def api_shopping_lists():
    return jsonify(db.get_shopping_lists())


@app.route("/api/insights")
def api_insights():
    return jsonify({
        "category_totals": db.get_category_totals(),
        "recurring_items": db.get_recurring_items(min_occurrences=3),
    })


@app.route("/api/receipts")
def api_receipts():
    return jsonify(db.get_all_receipts())


@app.route("/api/receipts/<int:receipt_id>/items")
def api_receipt_items(receipt_id):
    return jsonify(db.get_receipt_items(receipt_id))


@app.route("/api/categories")
def api_categories():
    return jsonify(db.CATEGORIES)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
