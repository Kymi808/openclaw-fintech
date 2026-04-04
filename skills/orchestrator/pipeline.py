"""
Orchestrator pipeline — wires all agents together.

Daily cycle:
  1. Intel gathers market briefing
  2. ML models generate predictions (CrossMamba primary)
  3. All 5 analyst personalities form theses in parallel
  4. 3 PM personalities propose parameters, CIO selects
  5. Execution agent places trades

Intraday cycle: same pipeline, different mode + auto-approval
"""
import asyncio
from datetime import datetime, timezone

from skills.shared import get_logger, audit_log
from skills.shared.alerting import alert_pipeline_failure, alert_daily_summary
from skills.intel.handlers import gather_briefing
from skills.analyst.handlers import form_all_theses
from skills.pm.handlers import resolve
from skills.execution.handlers import execute_daily, execute_intraday, close_intraday_positions
from skills.execution.session import (
    get_session, is_market_open, should_close_intraday, MarketSession,
)
from .checkpoint import CheckpointManager, PipelineStep, generate_run_id

logger = get_logger("orchestrator.pipeline")

_checkpoint_mgr = CheckpointManager()

# Whether to use real ML models or dummy predictions
USE_REAL_MODELS = True

# Rate limiting: minimum seconds between pipeline runs
MIN_CYCLE_INTERVAL = 300  # 5 minutes
_last_daily_run: float = 0.0
_last_intraday_run: float = 0.0


def _load_real_predictions(pm_params: dict = None) -> dict[str, float]:
    """Load predictions from the best available trained model."""
    import platform
    from skills.signals.bridge import generate_predictions, MODEL_PATHS

    # CrossMamba/TST segfault on macOS ARM (Apple Silicon) during inference
    # Use LightGBM on Mac, CrossMamba on Linux (production servers)
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        model_priority = ("lightgbm",)
        logger.info("macOS ARM detected — using LightGBM (CrossMamba not supported on Apple Silicon)")
    else:
        model_priority = ("crossmamba", "tst", "lightgbm")

    for model_name in model_priority:
        model_path = MODEL_PATHS.get(model_name)
        if not model_path or not model_path.exists():
            continue
        try:
            predictions, info = generate_predictions(model_name, pm_params)
            logger.info(
                f"{model_name} predictions: {info.get('n_tickers', 0)} tickers, "
                f"regime={info.get('regime_score', 0):.3f}"
            )
            return predictions
        except Exception as e:
            logger.warning(f"{model_name} failed: {e}, trying next model")

    logger.error("All models failed, falling back to dummy predictions")
    return _dummy_predictions()


