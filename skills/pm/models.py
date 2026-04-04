"""
Data models for PM Agent decisions.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class PMDecision:
    """Portfolio Manager's final decision after resolving the bull/bear debate."""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    decision_id: str = ""
    mode: str = "daily"  # "daily" | "intraday"
    final_params: dict = field(default_factory=dict)
    resolution: dict = field(default_factory=dict)
    changes_from_current: dict = field(default_factory=dict)
    requires_approval: bool = False
    approval_id: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()
