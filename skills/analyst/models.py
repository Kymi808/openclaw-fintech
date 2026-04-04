"""
Data models for Analyst Agent theses.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ReasoningPoint:
    """A single structured argument in a thesis."""
    factor: str       # e.g., "vix_regime", "model_dispersion", "sentiment"
    observation: str  # what the data shows
    implication: str  # why it matters
    weight: float     # 0-1, how much this drove the conviction

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Thesis:
    """Structured output from a bull or bear analyst."""
    agent: str  # "bull" or "bear"
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    conviction: float = 0.5  # 0.0 to 1.0
    recommended_params: dict = field(default_factory=dict)
    reasoning: list[ReasoningPoint] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "timestamp": self.timestamp,
            "conviction": self.conviction,
            "recommended_params": self.recommended_params,
            "reasoning": [r.to_dict() for r in self.reasoning],
            "risk_flags": self.risk_flags,
        }
