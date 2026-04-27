"""
Human-in-the-loop approval workflow for financial actions.

SQLite-backed for persistence across process restarts.
Supports: create, approve, deny, expire, query history.
"""
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

import numpy as np

from .config import audit_log, get_logger

logger = get_logger("approval")

DB_PATH = os.path.join("data", "approvals.db")


def _json_default(obj):
    """Handle numpy types in JSON serialization."""
    if isinstance(obj, (np.bool_, np.integer)):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


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
    """
    Manages approval requests for financial actions.

    SQLite-backed: all pending requests survive process restarts.
    Interface is unchanged from the original in-memory version.
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path) if os.path.dirname(self.db_path) else ".", exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approval_requests (
                    req_id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    action TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount REAL DEFAULT 0,
                    details TEXT DEFAULT '{}',
                    status TEXT DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    timeout_seconds INTEGER DEFAULT 300
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status ON approval_requests(status)
            """)

    def _get_next_id(self) -> str:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM approval_requests"
            ).fetchone()
        count = (row[0] if row else 0) + 1
        return f"APR-{count:06d}"

    def should_auto_approve(self, agent: str, action: str, amount: float,
                            limits: dict) -> bool:
        """Check if an action can be auto-approved based on limits."""
        threshold = limits.get("approval_threshold", 200.0)
        if amount <= threshold and action in ("execute_trade", "swap"):
            return True
        if action == "rebalance":
            return False
        if action == "governance_vote":
            return False
        return False

    def create_request(self, agent: str, action: str, description: str,
                       amount: float = 0.0, details: dict = None) -> str:
        """Create an approval request and return its ID."""
        req_id = self._get_next_id()
        now = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO approval_requests
                (req_id, agent, action, description, amount, details, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                req_id, agent, action, description, amount,
                json.dumps(details or {}, default=_json_default), "pending", now,
            ))

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
        return self._resolve(req_id, ApprovalStatus.APPROVED)

    def deny(self, req_id: str) -> bool:
        """Deny a pending request."""
        return self._resolve(req_id, ApprovalStatus.DENIED)

    def _resolve(self, req_id: str, new_status: ApprovalStatus) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            result = conn.execute(
                "UPDATE approval_requests SET status = ?, resolved_at = ? "
                "WHERE req_id = ? AND status = 'pending'",
                (new_status.value, now, req_id),
            )
            if result.rowcount == 0:
                return False

        action = "approval_granted" if new_status == ApprovalStatus.APPROVED else "approval_denied"
        # Get agent for audit
        req = self._get_request(req_id)
        if req:
            audit_log(req.agent, action, {"request_id": req_id})

        logger.info(f"{new_status.value.title()}: {req_id}")
        return True

    def get_pending(self) -> list[tuple[str, ApprovalRequest]]:
        """Return all pending approval requests."""
        # First, expire any timed-out requests
        self._expire_old_requests()

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT req_id, agent, action, description, amount, details, "
                "status, created_at, resolved_at, timeout_seconds "
                "FROM approval_requests WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()

        return [(row[0], self._row_to_request(row)) for row in rows]

    def get_history(self, limit: int = 50) -> list[tuple[str, ApprovalRequest]]:
        """Return recent approval history (all statuses)."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT req_id, agent, action, description, amount, details, "
                "status, created_at, resolved_at, timeout_seconds "
                "FROM approval_requests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

        return [(row[0], self._row_to_request(row)) for row in rows]

    def format_request_message(self, req_id: str) -> str:
        """Format an approval request for sending to the user."""
        req = self._get_request(req_id)
        if not req:
            return f"Unknown request: {req_id}"

        msg = (
            f"Approval Required [{req_id}]\n"
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

    def _get_request(self, req_id: str) -> Optional[ApprovalRequest]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT req_id, agent, action, description, amount, details, "
                "status, created_at, resolved_at, timeout_seconds "
                "FROM approval_requests WHERE req_id = ?",
                (req_id,),
            ).fetchone()
        if not row:
            return None
        return self._row_to_request(row)

    def _row_to_request(self, row) -> ApprovalRequest:
        return ApprovalRequest(
            agent=row[1],
            action=row[2],
            description=row[3],
            amount=row[4],
            details=json.loads(row[5]) if row[5] else {},
            status=ApprovalStatus(row[6]),
            created_at=row[7],
            resolved_at=row[8],
            timeout_seconds=row[9] or 300,
        )

    def _expire_old_requests(self):
        """Auto-expire requests that have exceeded their timeout."""
        now = datetime.now(timezone.utc)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT req_id, created_at, timeout_seconds "
                "FROM approval_requests WHERE status = 'pending'"
            ).fetchall()

            for req_id, created_at, timeout in rows:
                created = datetime.fromisoformat(created_at)
                if now - created > timedelta(seconds=timeout):
                    conn.execute(
                        "UPDATE approval_requests SET status = 'expired', resolved_at = ? "
                        "WHERE req_id = ?",
                        (now.isoformat(), req_id),
                    )
                    logger.info(f"Expired: {req_id} (timeout {timeout}s)")


# Singleton
approval_engine = ApprovalEngine()
