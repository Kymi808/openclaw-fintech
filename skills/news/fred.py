"""
FRED (Federal Reserve Economic Data) — macro economic data releases.

Free API (requires free key from https://fred.stlouisfed.org/docs/api/api_key.html).
Falls back to hardcoded release schedule if no key.

Tracks high-impact economic releases:
- FOMC decisions (interest rates)
- CPI/PPI (inflation)
- Non-farm payrolls (jobs)
- GDP
- Retail sales
- Housing starts
- Consumer confidence
- ISM Manufacturing/Services

These releases move the entire market. A hot CPI print affects every stock.
The system needs to know WHEN these releases happen and what the surprise was.
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from skills.shared import get_logger
from .gatherers import NewsSignal

logger = get_logger("news.fred")

FRED_BASE = "https://api.stlouisfed.org/fred"

# High-impact economic indicators
MACRO_SERIES = {
    # Series ID: (name, impact_level, typical_frequency)
    "FEDFUNDS": ("Fed Funds Rate", "critical", "6 weeks"),
    "CPIAUCSL": ("CPI (Consumer Price Index)", "critical", "monthly"),
    "PPIFIS": ("PPI (Producer Price Index)", "high", "monthly"),
    "PAYEMS": ("Non-Farm Payrolls", "critical", "monthly"),
    "UNRATE": ("Unemployment Rate", "high", "monthly"),
    "GDP": ("GDP Growth Rate", "critical", "quarterly"),
    "RSXFS": ("Retail Sales", "high", "monthly"),
    "HOUST": ("Housing Starts", "medium", "monthly"),
    "UMCSENT": ("Consumer Sentiment", "medium", "monthly"),
    "MANEMP": ("Manufacturing Employment", "medium", "monthly"),
}

# Hardcoded upcoming release dates (updated manually or via FRED API)
# When FRED_API_KEY is available, these are fetched dynamically
KNOWN_RELEASE_SCHEDULE = {
    "FOMC": "Every 6 weeks (check federalreserve.gov)",
    "CPI": "~13th of each month",
    "Jobs Report": "First Friday of each month",
    "GDP": "Last week of month following quarter end",
}


def _get_fred_key() -> str:
    return os.getenv("FRED_API_KEY", "")


def is_fred_configured() -> bool:
    key = _get_fred_key()
    return bool(key) and key not in ("", "xxxxx")


async def fetch_macro_releases(days_back: int = 7) -> list[NewsSignal]:
    """
    Fetch recent economic data releases and their impact.

    If FRED_API_KEY is set: fetches actual data + surprise from FRED API.
    If not: returns upcoming schedule based on known release calendar.
    """
    if not is_fred_configured():
        return _get_calendar_signals()

    signals = []
    api_key = _get_fred_key()
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_date = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for series_id, (name, impact, freq) in MACRO_SERIES.items():
                try:
                    resp = await client.get(
                        f"{FRED_BASE}/series/observations",
                        params={
                            "series_id": series_id,
                            "api_key": api_key,
                            "file_type": "json",
                            "observation_start": start_date,
                            "observation_end": end_date,
                            "sort_order": "desc",
                            "limit": 2,
                        },
                    )

                    if resp.status_code != 200:
                        continue

                    data = resp.json()
                    observations = data.get("observations", [])

                    if len(observations) >= 2:
                        latest = observations[0]
                        previous = observations[1]

                        try:
                            latest_val = float(latest["value"])
                            prev_val = float(previous["value"])
                            change = latest_val - prev_val
                            change_pct = change / prev_val if prev_val != 0 else 0
                        except (ValueError, ZeroDivisionError):
                            continue

                        # Score sentiment based on what the data means for markets
                        sentiment = _score_macro_sentiment(series_id, change, change_pct)

                        urgency = "breaking" if impact == "critical" else "important"
                        age_hours = _hours_since(latest["date"])
                        if age_hours > 48:
                            urgency = "routine"

                        signals.append(NewsSignal(
                            headline=f"[FRED] {name}: {latest_val:.2f} (prev: {prev_val:.2f}, chg: {change:+.2f})",
                            source="fred",
                            symbols=["SPY"],  # macro affects all stocks
                            category="macro",
                            subcategory=_macro_subcategory(series_id),
                            sentiment=sentiment,
                            relevance=1.0 if impact == "critical" else 0.7,
                            urgency=urgency,
                            timestamp=latest["date"],
                            summary=f"{name} released at {latest_val:.2f}, change of {change:+.2f} ({change_pct:+.2%}) from previous {prev_val:.2f}",
                        ))

                except Exception as e:
                    logger.debug(f"FRED series {series_id} failed: {e}")
                    continue

    except Exception as e:
        logger.warning(f"FRED API failed: {e}")
        return _get_calendar_signals()

    logger.info(f"FRED: {len(signals)} macro releases")
    return signals


def _score_macro_sentiment(series_id: str, change: float, change_pct: float) -> float:
    """
    Score the market sentiment impact of a macro release.

    This is nuanced — hot inflation is BAD for stocks (Fed tightens),
    strong jobs is MIXED (good economy but Fed may tighten),
    GDP growth is GOOD (earnings growth).
    """
    # Fed Funds Rate: increase = bearish (tightening), decrease = bullish (easing)
    if series_id == "FEDFUNDS":
        return -0.5 if change > 0 else (0.5 if change < 0 else 0.0)

    # CPI/PPI: higher inflation = bearish (Fed will tighten)
    if series_id in ("CPIAUCSL", "PPIFIS"):
        if change_pct > 0.003:  # hot inflation
            return -0.4
        elif change_pct < -0.001:  # cooling inflation
            return 0.3
        return 0.0

    # Jobs: strong = mixed (good economy but Fed may tighten)
    if series_id in ("PAYEMS", "UNRATE"):
        if series_id == "UNRATE":
            return 0.2 if change < 0 else -0.2  # lower unemployment = bullish
        return 0.1 if change > 0 else -0.1  # more jobs = slightly bullish

    # GDP: higher = bullish
    if series_id == "GDP":
        return 0.3 if change > 0 else -0.3

    # Retail sales, housing, consumer sentiment: higher = bullish
    if series_id in ("RSXFS", "HOUST", "UMCSENT"):
        return 0.2 if change > 0 else -0.2

    return 0.0


def _macro_subcategory(series_id: str) -> str:
    """Map FRED series to news subcategory."""
    if series_id == "FEDFUNDS":
        return "fed"
    if series_id in ("CPIAUCSL", "PPIFIS"):
        return "economic"
    if series_id in ("PAYEMS", "UNRATE"):
        return "economic"
    if series_id == "GDP":
        return "economic"
    return "economic"


def _get_calendar_signals() -> list[NewsSignal]:
    """Fallback: return known release schedule as signals."""
    signals = []
    now = datetime.now(timezone.utc)

    # Check if we're near known release dates
    day = now.day
    weekday = now.weekday()

    # CPI typically releases ~13th of month
    if 12 <= day <= 14:
        signals.append(NewsSignal(
            headline="[CALENDAR] CPI release expected this week",
            source="calendar",
            symbols=["SPY"],
            category="macro",
            subcategory="economic",
            sentiment=0.0,
            relevance=0.8,
            urgency="important",
            timestamp=now.isoformat(),
            summary="Consumer Price Index release expected. High impact on rates and equities.",
        ))

    # Jobs report: first Friday
    if day <= 7 and weekday == 4:
        signals.append(NewsSignal(
            headline="[CALENDAR] Non-Farm Payrolls releasing today",
            source="calendar",
            symbols=["SPY"],
            category="macro",
            subcategory="economic",
            sentiment=0.0,
            relevance=1.0,
            urgency="breaking",
            timestamp=now.isoformat(),
            summary="Monthly jobs report. Critical market-moving event.",
        ))

    return signals


def _hours_since(date_str: str) -> float:
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except Exception:
        return 24.0
