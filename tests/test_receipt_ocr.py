"""Tests for receipt OCR pipeline."""
import pytest
from skills.finance.receipt_ocr import ReceiptOCR


class TestReceiptParsing:
    def setup_method(self):
        self.ocr = ReceiptOCR()

    def test_parse_valid_json_response(self):
        raw = '''{"merchant": "Starbucks", "amount": 5.75, "currency": "USD",
                  "date": "2026-03-16", "payment_method": "Visa ***1234",
                  "tax": 0.45, "subtotal": 5.30, "items": [
                    {"name": "Latte", "qty": 1, "price": 5.30}
                  ]}'''
        result = self.ocr._parse_response(raw, "llava:13b")
        assert result.merchant == "Starbucks"
        assert result.amount == 5.75
        assert result.date == "2026-03-16"
        assert result.payment_method == "Visa ***1234"
        assert result.confidence == 1.0
        assert result.model_used == "llava:13b"

    def test_parse_json_with_surrounding_text(self):
        raw = 'Here is the extracted data:\n{"merchant": "Target", "amount": 42.99, "date": "03/15/2026"}\nDone.'
        result = self.ocr._parse_response(raw, "llava")
        assert result.merchant == "Target"
        assert result.amount == 42.99
        assert result.date == "2026-03-15"

    def test_parse_amount_with_currency_symbol(self):
        assert ReceiptOCR._parse_amount("$12.99") == 12.99
        assert ReceiptOCR._parse_amount("€42.00") == 42.0
        assert ReceiptOCR._parse_amount("1,234.56") == 1234.56
        assert ReceiptOCR._parse_amount(None) is None
        assert ReceiptOCR._parse_amount(10) == 10.0

    def test_parse_date_formats(self):
        assert ReceiptOCR._parse_date("2026-03-16") == "2026-03-16"
        assert ReceiptOCR._parse_date("03/16/2026") == "2026-03-16"
        assert ReceiptOCR._parse_date("3/16/26") == "2026-03-16"
        assert ReceiptOCR._parse_date(None) is None

    def test_confidence_scoring(self):
        # All fields present
        assert ReceiptOCR._calculate_confidence("Starbucks", 5.75, "2026-03-16", [{"name": "latte"}]) == 1.0
        # Missing items
        assert ReceiptOCR._calculate_confidence("Starbucks", 5.75, "2026-03-16", []) == 0.8
        # Missing date and items
        assert ReceiptOCR._calculate_confidence("Starbucks", 5.75, None, []) == 0.6
        # Only merchant
        assert ReceiptOCR._calculate_confidence("Starbucks", None, None, []) == 0.3
        # Nothing
        assert ReceiptOCR._calculate_confidence("Unknown", None, None, []) == 0.0

    def test_parse_error_response(self):
        raw = '{"error": "Not a receipt"}'
        with pytest.raises(ValueError, match="Not a receipt"):
            self.ocr._parse_response(raw, "llava")

    def test_parse_no_json(self):
        raw = "I cannot read this image clearly."
        with pytest.raises(ValueError, match="No JSON found"):
            self.ocr._parse_response(raw, "llava")

    def test_encode_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            self.ocr._encode_image("/nonexistent/receipt.jpg")

    def test_encode_unsupported_format(self, tmp_path):
        bad_file = tmp_path / "receipt.txt"
        bad_file.write_text("not an image")
        with pytest.raises(ValueError, match="Unsupported"):
            self.ocr._encode_image(str(bad_file))
