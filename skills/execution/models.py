"""
Data models for Execution Agent.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ExecutionReport:
    """Report of executed trades."""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    mode: str = "daily"
    decision_id: str = ""
    orders_placed: int = 0
    orders_filled: int = 0
    total_notional: float = 0.0
    trades: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()
