import base64
import json
import os
import re

from anthropic import Anthropic
from db import CATEGORIES

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-4-6"

PROMPT = f"""You are extracting structured data from a photo of a grocery receipt.

Return ONLY valid JSON, no markdown fences, no preamble. Use this exact shape:

{{
  "store": "string",
  "purchase_date": "YYYY-MM-DD",
  "printed_total": number or null,
  "items": [
    {{
      "name": "string",
      "category": "one of: {', '.join(CATEGORIES)}",
      "quantity": number,
      "unit_price": number or null,
      "line_total": number
    }}
  ]
}}

Rules:
- category MUST be one of the exact strings listed above.
- If a value is genuinely unreadable, use null rather than guessing.
- line_total is the actual amount charged for that line (after any in-line discount).
- Do not invent items. Extract every line item present.
"""


def extract_receipt(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    text = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(text)

    # Guard against category hallucination
    for item in data.get("items", []):
        if item.get("category") not in CATEGORIES:
            item["category"] = "Other"

    return data
