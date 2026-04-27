"""
Dead letter queue for failed financial operations.
Stores failed operations for manual review and retry.
Handles partial execution recovery (e.g., arbitrage buy succeeded but sell failed).
"""
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .config import get_logger, audit_log
from .database import Database

logger = get_logger("dead_letter")


class FailureType(Enum):
    TRADE_EXECUTION = "trade_execution"
    ARBITRAGE_PARTIAL = "arbitrage_partial"  # One leg succeeded, other failed
    SWAP_EXECUTION = "swap_execution"
    REBALANCE_PARTIAL = "rebalance_partial"
    API_FAILURE = "api_failure"
    APPROVAL_TIMEOUT = "approval_timeout"
    STATE_INCONSISTENCY = "state_inconsistency"


class DLQStatus(Enum):
    PENDING = "pending"          # Awaiting manual review
    RETRYING = "retrying"        # Being retried
    RESOLVED = "resolved"        # Successfully resolved
    ABANDONED = "abandoned"      # Manually abandoned
    ESCALATED = "escalated"      # Escalated to admin


@dataclass
class DeadLetterEntry:
    id: str
    agent: str
    failure_type: FailureType
    description: str
    original_action: dict  # The action that was being attempted
    partial_result: Optional[dict] = None  # What succeeded before failure
    error: str = ""
    status: DLQStatus = DLQStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None
    resolution_notes: str = ""


