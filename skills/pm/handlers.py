"""
Portfolio Manager Agent handlers.

Manages the multi-PM + CIO decision pipeline:
1. Receives theses from all analyst personalities
2. Each PM personality proposes parameters
3. CIO selects the final proposal based on market conditions
4. Gates major changes behind approval workflow
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from skills.shared import get_logger, audit_log, approval_engine
from .models import PMDecision
from .resolution import resolve_theses

logger = get_logger("pm.handlers")

STATE_FILE = Path("./workspaces/pm-agent/state.json")


def _load_state() -> dict:
    from skills.shared.state import safe_load_state
    return safe_load_state(STATE_FILE, {"current_params": {}, "decision_history": [], "last_run": None})


def _save_state(state: dict) -> None:
    from skills.shared.state import safe_save_state
    safe_save_state(STATE_FILE, state)


async def resolve(
    analyst_theses: dict[str, dict],
    briefing: dict = None,
    portfolio_state: dict = None,
    mode: str = "daily",
    # Legacy support for old 2-arg signature
    bear_thesis: dict = None,
) -> dict:
    """
    Resolve analyst theses into final portfolio parameters.

    Args:
        analyst_theses: {personality_name: thesis_dict} from all analysts
                        OR a single bull thesis (legacy)
        briefing: MarketBriefing dict
        portfolio_state: current portfolio state for drawdown checks
        mode: "daily" or "intraday"
        bear_thesis: legacy parameter — if provided, analyst_theses is treated as bull_thesis

    Returns:
        PMDecision dict
    """
    # Legacy 2-thesis support
    if bear_thesis is not None:
        analyst_theses = {
            "momentum": analyst_theses,  # bull -> momentum
            "risk": bear_thesis,         # bear -> risk
        }

    state = _load_state()
    current_params = state.get("current_params", {})

    # Run multi-PM + CIO resolution
    result = resolve_theses(
        analyst_theses, current_params, briefing, portfolio_state, mode
    )

    # Generate decision ID
    state["_counter"] = state.get("_counter", 0) + 1
    decision_id = f"PMD-{state['_counter']:06d}"

    # Create approval request if needed
    approval_id = ""
    if result["requires_approval"]:
        changes_desc = ", ".join(
            f"{k}: {v['from']} → {v['to']}"
            for k, v in result["changes_from_current"].items()
        )
        approval_id = approval_engine.create_request(
            agent="pm-agent",
            action="set_params",
            description=f"PM Decision {decision_id}: {changes_desc}",
            amount=0.0,
            details={
                "decision_id": decision_id,
                "mode": mode,
                "final_params": result["final_params"],
                "resolution": result["resolution"],
            },
        )

    decision = PMDecision(
        decision_id=decision_id,
        mode=mode,
        final_params=result["final_params"],
        resolution=result["resolution"],
        changes_from_current=result["changes_from_current"],
        requires_approval=result["requires_approval"],
        approval_id=approval_id,
    )

    # Update state
    if not result["requires_approval"]:
        state["current_params"] = result["final_params"]

    state["decision_history"].append(decision.to_dict())
    state["decision_history"] = state["decision_history"][-30:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    res = result["resolution"]
    audit_log("pm-agent", "decision_made", {
        "decision_id": decision_id,
        "mode": mode,
        "selected_pm": res.get("selected_pm"),
        "method": res.get("method"),
        "avg_conviction": res.get("avg_conviction"),
        "requires_approval": result["requires_approval"],
        "n_long": result["final_params"].get("max_positions_long"),
        "n_short": result["final_params"].get("max_positions_short"),
        "leverage": result["final_params"].get("max_gross_leverage"),
    })

    logger.info(
        f"PM Decision {decision_id}: CIO selected {res.get('selected_pm')} PM | "
        f"n_long={result['final_params'].get('max_positions_long')}, "
        f"n_short={result['final_params'].get('max_positions_short')}, "
        f"leverage={result['final_params'].get('max_gross_leverage')}"
    )

    return decision.to_dict()


async def apply_approved_params(decision_id: str) -> dict:
    """Apply params from an approved decision."""
    state = _load_state()
    for d in reversed(state["decision_history"]):
        if d["decision_id"] == decision_id:
            state["current_params"] = d["final_params"]
            _save_state(state)
            return {"status": "applied", "params": d["final_params"]}
    return {"error": f"Decision {decision_id} not found"}


async def get_current_params() -> dict:
    return _load_state().get("current_params", {})


async def heartbeat() -> str:
    """PM agent status with multi-PM context."""
    state = _load_state()
    params = state.get("current_params", {})
    if not params:
        return "PM Agent: No active parameters. Run a daily cycle to initialize."

    last = state["decision_history"][-1] if state["decision_history"] else None

    lines = [
        "PM Agent Status",
        f"  Active params: n_long={params.get('max_positions_long')}, "
        f"n_short={params.get('max_positions_short')}, "
        f"leverage={params.get('max_gross_leverage')}",
        f"  Vol target: {params.get('target_annual_vol')}",
        f"  Weighting: {params.get('weighting')}",
        f"  Sector neutral: {params.get('sector_neutral')}",
    ]
    if last:
        res = last.get("resolution", {})
        lines.append(f"  Last decision: {last['decision_id']}")
        lines.append(f"    CIO selected: {res.get('selected_pm', 'N/A')} PM")
        lines.append(f"    Avg conviction: {res.get('avg_conviction', 0):.3f}")
        convictions = res.get("analyst_convictions", {})
        if convictions:
            conv_str = ", ".join(f"{k}={v:.2f}" for k, v in convictions.items())
            lines.append(f"    Analyst convictions: {conv_str}")
        proposals = res.get("pm_proposals", {})
        if proposals:
            for pm_name, p in proposals.items():
                lines.append(
                    f"    {pm_name} PM proposed: n_long={p.get('max_positions_long')}, "
                    f"n_short={p.get('max_positions_short')}, "
                    f"leverage={p.get('max_gross_leverage')}"
                )
    return "\n".join(lines)
