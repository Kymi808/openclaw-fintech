"""
Human-in-the-loop approval workflow for financial actions.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from .config import audit_log, get_logger

logger = get_logger("approval")


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    AUTO_APPROVED = "auto_approved"


@dataclass
class ApprovalRequest:
    agent: str
    action: str
    description: str
    amount: Optional[float] = None
    details: dict = field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    resolved_at: Optional[str] = None
    timeout_seconds: int = 300  # 5 minutes


class ApprovalEngine:
    """Manages approval requests for financial actions."""

    def __init__(self):
        self._pending: dict[str, ApprovalRequest] = {}
        self._counter = 0

    def should_auto_approve(self, agent: str, action: str, amount: float,
                            limits: dict) -> bool:
        """Check if an action can be auto-approved based on limits."""
        threshold = limits.get("approval_threshold", 200.0)
        if amount <= threshold and action in ("execute_trade", "swap"):
            return True
        # Rebalance always requires approval
        if action == "rebalance":
            return False
        # Governance always requires approval
        if action == "governance_vote":
            return False
        return False

    def create_request(self, agent: str, action: str, description: str,
                       amount: float = 0.0, details: dict = None) -> str:
        """Create an approval request and return its ID."""
        self._counter += 1
        req_id = f"APR-{self._counter:06d}"
        req = ApprovalRequest(
            agent=agent,
            action=action,
            description=description,
            amount=amount,
            details=details or {},
        )
        self._pending[req_id] = req

        audit_log(agent, "approval_requested", {
            "request_id": req_id,
            "action": action,
            "amount": amount,
            "description": description,
        })

        logger.info(f"Approval request {req_id}: {action} ${amount:.2f} — {description}")
        return req_id

    def approve(self, req_id: str) -> bool:
        """Approve a pending request."""
        req = self._pending.get(req_id)
        if not req or req.status != ApprovalStatus.PENDING:
            return False
        req.status = ApprovalStatus.APPROVED
        req.resolved_at = datetime.now(timezone.utc).isoformat()
        audit_log(req.agent, "approval_granted", {"request_id": req_id})
        logger.info(f"Approved: {req_id}")
        return True

    def deny(self, req_id: str) -> bool:
        """Deny a pending request."""
        req = self._pending.get(req_id)
        if not req or req.status != ApprovalStatus.PENDING:
            return False
        req.status = ApprovalStatus.DENIED
        req.resolved_at = datetime.now(timezone.utc).isoformat()
        audit_log(req.agent, "approval_denied", {"request_id": req_id})
        logger.info(f"Denied: {req_id}")
        return True

    def get_pending(self) -> list[tuple[str, ApprovalRequest]]:
        """Return all pending approval requests."""
        return [
            (rid, req) for rid, req in self._pending.items()
            if req.status == ApprovalStatus.PENDING
        ]

    def format_request_message(self, req_id: str) -> str:
        """Format an approval request for sending to the user."""
        req = self._pending.get(req_id)
        if not req:
            return f"Unknown request: {req_id}"

        msg = (
            f"⚠️ Approval Required [{req_id}]\n"
            f"Agent: {req.agent}\n"
            f"Action: {req.action}\n"
        )
        if req.amount:
            msg += f"Amount: ${req.amount:,.2f}\n"
        msg += (
            f"Details: {req.description}\n\n"
            f"Reply 'approve {req_id}' or 'deny {req_id}'"
        )
        return msg


# Singleton
approval_engine = ApprovalEngine()
