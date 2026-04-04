"""
Analyst Agent handlers.

Supports multiple analyst personalities, each with different signal weights,
model preferences, and risk profiles. All personalities use the same
deterministic scoring functions — only the weights differ.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from skills.shared import get_logger, audit_log
from .models import Thesis, ReasoningPoint
from .scoring import personality_conviction
from .presets import interpolate_params_from_profile
from .personalities import ANALYST_PERSONALITIES, RISK_PROFILES

# Adaptive feedback — weights evolve based on performance
_feedback_initialized = False

logger = get_logger("analyst.handlers")

WORKSPACE = Path("./workspaces")


def _state_file(personality_name: str) -> Path:
    return WORKSPACE / f"{personality_name}-analyst" / "state.json"


def _load_state(personality_name: str) -> dict:
    from skills.shared.state import safe_load_state
    return safe_load_state(_state_file(personality_name), {"thesis_history": [], "last_run": None})


def _save_state(personality_name: str, state: dict) -> None:
    from skills.shared.state import safe_save_state
    safe_save_state(_state_file(personality_name), state)


async def form_thesis(
    personality_name: str,
    briefing: dict,
    predictions: dict[str, float],
    portfolio_state: dict,
) -> dict:
    """
    Form a thesis using a specific analyst personality.

    Args:
        personality_name: Key in ANALYST_PERSONALITIES (e.g., "momentum", "risk")
                          Also supports legacy "bull" and "bear" names.
        briefing: MarketBriefing dict from intel agent
        predictions: ticker -> ensemble score from ML models
        portfolio_state: current portfolio state

    Returns:
        Thesis dict with conviction, recommended_params, reasoning, risk_flags
    """
    # Legacy support: map bull/bear to personality names
    if personality_name == "bull":
        personality_name = "momentum"
    elif personality_name == "bear":
        personality_name = "risk"

    personality = ANALYST_PERSONALITIES.get(personality_name)
    if not personality:
        return {"error": f"Unknown personality: {personality_name}"}

    # Apply adaptive weight adjustments from feedback loop
    try:
        from skills.feedback.adapter import get_weight_adapter
        adapter = get_weight_adapter()
        personality = adapter.apply_to_analyst_personality(personality, personality_name)
    except Exception:
        pass  # feedback not available yet — use base weights

    regime = briefing.get("regime", {})
    breadth = briefing.get("breadth", {})
    sentiment = briefing.get("sentiment", {})

    # Compute conviction using personality-specific weights
    conviction, raw_reasoning = personality_conviction(
        personality, predictions, regime, breadth, sentiment, portfolio_state
    )

    # Interpolate parameter recommendations based on conviction + risk profile
    risk_profile = personality.get("risk_profile", "moderate")
    recommended_params = interpolate_params_from_profile(conviction, risk_profile)

    # Build structured reasoning
    reasoning = []
    for raw in raw_reasoning:
        obs = _generate_observation(raw, regime, breadth, sentiment, personality)
        reasoning.append(ReasoningPoint(
            factor=raw["factor"],
            observation=obs["observation"],
            implication=obs["implication"],
            weight=raw["weight"],
        ))

    # Identify risk flags
    risk_flags = _identify_risks(regime, breadth, sentiment, portfolio_state)

    thesis = Thesis(
        agent=personality_name,
        conviction=round(conviction, 3),
        recommended_params=recommended_params,
        reasoning=reasoning,
        risk_flags=risk_flags,
    )

    # Persist
    state = _load_state(personality_name)
    state["last_thesis"] = thesis.to_dict()
    state["thesis_history"].append(thesis.to_dict())
    state["thesis_history"] = state["thesis_history"][-20:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(personality_name, state)

    # Record prediction for feedback scoring
    try:
        from skills.feedback.scorer import OutcomeScorer
        OutcomeScorer().record_analyst_thesis(
            personality_name, thesis.conviction, personality.get("bias", "neutral")
        )
    except Exception:
        pass

    audit_log(f"{personality_name}-analyst", "thesis_formed", {
        "conviction": thesis.conviction,
        "n_long": recommended_params.get("max_positions_long"),
        "n_short": recommended_params.get("max_positions_short"),
        "leverage": recommended_params.get("max_gross_leverage"),
        "risk_flags": risk_flags,
    })

    logger.info(
        f"{personality_name.upper()} thesis: conviction={thesis.conviction:.3f}, "
        f"n_long={recommended_params.get('max_positions_long')}, "
        f"n_short={recommended_params.get('max_positions_short')}"
    )

    return thesis.to_dict()


async def form_all_theses(
    briefing: dict,
    predictions: dict[str, float],
    portfolio_state: dict,
) -> dict[str, dict]:
    """
    Run ALL analyst personalities in parallel.

    Returns dict mapping personality_name -> thesis dict.
    """
    import asyncio
    tasks = {}
    for name in ANALYST_PERSONALITIES:
        tasks[name] = form_thesis(name, briefing, predictions, portfolio_state)

    results = await asyncio.gather(*tasks.values())
    return dict(zip(tasks.keys(), results))


def _generate_observation(
    raw: dict,
    regime: dict,
    breadth: dict,
    sentiment: dict,
    personality: dict,
) -> dict:
    """Generate human-readable observation for a reasoning point."""
    factor = raw["factor"]
    score = raw["score"]
    name = personality.get("name", "Analyst")

    observations = {
        "model_dispersion": {
            "observation": f"Model score spread: {score:.2f} — "
                + ("strong signal" if score > 0.6 else "weak differentiation"),
            "implication": "Clear long/short candidates identified"
                if score > 0.6 else "Limited alpha opportunity in current rankings",
        },
        "breadth": {
            "observation": f"{breadth.get('advance_pct', 0.5):.0%} of stocks advancing",
            "implication": "Broad participation supports risk-on"
                if score > 0.6 else "Narrow market — concentration risk",
        },
        "sentiment": {
            "observation": f"News sentiment: {sentiment.get('aggregate_score', 0):+.3f} "
                f"({sentiment.get('n_articles', 0)} articles)",
            "implication": "Positive information flow"
                if score > 0.6 else "Neutral/negative news environment",
        },
        "vol_regime": {
            "observation": f"VIX proxy: {regime.get('vix_level', 20):.1f} "
                f"({regime.get('vix_regime', 'normal')})",
            "implication": "Low vol favors positioning"
                if score > 0.6 else f"Elevated vol ({regime.get('vix_regime')}) — caution warranted",
        },
        "credit_stress": {
            "observation": f"Credit spread: {regime.get('credit_spread', 0):+.4f}",
            "implication": "Credit conditions healthy"
                if score < 0.5 else "Credit deterioration — risk-off signal",
        },
        "drawdown_proximity": {
            "observation": f"Drawdown proximity: {score:.2f}",
            "implication": "Well within risk tolerance"
                if score < 0.4 else "Approaching drawdown limit — reduce exposure",
        },
    }

    return observations.get(factor, {
        "observation": f"{factor}: {score:.3f}",
        "implication": "Signal contributes to thesis",
    })


def _identify_risks(regime, breadth, sentiment, portfolio_state) -> list[str]:
    """Identify active risk flags from current conditions."""
    flags = []
    vix = regime.get("vix_level", 20)
    if vix > 25:
        flags.append("elevated_vix")
    if vix > 35:
        flags.append("crisis_vix")
    if breadth.get("advance_pct", 0.5) < 0.30:
        flags.append("negative_breadth")
    if sentiment.get("aggregate_score", 0) < -0.2:
        flags.append("negative_sentiment")
    if regime.get("credit_spread", 0) < -0.005:
        flags.append("credit_tightening")
    dd = portfolio_state.get("current_drawdown", 0)
    if dd < -0.05:
        flags.append("drawdown_warning")
    return flags
