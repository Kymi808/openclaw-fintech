"""Tests for the GDPR compliance scanner."""
import pytest
import httpx
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_gdpr_scan_detects_missing_cookie_consent():
    """Test that scanner detects missing cookie consent banner."""
    from skills.legal.handlers import gdpr_scan

    mock_html = """
    <html>
    <head><title>Test Site</title></head>
    <body>
        <form action="http://insecure.example.com/submit">
            <input type="text" name="email">
        </form>
        <script src="https://www.google-analytics.com/analytics.js"></script>
    </body>
    </html>
    """

    mock_response = AsyncMock()
    mock_response.text = mock_html
    mock_response.headers = {}
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await gdpr_scan("https://test.example.com")

    assert result["issue_count"] > 0
    issues = result["issues"]
    issue_types = [i["issue"] for i in issues]

    # Should detect missing cookie consent
    assert any("cookie" in i.lower() for i in issue_types)
    # Should detect unencrypted form
    assert any("form" in i.lower() or "unencrypted" in i.lower() for i in issue_types)
    # Should detect trackers without consent
    assert any("tracker" in i.lower() for i in issue_types)


@pytest.mark.asyncio
async def test_gdpr_scan_clean_site():
    """Test that a compliant site gets fewer issues."""
    from skills.legal.handlers import gdpr_scan

    mock_html = """
    <html>
    <head><title>Compliant Site</title></head>
    <body>
        <div id="cookie-consent">We use cookies...</div>
        <a href="/privacy-policy">Privacy Policy</a>
        <form action="https://secure.example.com/submit">
            <input type="email">
        </form>
    </body>
    </html>
    """

    mock_response = AsyncMock()
    mock_response.text = mock_html
    mock_response.headers = {
        "strict-transport-security": "max-age=31536000",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "content-security-policy": "default-src 'self'",
    }
    mock_response.status_code = 200
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        result = await gdpr_scan("https://compliant.example.com")

    # Should have no HIGH severity issues
    high_issues = [i for i in result["issues"] if i["severity"] == "HIGH"]
    assert len(high_issues) == 0
