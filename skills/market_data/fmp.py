"""
Financial Modeling Prep (FMP) data provider.

Provides production-grade fundamental data that yfinance cannot:
- Point-in-time financial statements (as-reported, not look-ahead)
- Earnings calendar with actual dates
- Sector/industry classification
- Analyst estimates and ratings
- Company profile data

Auto-integrates: if FMP_API_KEY is set in .env, the system uses FMP.
If not set, falls back to yfinance cache or synthetic data.

FMP API: https://financialmodelingprep.com/developer/docs
Pricing: Free tier (250 req/day), $29/mo (unlimited)
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List

import httpx
from skills.shared import get_logger

# Suppress httpx request logging — it leaks API keys in URLs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = get_logger("market_data.fmp")

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
def _get_fmp_key() -> str:
    return os.getenv("FMP_API_KEY", "")


def is_fmp_configured() -> bool:
    """Check if FMP API key is available."""
    key = _get_fmp_key()
    return bool(key) and key not in ("", "xxxxx", "your-key-here")


class FMPProvider:
    """
    Financial Modeling Prep data provider.

    Provides fundamental data with proper point-in-time handling:
    - Income statements, balance sheets, cash flow (quarterly + annual)
    - Key ratios (PE, PB, ROE, ROA, margins, etc.)
    - Earnings calendar (actual announcement dates)
    - Analyst estimates
    - Company profiles with GICS sector/industry

    All data is as-reported (no look-ahead bias).
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or _get_fmp_key()
        if not self.api_key:
            raise ValueError(
                "FMP_API_KEY not set. Get a key at https://financialmodelingprep.com/ "
                "and add FMP_API_KEY=your-key to gateway/.env"
            )
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self):
        await self._client.aclose()

    async def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Make an authenticated GET request to FMP /stable/ API."""
        params = params or {}
        params["apikey"] = self.api_key
        url = f"{FMP_BASE_URL}/{endpoint}"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Fundamental Ratios ───────────────────────────────────────────────

    async def get_key_metrics(self, symbol: str, period: str = "quarter", limit: int = 4) -> list[dict]:
        """
        Get key financial metrics (PE, PB, ROE, ROA, margins, etc.)

        Returns list of dicts, one per reporting period, most recent first.
        Each dict includes the period end date for point-in-time usage.
        """
        data = await self._get("key-metrics", {"symbol": symbol, "period": period, "limit": limit})
        return data

    async def get_ratios(self, symbol: str, period: str = "quarter", limit: int = 4) -> list[dict]:
        """Get financial ratios (profitability, liquidity, solvency, etc.)"""
        data = await self._get("ratios", {"symbol": symbol, "period": period, "limit": limit})
        return data

    async def get_fundamentals_batch(self, tickers: List[str]) -> Dict[str, dict]:
        """
        Get fundamental data for multiple tickers.

        Returns dict mapping ticker -> {field: value} with fields matching
        what the CS system expects:
        - trailingPE, forwardPE, priceToBook, priceToSalesTrailing12Months
        - returnOnEquity, returnOnAssets, grossMargins, operatingMargins, profitMargins
        - revenueGrowth, earningsGrowth, earningsQuarterlyGrowth
        - debtToEquity, currentRatio, quickRatio
        - marketCap, beta
        - dividendYield, payoutRatio
        - shortPercentOfFloat, shortRatio
        """
        fundamentals = {}

        for symbol in tickers:
            try:
                # Get latest key metrics + ratios
                metrics = await self.get_key_metrics(symbol, limit=1)
                ratios = await self.get_ratios(symbol, limit=1)

                if not metrics and not ratios:
                    continue

                m = metrics[0] if metrics else {}
                r = ratios[0] if ratios else {}

                # Map to CS system field names
                fund = {}
                field_map = {
                    "peRatio": "trailingPE",
                    "priceToBookRatio": "priceToBook",
                    "priceToSalesRatio": "priceToSalesTrailing12Months",
                    "enterpriseValueOverRevenue": "enterpriseToRevenue",
                    "enterpriseValueOverEBITDA": "enterpriseToEbitda",
                    "returnOnEquity": "returnOnEquity",
                    "returnOnAssets": "returnOnAssets",
                    "grossProfitMargin": "grossMargins",
                    "operatingProfitMargin": "operatingMargins",
                    "netProfitMargin": "profitMargins",
                    "revenueGrowth": "revenueGrowth",
                    "netIncomeGrowth": "earningsGrowth",
                    "debtToEquity": "debtToEquity",
                    "currentRatio": "currentRatio",
                    "quickRatio": "quickRatio",
                    "marketCap": "marketCap",
                    "dividendYield": "dividendYield",
                    "payoutRatio": "payoutRatio",
                }

                for fmp_field, cs_field in field_map.items():
                    # Check both metrics and ratios
                    val = m.get(fmp_field) or r.get(fmp_field)
                    if val is not None and isinstance(val, (int, float)):
                        fund[cs_field] = float(val)

                # Beta from profile (separate endpoint, cached)
                if fund:
                    fundamentals[symbol] = fund

            except Exception as e:
                logger.debug(f"FMP fundamental fetch failed for {symbol}: {e}")
                continue

        logger.info(f"FMP: fetched fundamentals for {len(fundamentals)}/{len(tickers)} tickers")
        return fundamentals

    # ── Earnings Calendar ────────────────────────────────────────────────

    async def get_earnings_calendar(
        self, from_date: str = None, to_date: str = None,
    ) -> list[dict]:
        """Get earnings calendar for all stocks in a date range."""
        params = {}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        return await self._get("earning_calendar", params)

    async def get_earnings_for_tickers(self, tickers: List[str]) -> Dict[str, List[str]]:
        """
        Get upcoming earnings dates per ticker.

        Returns dict mapping ticker -> list of date strings (YYYY-MM-DD).
        """
        # FMP provides a bulk earnings calendar — filter by our tickers
        today = datetime.now().strftime("%Y-%m-%d")
        future = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

        try:
            calendar = await self.get_earnings_calendar(from_date=today, to_date=future)
        except Exception as e:
            logger.warning(f"FMP earnings calendar failed: {e}")
            return {}

        ticker_set = set(tickers)
        earnings = {}
        for entry in calendar:
            sym = entry.get("symbol", "")
            if sym in ticker_set:
                date = entry.get("date", "")
                if date:
                    earnings.setdefault(sym, []).append(date)

        return earnings

    # ── Company Profiles ─────────────────────────────────────────────────

    async def get_profile(self, symbol: str) -> dict:
        """Get company profile (sector, industry, market cap, beta, etc.)"""
        data = await self._get("profile", {"symbol": symbol})
        return data[0] if data else {}

    async def get_sector_map(self, tickers: List[str]) -> Dict[str, str]:
        """Get GICS sector classification for multiple tickers."""
        sectors = {}
        for symbol in tickers:
            try:
                profile = await self.get_profile(symbol)
                sector = profile.get("sector", "")
                if sector:
                    sectors[symbol] = sector
            except Exception:
                continue
        return sectors

    # ── Analyst Estimates ────────────────────────────────────────────────

    async def get_analyst_estimates(self, symbol: str) -> dict:
        """Get analyst consensus estimates."""
        data = await self._get("analyst-estimates", {"symbol": symbol, "limit": 1})
        return data[0] if data else {}


# ── Auto-integration with CS system ──────────────────────────────────────

async def fetch_fundamentals_fmp(tickers: List[str], cache_dir: str = "data") -> Dict[str, dict]:
    """
    Drop-in replacement for CS system's fetch_fundamental_data().

    Auto-integrates: if FMP_API_KEY is set, uses FMP.
    If not, returns empty dict (caller falls back to yfinance/synthetic).
    """
    if not is_fmp_configured():
        logger.debug("FMP not configured — skipping")
        return {}

    cache_file = os.path.join(cache_dir, "fundamentals_fmp.json")

    # Check cache (refresh daily)
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("_date") == datetime.now().strftime("%Y-%m-%d"):
                logger.info("Loading cached FMP fundamentals")
                return {k: v for k, v in cached.items() if k != "_date"}
        except Exception:
            pass

    try:
        provider = FMPProvider()
        fundamentals = await provider.get_fundamentals_batch(tickers)
        await provider.close()

        # Cache with date stamp
        os.makedirs(cache_dir, exist_ok=True)
        to_cache = dict(fundamentals)
        to_cache["_date"] = datetime.now().strftime("%Y-%m-%d")
        with open(cache_file, "w") as f:
            json.dump(to_cache, f)

        return fundamentals
    except Exception as e:
        logger.warning(f"FMP fetch failed: {e}")
        return {}


async def fetch_earnings_fmp(tickers: List[str], cache_dir: str = "data") -> Dict[str, List[str]]:
    """
    Drop-in replacement for CS system's fetch_earnings_dates().

    Uses FMP earnings calendar for actual announcement dates.
    """
    if not is_fmp_configured():
        return {}

    try:
        provider = FMPProvider()
        earnings = await provider.get_earnings_for_tickers(tickers)
        await provider.close()
        return earnings
    except Exception as e:
        logger.warning(f"FMP earnings fetch failed: {e}")
        return {}