class DeadLetterQueue:
    """
    Persistent dead letter queue for failed financial operations.

    Critical for fintech: when an arbitrage trade buys on exchange A
    but the sell on exchange B fails, we need to track the open position
    and either retry the sell or alert for manual intervention.
    """

    def __init__(self):
        self._entries: dict[str, DeadLetterEntry] = {}
        self._counter = 0
        self._init_db_table()

    def _init_db_table(self):
        """Ensure DLQ table exists in the database."""
        try:
            db = Database()
            conn = db._get_connection()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS dead_letter_queue (
                    id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    failure_type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    original_action TEXT NOT NULL,
                    partial_result TEXT,
                    error TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    created_at REAL NOT NULL,
                    resolved_at REAL,
                    resolution_notes TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_dlq_status ON dead_letter_queue(status);
                CREATE INDEX IF NOT EXISTS idx_dlq_agent ON dead_letter_queue(agent);
            """)
            conn.commit()
        except Exception as e:
            logger.warning(f"Could not init DLQ table: {e}")

    def enqueue(
        self,
        agent: str,
        failure_type: FailureType,
        description: str,
        original_action: dict,
        partial_result: dict = None,
        error: str = "",
        max_retries: int = 3,
    ) -> str:
        """Add a failed operation to the dead letter queue."""
        self._counter += 1
        entry_id = f"DLQ-{self._counter:06d}"

        entry = DeadLetterEntry(
            id=entry_id,
            agent=agent,
            failure_type=failure_type,
            description=description,
            original_action=original_action,
            partial_result=partial_result,
            error=error,
            max_retries=max_retries,
        )
        self._entries[entry_id] = entry

        # Persist to DB
        try:
            db = Database()
            with db.transaction() as conn:
                conn.execute(
                    """INSERT INTO dead_letter_queue
                       (id, agent, failure_type, description, original_action,
                        partial_result, error, status, retry_count, max_retries, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry_id, agent, failure_type.value, description,
                        json.dumps(original_action),
                        json.dumps(partial_result) if partial_result else None,
                        error, entry.status.value, 0, max_retries, entry.created_at,
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to persist DLQ entry: {e}")

        audit_log(agent, "dlq_enqueued", {
            "entry_id": entry_id,
            "failure_type": failure_type.value,
            "description": description,
            "error": error,
        })

        severity = "CRITICAL" if failure_type == FailureType.ARBITRAGE_PARTIAL else "ERROR"
        logger.log(
            40 if severity == "CRITICAL" else 30,
            f"DLQ [{entry_id}]: {failure_type.value} — {description}"
        )

        return entry_id

    def mark_retrying(self, entry_id: str) -> bool:
        """Mark an entry as being retried."""
        entry = self._entries.get(entry_id)
        if not entry or entry.status not in (DLQStatus.PENDING, DLQStatus.RETRYING):
            return False
        if entry.retry_count >= entry.max_retries:
            self.escalate(entry_id, "Max retries exceeded")
            return False
        entry.status = DLQStatus.RETRYING
        entry.retry_count += 1
        self._persist_status(entry)
        return True

    def resolve(self, entry_id: str, notes: str = "") -> bool:
        """Mark an entry as resolved."""
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        entry.status = DLQStatus.RESOLVED
        entry.resolved_at = time.time()
        entry.resolution_notes = notes
        self._persist_status(entry)

        audit_log(entry.agent, "dlq_resolved", {
            "entry_id": entry_id,
            "notes": notes,
            "retry_count": entry.retry_count,
        })
        return True

    def abandon(self, entry_id: str, notes: str = "") -> bool:
        """Manually abandon a failed operation (accept the loss)."""
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        entry.status = DLQStatus.ABANDONED
        entry.resolved_at = time.time()
        entry.resolution_notes = notes
        self._persist_status(entry)

        audit_log(entry.agent, "dlq_abandoned", {
            "entry_id": entry_id,
            "notes": notes,
            "partial_result": entry.partial_result,
        })
        return True

    def escalate(self, entry_id: str, reason: str = "") -> bool:
        """Escalate to admin for manual intervention."""
        entry = self._entries.get(entry_id)
        if not entry:
            return False
        entry.status = DLQStatus.ESCALATED
        entry.resolution_notes = f"ESCALATED: {reason}"
        self._persist_status(entry)

        audit_log(entry.agent, "dlq_escalated", {
            "entry_id": entry_id,
            "reason": reason,
            "failure_type": entry.failure_type.value,
        })
        return True

    def get_pending(self, agent: str = None) -> list[DeadLetterEntry]:
        """Get all pending/retrying entries, optionally filtered by agent."""
        active_statuses = {DLQStatus.PENDING, DLQStatus.RETRYING, DLQStatus.ESCALATED}
        return [
            e for e in self._entries.values()
            if e.status in active_statuses
            and (agent is None or e.agent == agent)
        ]

    def get_entry(self, entry_id: str) -> Optional[DeadLetterEntry]:
        return self._entries.get(entry_id)

    def format_alert(self, entry: DeadLetterEntry) -> str:
        """Format a DLQ entry for sending to admin."""
        icon = "🚨" if entry.failure_type == FailureType.ARBITRAGE_PARTIAL else "⚠️"
        lines = [
            f"{icon} Dead Letter Queue Alert [{entry.id}]",
            f"Agent: {entry.agent}",
            f"Type: {entry.failure_type.value}",
            f"Description: {entry.description}",
            f"Error: {entry.error}",
            f"Retries: {entry.retry_count}/{entry.max_retries}",
            f"Status: {entry.status.value}",
        ]
        if entry.partial_result:
            lines.append(f"Partial result: {json.dumps(entry.partial_result, indent=2)}")
        lines.append(f"\nActions: 'retry {entry.id}' | 'abandon {entry.id}' | 'escalate {entry.id}'")
        return "\n".join(lines)

    def _persist_status(self, entry: DeadLetterEntry):
        """Update entry status in the database."""
        try:
            db = Database()
            with db.transaction() as conn:
                conn.execute(
                    """UPDATE dead_letter_queue
                       SET status=?, retry_count=?, resolved_at=?, resolution_notes=?
                       WHERE id=?""",
                    (
                        entry.status.value, entry.retry_count,
                        entry.resolved_at, entry.resolution_notes, entry.id,
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to persist DLQ status: {e}")

    def get_stats(self) -> dict:
        """Get DLQ statistics for monitoring."""
        from collections import Counter
        status_counts = Counter(e.status.value for e in self._entries.values())
        type_counts = Counter(e.failure_type.value for e in self._entries.values())
        return {
            "total": len(self._entries),
            "by_status": dict(status_counts),
            "by_type": dict(type_counts),
            "pending_count": status_counts.get("pending", 0) + status_counts.get("retrying", 0),
            "escalated_count": status_counts.get("escalated", 0),
        }


# Singleton
dlq = DeadLetterQueue()
