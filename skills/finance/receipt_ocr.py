"""
Receipt OCR pipeline using Ollama vision models.
Extracts merchant, amount, date, payment method, and line items from receipt images.
"""
import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import httpx

from skills.shared import get_logger, retry, RetryExhausted

logger = get_logger("receipt_ocr")

OLLAMA_URL = "http://localhost:11434"

# Supported vision models in order of preference
VISION_MODELS = ["llava:13b", "llava:7b", "llava", "bakllava"]

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
    """Extract structured data from receipt images using vision LLMs."""

    def __init__(self, ollama_url: str = None):
        self.ollama_url = ollama_url or OLLAMA_URL

    async def _get_available_model(self) -> Optional[str]:
        """Find the first available vision model."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.ollama_url}/api/tags")
                resp.raise_for_status()
                available = {m["name"] for m in resp.json().get("models", [])}

                for model in VISION_MODELS:
                    if model in available:
                        return model
                    # Check without tag
                    base = model.split(":")[0]
                    for avail in available:
                        if avail.startswith(base):
                            return avail
        except Exception as e:
            logger.error(f"Cannot connect to Ollama: {e}")
        return None

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

    @retry(max_attempts=2, base_delay=2.0, retryable_exceptions=(httpx.ConnectError, httpx.ReadTimeout))
    async def extract(self, image_path: str) -> ReceiptData:
        """Extract receipt data from an image file."""
        model = await self._get_available_model()
        if not model:
            raise RuntimeError(
                "No vision model available in Ollama. "
                "Install one with: ollama pull llava:13b"
            )

        image_b64 = self._encode_image(image_path)

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.ollama_url}/api/generate",
                json={
                    "model": model,
                    "prompt": EXTRACTION_PROMPT,
                    "images": [image_b64],
                    "stream": False,
                    "options": {
                        "temperature": 0.1,  # Low temp for precise extraction
                        "num_predict": 1024,
                    },
                },
            )
            resp.raise_for_status()
            raw_response = resp.json().get("response", "")

        return self._parse_response(raw_response, model)

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
