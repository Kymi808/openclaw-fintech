"""
Receipt OCR pipeline using Anthropic Claude vision.
Extracts merchant, amount, date, payment method, and line items from receipt images.
"""
import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import httpx

from skills.shared import get_logger, retry, RetryExhausted

logger = get_logger("receipt_ocr")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

EXTRACTION_PROMPT = """Analyze this receipt image and extract the following information as valid JSON.
Be precise with numbers — do not round. If a field is not visible, use null.

Return ONLY a JSON object with these fields:
{
  "merchant": "Store or business name",
  "amount": 0.00,
  "currency": "USD",
  "date": "YYYY-MM-DD",
  "payment_method": "Visa ***1234 or Cash or null",
  "tax": 0.00,
  "subtotal": 0.00,
  "items": [
    {"name": "Item description", "qty": 1, "price": 0.00}
  ]
}

If the image is not a receipt, return: {"error": "Not a receipt"}
Return ONLY valid JSON, no other text."""


@dataclass
class ReceiptData:
    merchant: str
    amount: float
    currency: str
    date: Optional[str]
    payment_method: Optional[str]
    tax: Optional[float]
    subtotal: Optional[float]
    items: list[dict]
    raw_response: str
    model_used: str
    confidence: float  # 0.0 to 1.0


class ReceiptOCR:
    """Extract structured data from receipt images using Claude vision."""

    def __init__(self, api_key: str = None, model: str = None):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model or ANTHROPIC_MODEL

    def _encode_image(self, image_path: str) -> str:
        """Read and base64-encode an image file."""
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        # Validate file type
        suffix = path.suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            raise ValueError(f"Unsupported image format: {suffix}")

        # Check file size (max 20MB)
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > 20:
            raise ValueError(f"Image too large: {size_mb:.1f}MB (max 20MB)")

        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    def _get_media_type(self, image_path: str) -> str:
        """Get the MIME type for an image file."""
        suffix = Path(image_path).suffix.lower()
        return {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp", ".bmp": "image/bmp",
        }.get(suffix, "image/jpeg")

    @retry(max_attempts=2, base_delay=2.0, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
    async def extract(self, image_path: str) -> ReceiptData:
        """Extract receipt data from an image file using Claude vision."""
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. "
                "Set it in gateway/.env or as an environment variable."
            )

        image_b64 = self._encode_image(image_path)
        media_type = self._get_media_type(image_path)

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 1024,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": image_b64,
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": EXTRACTION_PROMPT,
                                },
                            ],
                        }
                    ],
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            raw_response = resp.json()["content"][0]["text"]

        return self._parse_response(raw_response, self.model)

    def _parse_response(self, raw: str, model: str) -> ReceiptData:
        """Parse the LLM response into structured ReceiptData."""
        # Try to extract JSON from the response
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            raise ValueError(f"No JSON found in model response: {raw[:200]}")

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in model response: {e}")

        if "error" in data:
            raise ValueError(f"Not a receipt: {data['error']}")

        # Validate and clean
        merchant = str(data.get("merchant", "Unknown")).strip()
        amount = self._parse_amount(data.get("amount"))
        currency = str(data.get("currency", "USD")).upper()
        date = self._parse_date(data.get("date"))
        payment = data.get("payment_method")
        tax = self._parse_amount(data.get("tax"))
        subtotal = self._parse_amount(data.get("subtotal"))
        items = data.get("items", [])

        # Confidence scoring
        confidence = self._calculate_confidence(merchant, amount, date, items)

        return ReceiptData(
            merchant=merchant,
            amount=amount,
            currency=currency,
            date=date,
            payment_method=str(payment) if payment else None,
            tax=tax,
            subtotal=subtotal,
            items=items if isinstance(items, list) else [],
            raw_response=raw,
            model_used=model,
            confidence=confidence,
        )

    @staticmethod
    def _parse_amount(value) -> Optional[float]:
        """Parse an amount from various formats."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return round(float(value), 2)
        if isinstance(value, str):
            # Remove currency symbols and commas
            cleaned = re.sub(r'[^0-9.\-]', '', value)
            try:
                return round(float(cleaned), 2)
            except ValueError:
                return None
        return None

    @staticmethod
    def _parse_date(value) -> Optional[str]:
        """Parse and normalize a date string to YYYY-MM-DD."""
        if not value:
            return None
        if isinstance(value, str):
            # Already in ISO format?
            if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
                return value
            # Try common formats
            from datetime import datetime
            for fmt in [
                "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y",
                "%B %d, %Y", "%b %d, %Y", "%Y/%m/%d",
                "%m-%d-%Y", "%d-%m-%Y",
            ]:
                try:
                    dt = datetime.strptime(value.strip(), fmt)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue
        return str(value)

    @staticmethod
    def _calculate_confidence(
        merchant: str, amount: Optional[float],
        date: Optional[str], items: list
    ) -> float:
        """Estimate extraction confidence (0.0 - 1.0)."""
        score = 0.0

        if merchant and merchant != "Unknown":
            score += 0.3
        if amount is not None and amount > 0:
            score += 0.3
        if date:
            score += 0.2
        if items and len(items) > 0:
            score += 0.2

        return round(score, 2)


# Singleton
receipt_ocr = ReceiptOCR()

# Backwards-compatible alias
OLLAMA_URL = None  # No longer used — kept only for import compatibility