async def run_daily_cycle(predictions: dict[str, float] = None) -> dict:
    """
    Full daily rebalancing pipeline with multi-agent debate.

    Pipeline:
    1. Intel → MarketBriefing
    2. ML models generate predictions (CrossMamba primary)
    3. 5 Analysts (parallel) → {momentum, value, macro, sentiment, risk} theses
    4. 3 PMs propose → CIO selects → PMDecision
    5. Execution → trades on Alpaca

    Args:
        predictions: ticker -> ML ensemble score. If None, loads from CrossMamba model.
    """
    import time
    global _last_daily_run
    now = time.time()
    if now - _last_daily_run < MIN_CYCLE_INTERVAL:
        elapsed = int(now - _last_daily_run)
        remaining = MIN_CYCLE_INTERVAL - elapsed
        return {
            "status": "rate_limited",
            "message": f"Daily cycle ran {elapsed}s ago. Wait {remaining}s (cooldown: {MIN_CYCLE_INTERVAL}s).",
        }
    _last_daily_run = now

    start = datetime.now(timezone.utc)
    run_id = generate_run_id("daily")
    logger.info(f"=== DAILY CYCLE START [{run_id}] ===")

    # Check for incomplete previous runs
    incomplete = _checkpoint_mgr.get_incomplete()
    for inc in incomplete:
        can_resume, reason = _checkpoint_mgr.can_resume(inc)
        if not can_resume:
            logger.error(f"Previous run {inc.run_id} incomplete: {reason}")
            await alert_pipeline_failure("daily", inc.current_step, reason)
            _checkpoint_mgr.mark_failed(inc.run_id, reason)

    # Create checkpoint
    _checkpoint_mgr.create(run_id, "daily")

    if predictions is None:
        if USE_REAL_MODELS:
            predictions = _load_real_predictions()
        else:
            predictions = _dummy_predictions()

    # Cache predictions so intraday cycles can reuse them
    _cache_predictions(predictions)

    result = {
        "cycle": "daily",
        "run_id": run_id,
        "start": start.isoformat(),
        "predictions_count": len(predictions),
    }

    try:
        # 1. Market Intelligence
        logger.info("Step 1: Gathering market intelligence...")
        briefing = await gather_briefing()
        result["briefing"] = briefing
        _checkpoint_mgr.update(run_id, PipelineStep.INTEL_DONE, briefing=briefing)
        logger.info(f"  {briefing.get('macro_summary', '')}")

        # 2. All Analyst Personalities (parallel)
        logger.info("Step 2: 5 analysts forming theses...")
        portfolio_state = {"current_drawdown": 0.0}
        analyst_theses = await form_all_theses(briefing, predictions, portfolio_state)
        result["analyst_theses"] = analyst_theses
        _checkpoint_mgr.update(run_id, PipelineStep.ANALYSTS_DONE, analyst_theses=analyst_theses)

        for name, thesis in analyst_theses.items():
            conv = thesis.get("conviction", 0)
            params = thesis.get("recommended_params", {})
            logger.info(
                f"  {name:>10}: conviction={conv:.3f}, "
                f"n_long={params.get('max_positions_long')}, "
                f"n_short={params.get('max_positions_short')}"
            )

        # 3. Multi-PM Resolution + CIO
        logger.info("Step 3: 3 PMs proposing, CIO deciding...")
        decision = await resolve(
            analyst_theses, briefing=briefing, portfolio_state=portfolio_state, mode="daily"
        )
        result["decision"] = decision
        _checkpoint_mgr.update(run_id, PipelineStep.PM_DONE, decision=decision)

        res = decision.get("resolution", {})
        params = decision.get("final_params", {})
        logger.info(
            f"  CIO selected: {res.get('selected_pm')} PM | "
            f"n_long={params.get('max_positions_long')}, "
            f"n_short={params.get('max_positions_short')}, "
            f"leverage={params.get('max_gross_leverage')}"
        )

        for pm_name, proposal in res.get("pm_proposals", {}).items():
            logger.info(
                f"    {pm_name} PM: n_long={proposal.get('max_positions_long')}, "
                f"n_short={proposal.get('max_positions_short')}, "
                f"leverage={proposal.get('max_gross_leverage')}"
            )

        # 4. Approval check
        if decision.get("requires_approval"):
            result["status"] = "awaiting_approval"
            result["approval_id"] = decision.get("approval_id")

            lines = [
                f"PM Decision {decision['decision_id']} requires approval.",
                "",
                "  Analyst Convictions:",
            ]
            for name, conv in res.get("analyst_convictions", {}).items():
                lines.append(f"    {name:>10}: {conv:.3f}")

            lines.append("")
            lines.append("  PM Proposals:")
            for pm_name, proposal in res.get("pm_proposals", {}).items():
                marker = " <-- SELECTED" if pm_name == res.get("selected_pm") else ""
                lines.append(
                    f"    {pm_name:>12}: n_long={proposal.get('max_positions_long')}, "
                    f"n_short={proposal.get('max_positions_short')}, "
                    f"leverage={proposal.get('max_gross_leverage')}{marker}"
                )

            lines.extend([
                "",
                f"  CIO rationale: {res.get('rationale', '')}",
                "",
                f"  Final: n_long={params.get('max_positions_long')}, "
                f"n_short={params.get('max_positions_short')}, "
                f"leverage={params.get('max_gross_leverage')}, "
                f"vol_target={params.get('target_annual_vol')}",
                "",
                f"  Reply 'approve {decision.get('approval_id')}' to execute.",
            ])

            result["message"] = "\n".join(lines)
            return result

        # 5. Execution
        _checkpoint_mgr.update(run_id, PipelineStep.EXECUTION_STARTED)
        logger.info("Step 4: Executing trades...")
        execution = await execute_daily(decision, predictions)
        result["execution"] = execution
        result["status"] = "executed"

        _checkpoint_mgr.mark_complete(run_id, execution)

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        result["elapsed_seconds"] = round(elapsed, 1)

        audit_log("orchestrator", "daily_cycle_complete", {
            "run_id": run_id,
            "elapsed": elapsed,
            "status": result["status"],
            "selected_pm": res.get("selected_pm"),
            "n_long": params.get("max_positions_long"),
            "n_short": params.get("max_positions_short"),
        })

        logger.info(f"=== DAILY CYCLE COMPLETE [{run_id}] ({elapsed:.1f}s) ===")

    except Exception as e:
        logger.error(f"Daily cycle failed [{run_id}]: {e}")
        _checkpoint_mgr.mark_failed(run_id, str(e))
        await alert_pipeline_failure("daily", "unknown", str(e))
        result["status"] = "failed"
        result["error"] = str(e)

    return result


