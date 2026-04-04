"""
Feedback loop — the daily learning cycle.

Called by the scheduler after market close. Connects:
1. OutcomeScorer — evaluates past predictions
2. WeightAdapter — adjusts weights based on scores
3. Model retraining trigger — checks if it's time to retrain

This is what makes the system evolve over time.
"""
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from skills.shared import get_logger, audit_log
from skills.shared.state import safe_load_state, safe_save_state
from .scorer import OutcomeScorer
from .adapter import WeightAdapter, get_weight_adapter

logger = get_logger("feedback.loop")

RETRAIN_INTERVAL_DAYS = 14  # retrain ML models every 14 calendar days
RETRAIN_STATE_FILE = Path("./data/retrain_state.json")


async def run_feedback_loop():
    """
    Daily feedback loop — called after market close.

    Steps:
    1. Get today's portfolio return
    2. Score mature predictions (10+ days old)
    3. Update adaptive weights
    4. Check if ML model retraining is due
    5. Log learning progress
    """
    logger.info("=== FEEDBACK LOOP START ===")

    scorer = OutcomeScorer()
    adapter = get_weight_adapter()

    # 1. Get recent portfolio return for scoring
    portfolio_return = await _get_recent_return()
    market_return = await _get_market_return()

    logger.info(f"Portfolio return (10d): {portfolio_return:+.4f}, Market: {market_return:+.4f}")

    # 2. Score mature predictions
    scorer.score_outcomes(portfolio_return, market_return)

    # 3. Update adaptive weights
    adapter.update_weights()

    # 4. Check model retraining
    retrain_needed = _check_retrain_due()
    if retrain_needed:
        logger.info("MODEL RETRAIN DUE — schedule retraining of CrossMamba/TST/LightGBM")
        audit_log("feedback", "retrain_due", {
            "last_retrain": _get_last_retrain_date(),
            "interval_days": RETRAIN_INTERVAL_DAYS,
        })
        # In production: trigger actual retraining via subprocess or job queue
        # For now: log and alert
        from skills.shared.alerting import send_alert, AlertLevel
        await send_alert(
            "Model Retrain Due",
            f"Last retrain was {RETRAIN_INTERVAL_DAYS}+ days ago. "
            f"Run: cd CS_Multi_Model_Trading_System && python main.py compare",
            AlertLevel.INFO,
        )

    # 5. Log learning progress
    all_scores = scorer.get_all_agent_scores()
    status = adapter.get_status()

    log_lines = ["Feedback loop results:"]
    for agent, score_data in sorted(all_scores.items()):
        adjustment = status.get("analyst_adjustments", {}).get(
            agent.replace("-analyst", ""), 1.0
        )
        log_lines.append(
            f"  {agent:<25} score={score_data['avg_score']:.3f} "
            f"(n={score_data['n_scored']}, trend={score_data['trend']:+.3f}) "
            f"weight_mult={adjustment:.3f}"
        )

    for line in log_lines:
        logger.info(line)

    audit_log("feedback", "loop_complete", {
        "portfolio_return": portfolio_return,
        "market_return": market_return,
        "n_agents_scored": len(all_scores),
        "update_count": status.get("update_count", 0),
    })

    logger.info("=== FEEDBACK LOOP COMPLETE ===")

    return {
        "portfolio_return": portfolio_return,
        "market_return": market_return,
        "agent_scores": all_scores,
        "weight_status": status,
        "retrain_due": retrain_needed,
    }


async def _get_recent_return() -> float:
    """Get portfolio return over the scoring horizon."""
    try:
        from skills.pnl.tracker import get_pnl_tracker
        tracker = get_pnl_tracker()
        returns = tracker.get_daily_returns(10)
        if len(returns) >= 2:
            # Compound last 10 days
            import numpy as np
            return float(np.prod([1 + r for r in returns[-10:]]) - 1)
    except Exception:
        pass
    return 0.0


async def _get_market_return() -> float:
    """Get SPY return over the scoring horizon."""
    try:
        from skills.market_data import get_data_provider
        provider = get_data_provider()
        snaps = await provider.get_snapshots(["SPY"], feed="iex")
        spy = snaps.get("SPY")
        if spy:
            # change_pct is daily, we want ~10 day
            # Rough approximation: daily * 10 (not compounded, but close enough for scoring)
            return spy.change_pct / 100 * 10
    except Exception:
        pass
    return 0.0


def _check_retrain_due() -> bool:
    """Check if ML model retraining is due."""
    state = safe_load_state(RETRAIN_STATE_FILE, {"last_retrain": None})
    last = state.get("last_retrain")
    if not last:
        return True

    try:
        last_date = datetime.fromisoformat(last)
        days_since = (datetime.now(timezone.utc) - last_date).days
        return days_since >= RETRAIN_INTERVAL_DAYS
    except Exception:
        return True


def _get_last_retrain_date() -> str:
    state = safe_load_state(RETRAIN_STATE_FILE, {"last_retrain": "never"})
    return state.get("last_retrain", "never")


def mark_retrain_complete():
    """Called after successful model retraining."""
    safe_save_state(RETRAIN_STATE_FILE, {
        "last_retrain": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("Model retrain marked complete")
