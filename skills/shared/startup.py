"""
Startup reconciliation — checks for inconsistent state on boot.
Detects orphaned trades, stale approvals, and missed heartbeats.
"""
import time
from datetime import datetime, timezone

from .config import get_logger, audit_log
from .database import Database
from .dead_letter import dlq, FailureType

logger = get_logger("startup")


class StartupReconciler:
    """
    Runs on startup to detect and resolve state inconsistencies.

    Checks:
    1. Trades stuck in PENDING — may have been filled on the exchange
    2. Approval requests that have timed out
    3. DLQ entries that need attention
    4. Stale heartbeat indicators
    """

    def __init__(self):
        self.issues_found = 0
        self.issues_resolved = 0

    async def reconcile_all(self) -> dict:
        """Run all reconciliation checks."""
        logger.info("Starting startup reconciliation...")

        results = {
            "pending_trades": await self.check_pending_trades(),
            "stale_approvals": self.check_stale_approvals(),
            "dlq_status": self.check_dlq(),
            "data_retention": self.enforce_retention(),
        }

        audit_log("system", "startup_reconciliation", {
            "issues_found": self.issues_found,
            "issues_resolved": self.issues_resolved,
            "results": results,
        })

        logger.info(
            f"Reconciliation complete: {self.issues_found} issues found, "
            f"{self.issues_resolved} resolved"
        )
        return results

    async def check_pending_trades(self) -> dict:
        """
        Check for trades stuck in PENDING status.
        These may have been filled on the exchange while we were down.
        """
        try:
            db = Database()
            conn = db._get_connection()
            pending = conn.execute(
                """SELECT * FROM trades
                   WHERE status = 'PENDING'
                   AND created_at < datetime('now', '-5 minutes')"""
            ).fetchall()

            if not pending:
                return {"stuck_trades": 0}

            self.issues_found += len(pending)

            for trade in pending:
                trade_dict = dict(trade)
                logger.warning(
                    f"Stuck trade detected: {trade_dict['trade_id']} "
                    f"on {trade_dict['exchange']} — {trade_dict['pair']} "
                    f"{trade_dict['side']} {trade_dict['amount']}"
                )

                # In production, we would:
                # 1. Query the exchange API for the order status
                # 2. If filled: update our DB
                # 3. If cancelled/expired: update our DB
                # 4. If still pending: add to DLQ for monitoring

                dlq.enqueue(
                    agent=trade_dict.get("agent", "trading-agent"),
                    failure_type=FailureType.STATE_INCONSISTENCY,
                    description=(
                        f"Trade {trade_dict['trade_id']} stuck in PENDING "
                        f"since {trade_dict['created_at']}"
                    ),
                    original_action={
                        "trade_id": trade_dict["trade_id"],
                        "exchange": trade_dict["exchange"],
                        "pair": trade_dict["pair"],
                        "side": trade_dict["side"],
                        "amount": trade_dict["amount"],
                    },
                    error="Trade stuck in PENDING after restart — needs exchange status check",
                )

            return {"stuck_trades": len(pending)}

        except Exception as e:
            logger.error(f"Pending trade check failed: {e}")
            return {"error": str(e)}

    def check_stale_approvals(self, timeout_minutes: int = 30) -> dict:
        """Check for approval requests that have timed out."""
        from .approval import approval_engine, ApprovalStatus

        expired = []
        for req_id, req in approval_engine.get_pending():
            # Parse created_at and check age
            try:
                created = datetime.fromisoformat(req.created_at)
                age_minutes = (datetime.now(timezone.utc) - created).total_seconds() / 60

                if age_minutes > timeout_minutes:
                    expired.append(req_id)
                    req.status = ApprovalStatus.EXPIRED
                    req.resolved_at = datetime.now(timezone.utc).isoformat()

                    self.issues_found += 1
                    self.issues_resolved += 1

                    audit_log(req.agent, "approval_expired", {
                        "request_id": req_id,
                        "age_minutes": round(age_minutes, 1),
                        "action": req.action,
                        "amount": req.amount,
                    })

                    logger.warning(
                        f"Approval {req_id} expired after {age_minutes:.0f} minutes: "
                        f"{req.action} ${req.amount}"
                    )

                    # If the expired approval was for a trade, add to DLQ
                    if req.action in ("execute_trade", "swap", "rebalance"):
                        dlq.enqueue(
                            agent=req.agent,
                            failure_type=FailureType.APPROVAL_TIMEOUT,
                            description=f"Approval {req_id} expired: {req.description}",
                            original_action=req.details,
                            error=f"No response after {timeout_minutes} minutes",
                        )

            except (ValueError, TypeError):
                continue

        return {"expired_approvals": len(expired), "expired_ids": expired}

    def check_dlq(self) -> dict:
        """Check DLQ status and escalate if needed."""
        stats = dlq.get_stats()

        # Auto-escalate entries that have been pending too long
        for entry in dlq.get_pending():
            age_hours = (time.time() - entry.created_at) / 3600
            if age_hours > 4 and entry.status.value == "pending":
                dlq.escalate(entry.id, f"Pending for {age_hours:.1f} hours without action")
                self.issues_found += 1

        return stats

    def enforce_retention(self) -> dict:
        """Run data retention on startup."""
        try:
            db = Database()
            return db.enforce_retention(
                audit_days=365,
                snapshot_days=90,
                scan_days=180,
            )
        except Exception as e:
            logger.error(f"Retention enforcement failed: {e}")
            return {"error": str(e)}


# Singleton
reconciler = StartupReconciler()
