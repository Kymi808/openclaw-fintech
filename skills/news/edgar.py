"""
SEC EDGAR news gatherer — corporate filings and insider trading.

Free API, no key required (just a User-Agent header).

Tracks:
1. 8-K filings (material events: earnings, M&A, management changes, guidance)
2. Form 4 (insider buying/selling — strong predictive signal)
3. 10-K/10-Q (quarterly/annual reports)

Insider buying is one of the strongest predictive signals in equities.
Insiders buy for one reason: they think the stock will go up.
Insiders sell for many reasons (taxes, diversification), so sells are weaker.

Reference: Lakonishok & Lee (2001), "Are Insider Trades Informative?"
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from skills.shared import get_logger
from .gatherers import NewsSignal

logger = get_logger("news.edgar")

EDGAR_BASE = "https://efts.sec.gov/LATEST"
EDGAR_FULL_TEXT = "https://www.sec.gov/cgi-bin/browse-edgar"

# SEC requires a real User-Agent
USER_AGENT = os.getenv("SEC_EDGAR_USER_AGENT", "OpenClaw Trading Bot contact@example.com")

# CIK lookup for common tickers (SEC uses CIK numbers, not tickers)
# We fetch this dynamically, but cache common ones
TICKER_CIK_CACHE = {}


async def _get_cik(ticker: str) -> Optional[str]:
    """Look up SEC CIK number for a ticker symbol."""
    if ticker in TICKER_CIK_CACHE:
        return TICKER_CIK_CACHE[ticker]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://www.sec.gov/cgi-bin/browse-edgar",
                params={"action": "getcompany", "company": ticker,
                        "type": "", "dateb": "", "owner": "include",
                        "count": 1, "search_text": "", "CIK": ticker,
                        "output": "atom"},
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 200:
                # Parse CIK from response
                text = resp.text
                if "CIK=" in text:
                    cik = text.split("CIK=")[1].split("&")[0].split('"')[0]
                    TICKER_CIK_CACHE[ticker] = cik
                    return cik
    except Exception:
        pass
    return None


async def fetch_recent_filings(
    tickers: list[str] = None,
    filing_types: list[str] = None,
    days_back: int = 7,
) -> list[NewsSignal]:
    """
    Fetch recent SEC filings from EDGAR full-text search.

    Args:
        tickers: filter by these companies (None = all)
        filing_types: ["8-K", "4", "10-K", "10-Q"] (None = all material filings)
        days_back: how far back to search

    Returns:
        list of NewsSignal objects
    """
    if filing_types is None:
        filing_types = ["8-K", "4"]  # material events + insider trading

    signals = []
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for filing_type in filing_types:
                params = {
                    "q": "",
                    "dateRange": "custom",
                    "startdt": start_date,
                    "enddt": end_date,
                    "forms": filing_type,
                }

                # If specific tickers, search for each
                if tickers:
                    for ticker in tickers[:20]:  # limit to avoid rate limits
                        params["q"] = ticker
                        filing_signals = await _search_filings(client, params, filing_type, ticker)
                        signals.extend(filing_signals)
                else:
                    filing_signals = await _search_filings(client, params, filing_type)
                    signals.extend(filing_signals)

    except Exception as e:
        logger.warning(f"EDGAR fetch failed: {e}")

    logger.info(f"EDGAR: {len(signals)} filings found")
    return signals


async def _search_filings(
    client: httpx.AsyncClient,
    params: dict,
    filing_type: str,
    ticker: str = "",
) -> list[NewsSignal]:
    """Search EDGAR full-text search API."""
    signals = []

    try:
        resp = await client.get(
            f"{EDGAR_BASE}/search-index",
            params=params,
            headers={"User-Agent": USER_AGENT},
        )

        if resp.status_code != 200:
            return signals

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])

        for hit in hits[:10]:  # limit per query
            source = hit.get("_source", {})
            file_date = source.get("file_date", "")
            entity = source.get("entity_name", "")
            description = source.get("display_description", "")

            # Determine sentiment from filing type
            sentiment, urgency, subcategory = _classify_filing(filing_type, description)

            # Map entity to ticker symbols
            symbols = [ticker] if ticker else _extract_symbols(entity)

            signals.append(NewsSignal(
                headline=f"[SEC {filing_type}] {entity}: {description[:100]}",
                source="sec_edgar",
                symbols=symbols,
                category="company",
                subcategory=subcategory,
                sentiment=sentiment,
                relevance=0.8 if filing_type in ("8-K", "4") else 0.5,
                urgency=urgency,
                timestamp=file_date,
                summary=description[:200],
            ))

    except Exception as e:
        logger.debug(f"EDGAR search failed for {filing_type}: {e}")

    return signals


def _classify_filing(filing_type: str, description: str) -> tuple[float, str, str]:
    """Classify a filing's likely sentiment, urgency, and subcategory."""
    desc_lower = description.lower()

    if filing_type == "4":
        # Form 4: insider trading
        if "purchase" in desc_lower or "acquisition" in desc_lower:
            return 0.4, "important", "insider"  # insider buying = bullish
        elif "sale" in desc_lower or "disposition" in desc_lower:
            return -0.1, "routine", "insider"  # insider selling = mildly bearish
        return 0.0, "routine", "insider"

    if filing_type == "8-K":
        # 8-K: material events
        if any(w in desc_lower for w in ["bankruptcy", "default", "delisted"]):
            return -0.8, "breaking", "legal"
        if any(w in desc_lower for w in ["acquisition", "merger", "agreement"]):
            return 0.3, "breaking", "ma"
        if any(w in desc_lower for w in ["officer", "director", "appointed", "resigned"]):
            return 0.0, "important", "insider"
        if any(w in desc_lower for w in ["earnings", "results", "revenue"]):
            return 0.0, "breaking", "earnings"
        return 0.0, "important", "regulatory"

    if filing_type in ("10-K", "10-Q"):
        return 0.0, "routine", "earnings"

    return 0.0, "routine", "regulatory"


def _extract_symbols(entity_name: str) -> list[str]:
    """Best-effort extraction of ticker from entity name."""
    # This is approximate — SEC uses company names, not tickers
    # In production, use a CIK-to-ticker mapping
    return []


def _hours_since(date_str: str) -> float:
    """Hours since a date string."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 24.0
