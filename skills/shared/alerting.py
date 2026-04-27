"""
Production alerting via webhooks (Slack, Discord, email-compatible).

Sends alerts for:
- Pipeline failures
- Order rejections
- Reconciliation discrepancies
- Drawdown warnings
- Fill price deviations

Configure via ALERT_WEBHOOK_URL in .env. Supports Slack and Discord webhook formats.
"""
import os
from datetime import datetime, timezone
from enum import Enum

import httpx

from .config import get_logger

logger = get_logger("alerting")

WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")


class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


async def send_alert(
    title: str,
    message: str,
    level: AlertLevel = AlertLevel.INFO,
    fields: dict = None,
) -> bool:
    """
    Send an alert via webhook.

    Supports Slack-format webhooks (also works with Discord via /slack endpoint).
    If no webhook URL configured, logs the alert instead.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Build Slack-compatible payload
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"[{level.value.upper()}] {title}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": message},
        },
    ]

    if fields:
        field_texts = [f"*{k}:* {v}" for k, v in fields.items()]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(field_texts)},
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"OpenClaw Trading | {timestamp}"}],
    })

    payload = {"blocks": blocks, "text": f"[{level.value.upper()}] {title}: {message}"}

    if not WEBHOOK_URL:
        # No webhook configured — log instead
        log_fn = {"info": logger.info, "warning": logger.warning, "critical": logger.error}
        log_fn.get(level.value, logger.info)(
            f"ALERT [{level.value}] {title}: {message}"
            + (f" | {fields}" if fields else "")
        )
        return True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(WEBHOOK_URL, json=payload)
            if resp.status_code in (200, 204):
                return True
            logger.warning(f"Webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Failed to send alert: {e}")
        return False


# ── Convenience functions ────────────────────────────────────────────────

async def alert_pipeline_failure(cycle_type: str, step: str, error: str):
    await send_alert(
        title=f"{cycle_type.title()} Pipeline Failed",
        message=f"Failed at step: *{step}*\n```{error[:500]}```",
        level=AlertLevel.CRITICAL,
        fields={"Cycle": cycle_type, "Step": step},
    )


async def alert_order_rejected(symbol: str, side: str, reason: str):
    await send_alert(
        title=f"Order Rejected: {symbol}",
        message=f"{side.upper()} {symbol} rejected: {reason}",
        level=AlertLevel.WARNING,
        fields={"Symbol": symbol, "Side": side},
    )


async def alert_reconciliation_discrepancy(n_discrepancies: int, details: str):
    await send_alert(
        title=f"Position Reconciliation: {n_discrepancies} Discrepancies",
        message=details,
        level=AlertLevel.CRITICAL if n_discrepancies > 0 else AlertLevel.INFO,
        fields={"Discrepancies": str(n_discrepancies)},
    )


async def alert_drawdown(current_dd: float, threshold: float):
    await send_alert(
        title=f"Drawdown Warning: {current_dd:.2%}",
        message=f"Current drawdown ({current_dd:.2%}) approaching threshold ({threshold:.2%})",
        level=AlertLevel.WARNING,
        fields={"Drawdown": f"{current_dd:.2%}", "Threshold": f"{threshold:.2%}"},
    )


async def alert_fill_deviation(symbol: str, expected: float, actual: float, deviation: float):
    await send_alert(
        title=f"Fill Price Deviation: {symbol}",
        message=f"Expected ${expected:.2f}, filled at ${actual:.2f} ({deviation:.2%} deviation)",
        level=AlertLevel.WARNING,
        fields={"Symbol": symbol, "Expected": f"${expected:.2f}", "Actual": f"${actual:.2f}"},
    )


async def alert_daily_summary(stats: dict):
    await send_alert(
        title="Daily P&L Summary",
        message=(
            f"Equity: ${stats.get('equity', 0):,.2f}\n"
            f"Daily: {stats.get('daily_return', 'N/A')}\n"
            f"Cumulative: {stats.get('cumulative_return', 'N/A')}\n"
            f"Sharpe (30d): {stats.get('sharpe_30d', 'N/A')}"
        ),
        level=AlertLevel.INFO,
        fields={
            "Positions": str(stats.get("n_positions", 0)),
            "Gross Exposure": stats.get("gross_exposure", "N/A"),
        },
    )
