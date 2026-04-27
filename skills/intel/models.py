"""
Data models for Market Intelligence Agent.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class RegimeData:
    """Cross-asset regime indicators."""
    vix_level: float = 0.0
    vix_change_1d: float = 0.0
    vix_regime: str = "normal"  # "low_vol" | "normal" | "elevated" | "crisis"
    # HMM regime detection (Hamilton 1989)
    hmm_regime: str = "unknown"              # "bull" | "sideways" | "bear"
    hmm_probabilities: dict = field(default_factory=dict)
    hmm_confidence: float = 0.0
    hmm_regime_duration: int = 0
    yield_curve_slope: float = 0.0  # TLT vs SHV spread change
    credit_spread: float = 0.0  # HYG vs LQD ratio change
    dollar_trend: float = 0.0  # UUP 5d return
    gold_trend: float = 0.0  # GLD 5d return

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class BreadthData:
    """Market breadth indicators."""
    advance_pct: float = 0.5  # % of tracked stocks with positive daily return
    sector_leaders: list[str] = field(default_factory=list)
    sector_laggards: list[str] = field(default_factory=list)
    sp500_return_1d: float = 0.0
    sp500_return_5d: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class SentimentData:
    """Aggregate news sentiment."""
    aggregate_score: float = 0.0  # -1 to +1
    n_articles: int = 0
    top_headlines: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class MarketBriefing:
    """Complete market intelligence briefing consumed by analyst agents."""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    session: str = "unknown"  # "pre_market" | "open" | "closing" | "after_hours" | "closed"
    regime: RegimeData = field(default_factory=RegimeData)
    breadth: BreadthData = field(default_factory=BreadthData)
    sentiment: SentimentData = field(default_factory=SentimentData)
    macro_summary: str = ""
    news_digest: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "session": self.session,
            "regime": self.regime.to_dict(),
            "breadth": self.breadth.to_dict(),
            "sentiment": self.sentiment.to_dict(),
            "macro_summary": self.macro_summary,
            "news_digest": self.news_digest,
        }
