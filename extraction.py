import base64
import json
import os
import re

from anthropic import Anthropic, NotFoundError
from db import CATEGORIES

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Ordered by preference. First one that the /v1/models endpoint confirms
# exists for this API key wins. Update this list when Anthropic ships new
# generations — don't just swap the top value blindly, since dateless IDs
# are permanent and older ones don't silently upgrade.
MODEL_CANDIDATES = [
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-3-7-sonnet-latest",
]

_resolved_model = None


def resolve_model() -> str:
    """Pick the first available model from MODEL_CANDIDATES, checked once
    per process and cached. Raises if none of them are available."""
    global _resolved_model
    if _resolved_model:
        return _resolved_model

    try:
        available_ids = {m.id for m in client.models.list()}
    except Exception:
        # Can't reach the models endpoint — fall back to the top candidate
        # and let the actual extraction call surface any real failure.
        _resolved_model = MODEL_CANDIDATES[0]
        return _resolved_model

    for candidate in MODEL_CANDIDATES:
        if candidate in available_ids:
            _resolved_model = candidate
            return _resolved_model

    raise RuntimeError(
        f"None of the candidate models {MODEL_CANDIDATES} are available "
        f"to this API key. Available: {sorted(available_ids)}"
    )

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


def _call_extraction(model: str, b64: str, media_type: str):
    return client.messages.create(
        model=model,
        max_tokens=8192,
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


def extract_receipt(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    model = resolve_model()
    try:
        response = _call_extraction(model, b64, media_type)
    except NotFoundError:
        # Cached model got deprecated mid-run (rare, but happens on long
        # uptimes). Drop it, re-resolve, retry once.
        global _resolved_model
        _resolved_model = None
        model = resolve_model()
        response = _call_extraction(model, b64, media_type)

    if response.stop_reason == "max_tokens":
        # The model ran out of output budget mid-response — the JSON is
        # guaranteed incomplete. Fail with a clear reason instead of
        # letting json.loads() throw an "unterminated string" error that
        # gives no hint about what actually went wrong.
        raise ValueError(
            "Extraction failed: the receipt is long enough that the "
            "model's response was cut off before finishing (hit the "
            "max_tokens limit). Try again, or increase max_tokens in "
            "extraction.py if this keeps happening on large receipts."
        )

    text = "".join(block.text for block in response.content if block.type == "text")
    text = re.sub(r"^```json|```$", "", text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(text)

    # Guard against category hallucination
    for item in data.get("items", []):
        if item.get("category") not in CATEGORIES:
            item["category"] = "Other"

    return data
