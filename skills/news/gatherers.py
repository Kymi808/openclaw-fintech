"""
Specialized news gathering agents.

Three agents, each focused on a different level of analysis:
1. Macro — Fed, economic data, geopolitical, rates, commodities
2. Sector — industry trends, sector rotation, regulatory changes
3. Company — earnings, insider activity, management changes, M&A

Each gatherer fetches from Alpaca News API with targeted symbol/keyword
filters, scores relevance, and extracts structured signals.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np

from skills.shared import get_logger
from skills.market_data import get_data_provider

logger = get_logger("news.gatherers")


@dataclass
class NewsSignal:
    """A structured signal extracted from a news article."""
    headline: str
    source: str
    symbols: list[str]
    category: str        # "macro", "sector", "company"
    subcategory: str     # "fed", "earnings", "insider", etc.
    sentiment: float     # -1 to +1
    relevance: float     # 0 to 1
    urgency: str         # "breaking", "important", "routine"
    timestamp: str
    summary: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


# ── Keyword Lexicons ─────────────────────────────────────────────────────

MACRO_KEYWORDS = {
    "fed": ["federal reserve", "fomc", "interest rate", "powell", "rate cut", "rate hike",
            "monetary policy", "quantitative", "tightening", "easing"],
    "economic": ["gdp", "inflation", "cpi", "ppi", "jobs report", "unemployment",
                 "payrolls", "consumer spending", "retail sales", "housing"],
    "geopolitical": ["tariff", "sanctions", "trade war", "china", "russia", "ukraine",
                     "middle east", "oil embargo", "supply chain"],
    "rates": ["treasury", "yield curve", "bond", "10-year", "inversion",
              "credit spread", "high yield"],
}

SECTOR_KEYWORDS = {
    "technology": ["ai", "semiconductor", "chip", "software", "cloud", "data center", "nvidia", "tsmc"],
    "energy": ["oil", "natural gas", "opec", "drilling", "refinery", "renewable", "solar", "ev"],
    "healthcare": ["fda", "drug approval", "clinical trial", "biotech", "pharmaceutical", "medicare"],
    "financials": ["bank", "lending", "credit", "insurance", "fintech", "mortgage", "default"],
    "consumer": ["retail", "consumer spending", "e-commerce", "inflation impact", "brand"],
    "industrials": ["manufacturing", "supply chain", "infrastructure", "defense", "aerospace"],
}

COMPANY_KEYWORDS = {
    "earnings": ["earnings", "revenue", "eps", "beat", "miss", "guidance", "outlook", "forecast"],
    "insider": ["insider", "ceo", "cfo", "board", "executive", "resign", "appoint", "compensation"],
    "ma": ["acquisition", "merger", "takeover", "buyout", "deal", "bid"],
    "legal": ["lawsuit", "sec", "investigation", "settlement", "fine", "regulatory"],
    "analyst": ["upgrade", "downgrade", "price target", "initiate", "overweight", "underweight"],
}

POSITIVE_WORDS = {
    "beat", "exceeds", "surpass", "upgrade", "bullish", "growth", "profit",
    "gain", "rally", "surge", "boost", "strong", "record", "outperform",
    "rise", "soar", "breakout", "recovery", "expansion", "approval",
}
NEGATIVE_WORDS = {
    "miss", "disappoint", "downgrade", "bearish", "loss", "decline",
    "drop", "fall", "crash", "weak", "cut", "warning", "risk",
    "layoff", "lawsuit", "tariff", "default", "recession", "bankruptcy",
}


def _score_sentiment(text: str) -> float:
    """Score sentiment from text."""
    words = set(text.lower().split())
    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)
    total = pos + neg
    return (pos - neg) / max(total, 1)


def _match_keywords(text: str, keyword_dict: dict) -> list[tuple[str, int]]:
    """Find which keyword categories match the text."""
    text_lower = text.lower()
    matches = []
    for category, keywords in keyword_dict.items():
        count = sum(1 for kw in keywords if kw in text_lower)
        if count > 0:
            matches.append((category, count))
    return sorted(matches, key=lambda x: -x[1])


class MacroNewsGatherer:
    """
    Macro news agent — monitors Fed, economic data, geopolitical events.

    Focuses on broad market-moving events that affect all positions.
    Uses general market symbols (SPY, TLT, GLD, USO) for Alpaca news search.
    """
    WATCH_SYMBOLS = ["SPY", "TLT", "GLD", "USO", "UUP", "VIX"]

    async def gather(self, limit: int = 30) -> list[NewsSignal]:
        provider = get_data_provider()
        articles = await provider.get_news(symbols=self.WATCH_SYMBOLS, limit=limit)

        signals = []
        for article in articles:
            text = f"{article.headline} {article.summary}"
            matches = _match_keywords(text, MACRO_KEYWORDS)
            if not matches:
                continue

            subcategory = matches[0][0]
            relevance = min(1.0, matches[0][1] / 3)
            sentiment = _score_sentiment(text)

            # Urgency based on recency and keyword density
            age_hours = (datetime.now(timezone.utc) - article.created_at).total_seconds() / 3600
            urgency = "breaking" if age_hours < 1 else "important" if age_hours < 6 else "routine"

            signals.append(NewsSignal(
                headline=article.headline,
                source=article.source,
                symbols=article.symbols[:5],
                category="macro",
                subcategory=subcategory,
                sentiment=round(sentiment, 3),
                relevance=round(relevance, 3),
                urgency=urgency,
                timestamp=article.created_at.isoformat(),
                summary=article.summary[:200],
            ))

        logger.info(f"Macro news: {len(signals)} signals from {len(articles)} articles")
        return signals


class SectorNewsGatherer:
    """
    Sector news agent — monitors industry-specific developments.

    Tracks sector ETFs and leading stocks per sector.
    """
    SECTOR_SYMBOLS = {
        "technology": ["NVDA", "MSFT", "AAPL", "AVGO", "AMD"],
        "energy": ["XOM", "CVX", "COP", "SLB"],
        "healthcare": ["UNH", "LLY", "JNJ", "MRK", "ABBV"],
        "financials": ["JPM", "GS", "MS", "BLK", "V"],
        "consumer": ["AMZN", "TSLA", "HD", "COST", "MCD"],
        "industrials": ["BA", "CAT", "DE", "RTX", "HON"],
    }

    async def gather(self, limit: int = 30) -> list[NewsSignal]:
        provider = get_data_provider()

        # Fetch news for each sector's leading stocks
        all_symbols = []
        for syms in self.SECTOR_SYMBOLS.values():
            all_symbols.extend(syms[:3])  # top 3 per sector

        articles = await provider.get_news(symbols=all_symbols, limit=limit)

        signals = []
        for article in articles:
            text = f"{article.headline} {article.summary}"
            matches = _match_keywords(text, SECTOR_KEYWORDS)
            if not matches:
                continue

            subcategory = matches[0][0]
            relevance = min(1.0, matches[0][1] / 2)
            sentiment = _score_sentiment(text)

            age_hours = (datetime.now(timezone.utc) - article.created_at).total_seconds() / 3600
            urgency = "breaking" if age_hours < 1 else "important" if age_hours < 6 else "routine"

            signals.append(NewsSignal(
                headline=article.headline,
                source=article.source,
                symbols=article.symbols[:5],
                category="sector",
                subcategory=subcategory,
                sentiment=round(sentiment, 3),
                relevance=round(relevance, 3),
                urgency=urgency,
                timestamp=article.created_at.isoformat(),
                summary=article.summary[:200],
            ))

        logger.info(f"Sector news: {len(signals)} signals from {len(articles)} articles")
        return signals


class CompanyNewsGatherer:
    """
    Company news agent — monitors per-stock events.

    Tracks earnings, insider activity, M&A, analyst actions
    for all stocks in the current portfolio universe.
    """

    async def gather(
        self, symbols: list[str] = None, limit: int = 50,
    ) -> list[NewsSignal]:
        provider = get_data_provider()

        if symbols is None:
            # Default to large-cap universe
            from skills.intel.regime import BREADTH_SYMBOLS
            symbols = BREADTH_SYMBOLS

        # Batch in groups (Alpaca limits symbols per request)
        all_articles = []
        for i in range(0, len(symbols), 10):
            batch = symbols[i:i + 10]
            articles = await provider.get_news(symbols=batch, limit=20)
            all_articles.extend(articles)
            if i + 10 < len(symbols):
                await asyncio.sleep(0.2)

        signals = []
        for article in all_articles:
            text = f"{article.headline} {article.summary}"
            matches = _match_keywords(text, COMPANY_KEYWORDS)
            if not matches:
                continue

            subcategory = matches[0][0]
            relevance = min(1.0, matches[0][1] / 2)
            sentiment = _score_sentiment(text)

            age_hours = (datetime.now(timezone.utc) - article.created_at).total_seconds() / 3600
            urgency = "breaking" if age_hours < 2 else "important" if age_hours < 12 else "routine"

            # Earnings and analyst actions are higher urgency
            if subcategory in ("earnings", "analyst") and age_hours < 6:
                urgency = "breaking"

            signals.append(NewsSignal(
                headline=article.headline,
                source=article.source,
                symbols=article.symbols[:5],
                category="company",
                subcategory=subcategory,
                sentiment=round(sentiment, 3),
                relevance=round(relevance, 3),
                urgency=urgency,
                timestamp=article.created_at.isoformat(),
                summary=article.summary[:200],
            ))

        logger.info(f"Company news: {len(signals)} signals from {len(all_articles)} articles")
        return signals