async def run_intraday_cycle(predictions: dict[str, float] = None) -> dict:
    """Intraday adjustment pipeline — same structure, different mode."""
    import time
    global _last_intraday_run
    now = time.time()
    if now - _last_intraday_run < MIN_CYCLE_INTERVAL:
        elapsed = int(now - _last_intraday_run)
        return {"status": "rate_limited", "message": f"Intraday cycle ran {elapsed}s ago."}
    _last_intraday_run = now

    if not is_market_open():
        return {"status": "market_closed", "session": get_session().value}

    if should_close_intraday():
        return await close_intraday_positions()

    logger.info("=== INTRADAY CYCLE START ===")

    # Load predictions (reuse daily predictions for model-aligned intraday)
    if predictions is None:
        if USE_REAL_MODELS:
            predictions = _load_real_predictions()
        else:
            predictions = _dummy_predictions()

    # Run agent debate for parameter adjustments
    briefing = await gather_briefing()
    portfolio_state = {"current_drawdown": 0.0}
    analyst_theses = await form_all_theses(briefing, predictions, portfolio_state)

    decision = await resolve(
        analyst_theses, briefing=briefing, portfolio_state=portfolio_state, mode="intraday"
    )

    if decision.get("requires_approval"):
        return {
            "status": "awaiting_approval",
            "decision": decision,
        }

    # Intraday execution (position adjustments)
    execution = await execute_intraday(decision, predictions)

    # Also scan for model-aligned intraday setups
    from skills.intraday.handlers import scan_for_setups
    scan_result = await scan_for_setups(model_predictions=predictions)

    logger.info("=== INTRADAY CYCLE COMPLETE ===")
    return {
        "status": "complete",
        "cycle": "intraday",
        "decision": decision,
        "execution": execution,
        "intraday_scan": scan_result,
    }


def _cache_predictions(predictions: dict[str, float]):
    """Cache daily predictions so intraday cycles can reuse them."""
    from skills.shared.state import safe_save_state
    from pathlib import Path
    safe_save_state(
        Path("./data/cached_predictions.json"),
        {"predictions": predictions, "date": datetime.now(timezone.utc).isoformat()},
    )


def _dummy_predictions() -> dict[str, float]:
    """Dummy predictions for testing without the ML model."""
    import numpy as np
    rng = np.random.RandomState(42)
    symbols = [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
        "JPM", "V", "UNH", "JNJ", "XOM", "PG", "MA", "HD", "CVX", "MRK",
        "ABBV", "PEP", "KO", "COST", "AVGO", "LLY", "WMT", "MCD", "CSCO",
        "TMO", "ACN", "ABT", "DHR", "NEE", "TXN", "UPS", "RTX",
        "CRM", "INTC", "NFLX", "AMD", "QCOM", "ORCL", "LOW", "GS", "BA",
        "CAT", "DE", "SBUX", "PLD", "AMAT",
    ]
    scores = rng.normal(0, 0.05, len(symbols))
    return dict(zip(symbols, scores))
