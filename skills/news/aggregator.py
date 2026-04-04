"""
News aggregation — combines signals from all 3 news gatherers into
a structured digest consumed by the analyst agents.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from skills.shared import get_logger
from .gatherers import (
    MacroNewsGatherer, SectorNewsGatherer, CompanyNewsGatherer,
    NewsSignal,
)
from .edgar import fetch_recent_filings
from .fred import fetch_macro_releases

logger = get_logger("news.aggregator")


@dataclass
class NewsDigest:
    """
    Aggregated news digest from all 3 gatherers.
    Fed into the MarketBriefing consumed by analyst agents.
    """
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    n_total: int = 0
    n_breaking: int = 0
    macro_sentiment: float = 0.0
    sector_sentiment: dict = field(default_factory=dict)  # sector -> sentiment
    company_signals: list = field(default_factory=list)    # top company events
    macro_signals: list = field(default_factory=list)      # top macro events
    sector_signals: list = field(default_factory=list)     # top sector events
    overall_sentiment: float = 0.0
    key_themes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "n_total": self.n_total,
            "n_breaking": self.n_breaking,
            "macro_sentiment": self.macro_sentiment,
            "sector_sentiment": self.sector_sentiment,
            "overall_sentiment": self.overall_sentiment,
            "key_themes": self.key_themes,
            "top_macro": [s.to_dict() for s in self.macro_signals[:5]],
            "top_sector": [s.to_dict() for s in self.sector_signals[:5]],
            "top_company": [s.to_dict() for s in self.company_signals[:5]],
        }


async def aggregate_all_news(portfolio_symbols: list[str] = None) -> NewsDigest:
    """
    Run all 3 news gatherers in parallel and aggregate into a digest.

    Args:
        portfolio_symbols: stocks currently in portfolio (for company news focus)

    Returns:
        NewsDigest with macro/sector/company signals
    """
    macro_gatherer = MacroNewsGatherer()
    sector_gatherer = SectorNewsGatherer()
    company_gatherer = CompanyNewsGatherer()

    # Run all 5 sources in parallel (3 Alpaca news + EDGAR + FRED)
    macro_signals, sector_signals, company_signals, edgar_signals, fred_signals = await asyncio.gather(
        macro_gatherer.gather(limit=30),
        sector_gatherer.gather(limit=30),
        company_gatherer.gather(symbols=portfolio_symbols, limit=50),
        fetch_recent_filings(tickers=portfolio_symbols, days_back=3),
        fetch_macro_releases(days_back=3),
    )

    # Merge EDGAR filings into company signals, FRED into macro
    company_signals.extend(edgar_signals)
    macro_signals.extend(fred_signals)

    # Sort each by relevance × urgency priority
    urgency_weight = {"breaking": 3.0, "important": 2.0, "routine": 1.0}
    for signals in (macro_signals, sector_signals, company_signals):
        signals.sort(
            key=lambda s: s.relevance * urgency_weight.get(s.urgency, 1.0),
            reverse=True,
        )

    # Compute aggregate sentiments
    macro_sent = _avg_sentiment(macro_signals)
    sector_sentiments = _sector_sentiments(sector_signals)
    company_sent = _avg_sentiment(company_signals)

    all_signals = macro_signals + sector_signals + company_signals
    overall = _avg_sentiment(all_signals)
    n_breaking = sum(1 for s in all_signals if s.urgency == "breaking")

    # Extract key themes (most common subcategories)
    subcats = {}
    for s in all_signals:
        subcats[s.subcategory] = subcats.get(s.subcategory, 0) + s.relevance
    key_themes = sorted(subcats, key=subcats.get, reverse=True)[:5]

    digest = NewsDigest(
        n_total=len(all_signals),
        n_breaking=n_breaking,
        macro_sentiment=round(macro_sent, 3),
        sector_sentiment=sector_sentiments,
        company_signals=company_signals[:10],
        macro_signals=macro_signals[:10],
        sector_signals=sector_signals[:10],
        overall_sentiment=round(overall, 3),
        key_themes=key_themes,
    )

    logger.info(
        f"News digest: {digest.n_total} signals ({digest.n_breaking} breaking), "
        f"sentiment={digest.overall_sentiment:+.3f}, themes={digest.key_themes}"
    )

    return digest


def _avg_sentiment(signals: list[NewsSignal]) -> float:
    if not signals:
        return 0.0
    weighted = sum(s.sentiment * s.relevance for s in signals)
    total_weight = sum(s.relevance for s in signals)
    return weighted / total_weight if total_weight > 0 else 0.0


def _sector_sentiments(signals: list[NewsSignal]) -> dict[str, float]:
    sectors = {}
    counts = {}
    for s in signals:
        sec = s.subcategory
        sectors[sec] = sectors.get(sec, 0) + s.sentiment * s.relevance
        counts[sec] = counts.get(sec, 0) + s.relevance
    return {
        sec: round(sectors[sec] / counts[sec], 3) if counts[sec] > 0 else 0.0
        for sec in sectors
    }
