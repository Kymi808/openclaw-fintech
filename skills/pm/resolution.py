"""
Multi-PM resolution and CIO decision-making.

Three PM personalities each propose portfolio parameters by weighting
analyst theses differently. The CIO then selects the final parameters.

Resolution hierarchy:
1. Each PM blends all analyst theses using their own analyst_weights
2. CIO picks the PM whose proposal best fits current conditions
3. Safety overrides can force conservative positioning regardless
"""
from skills.shared import get_logger
from skills.analyst.presets import INTERPOLATABLE
from skills.analyst.personalities import PM_PERSONALITIES

logger = get_logger("pm.resolution")

# CIO safety override thresholds
SAFETY_OVERRIDE_VIX = 35.0  # Force conservative above this VIX
SAFETY_OVERRIDE_DRAWDOWN = -0.10  # Force conservative at this drawdown

# Approval thresholds for parameter changes
APPROVAL_THRESHOLDS = {
    "max_gross_leverage": 0.2,
    "max_positions_long": 5,
    "max_positions_short": 5,
    "target_annual_vol": 0.03,
    "sector_neutral": True,  # any toggle requires approval
}


def pm_propose(
    pm_personality: dict,
    analyst_theses: dict[str, dict],
    briefing: dict = None,
) -> dict:
    """
    A single PM proposes portfolio parameters by blending analyst theses.

    Args:
        pm_personality: PM personality dict with analyst_weights, leverage_bias, position_bias
        analyst_theses: {analyst_name: thesis_dict}

    Returns:
        Proposed parameters dict
    """
    analyst_weights = pm_personality["analyst_weights"]
    leverage_bias = pm_personality.get("leverage_bias", 1.0)
    position_bias = pm_personality.get("position_bias", 1.0)

    # Blend analyst recommended params, weighted by PM's analyst preferences
    blended = {}
    total_weight = 0.0

    for analyst_name, weight in analyst_weights.items():
        thesis = analyst_theses.get(analyst_name)
        if not thesis or "recommended_params" not in thesis:
            continue

        params = thesis["recommended_params"]
        conviction = thesis.get("conviction", 0.5)
        # Weight = PM's preference × analyst's own conviction
        effective_weight = weight * conviction
        total_weight += effective_weight

        for key in INTERPOLATABLE:
            if key in params:
                blended.setdefault(key, 0.0)
                blended[key] += params[key] * effective_weight

    # Normalize
    if total_weight > 0:
        for key in blended:
            blended[key] /= total_weight

    # Apply PM's bias
    if "max_gross_leverage" in blended:
        blended["max_gross_leverage"] *= leverage_bias
    for key in ("max_positions_long", "max_positions_short"):
        if key in blended:
            blended[key] = round(blended[key] * position_bias)

    # Round appropriately
    for key in blended:
        if key in ("max_positions_long", "max_positions_short"):
            blended[key] = max(1, round(blended[key]))
        else:
            blended[key] = round(blended[key], 4)

    # Non-numeric: majority vote from weighted analysts
    blended["weighting"] = _majority_vote(
        analyst_theses, analyst_weights, "weighting", "risk_parity"
    )
    blended["sector_neutral"] = _majority_vote_bool(
        analyst_theses, analyst_weights, "sector_neutral", True
    )

    return blended


def cio_decide(
    pm_proposals: dict[str, dict],
    analyst_theses: dict[str, dict],
    briefing: dict = None,
    portfolio_state: dict = None,
) -> tuple[str, dict, str]:
    """
    CIO selects the final parameters from PM proposals.

    Selection logic:
    1. Safety override: if VIX > 35 or drawdown > 10%, force conservative PM
    2. Otherwise: select PM based on market conditions
       - High VIX (>25) → conservative PM
       - Low VIX (<18) + positive breadth → aggressive PM
       - Default → balanced PM

    Returns:
        (selected_pm_name, final_params, rationale)
    """
    portfolio_state = portfolio_state or {}
    vix = 20.0
    vix_regime = "normal"
    advance_pct = 0.5

    if briefing:
        regime = briefing.get("regime", {})
        vix = regime.get("vix_level", 20.0)
        vix_regime = regime.get("vix_regime", "normal")
        advance_pct = briefing.get("breadth", {}).get("advance_pct", 0.5)

    drawdown = portfolio_state.get("current_drawdown", 0.0)

    # Extract HMM regime data (if available)
    hmm_regime = "unknown"
    hmm_confidence = 0.0
    hmm_probs = {}
    if briefing:
        regime = briefing.get("regime", {})
        hmm_regime = regime.get("hmm_regime", "unknown")
        hmm_confidence = regime.get("hmm_confidence", 0.0)
        hmm_probs = regime.get("hmm_probabilities", {})

    # Safety override (hard limits, cannot be overridden)
    if vix > SAFETY_OVERRIDE_VIX or drawdown < SAFETY_OVERRIDE_DRAWDOWN:
        selected = "conservative"
        rationale = (
            f"SAFETY OVERRIDE: VIX={vix:.1f}, drawdown={drawdown:.1%}. "
            f"Forcing conservative positioning."
        )
        logger.warning(f"CIO SAFETY OVERRIDE: {rationale}")

    # HMM-based selection (when available and confident)
    # HMM provides probability distributions — strictly more informative than VIX alone
    elif hmm_regime != "unknown" and hmm_confidence > 0.6:
        if hmm_regime == "bear" or hmm_probs.get("bear", 0) > 0.5:
            selected = "conservative"
            rationale = (
                f"HMM regime: bear (P(bear)={hmm_probs.get('bear', 0):.0%}, "
                f"confidence={hmm_confidence:.0%}). Conservative PM selected."
            )
        elif hmm_regime == "bull" and hmm_probs.get("bull", 0) > 0.65:
            selected = "aggressive"
            rationale = (
                f"HMM regime: bull (P(bull)={hmm_probs.get('bull', 0):.0%}). "
                f"Aggressive PM selected."
            )
        else:
            selected = "balanced"
            rationale = (
                f"HMM regime: {hmm_regime} (mixed: bull={hmm_probs.get('bull', 0):.0%}, "
                f"bear={hmm_probs.get('bear', 0):.0%}). Balanced PM selected."
            )

    # Fallback: VIX/breadth-based selection (when HMM unavailable)
    elif vix_regime in ("elevated", "crisis") or advance_pct < 0.35:
        selected = "conservative"
        rationale = (
            f"Risk-off conditions: VIX {vix_regime} ({vix:.1f}), "
            f"breadth {advance_pct:.0%}. Conservative PM selected."
        )

    elif vix < 18 and advance_pct > 0.60:
        selected = "aggressive"
        rationale = (
            f"Risk-on conditions: VIX {vix:.1f} (low), "
            f"breadth {advance_pct:.0%} (strong). Aggressive PM selected."
        )

    else:
        selected = "balanced"
        rationale = (
            f"Mixed conditions: VIX {vix:.1f} ({vix_regime}), "
            f"breadth {advance_pct:.0%}. Balanced PM selected."
        )

    final_params = pm_proposals.get(selected, pm_proposals.get("balanced", {}))

    logger.info(f"CIO selected {selected} PM: {rationale}")
    return selected, final_params, rationale


def resolve_theses(
    analyst_theses: dict[str, dict],
    current_params: dict,
    briefing: dict = None,
    portfolio_state: dict = None,
    mode: str = "daily",
) -> dict:
    """
    Full resolution pipeline: all PMs propose → CIO decides → approval check.

    This replaces the old bull/bear-only resolution.
    """
    # Legacy support: if we get bull_thesis/bear_thesis format, convert
    if "bull" in analyst_theses or "bear" in analyst_theses:
        # Map to personality names
        mapped = {}
        if "bull" in analyst_theses:
            mapped["momentum"] = analyst_theses["bull"]
        if "bear" in analyst_theses:
            mapped["risk"] = analyst_theses["bear"]
        analyst_theses = mapped

    # 1. Each PM proposes parameters (with adaptive weight adjustments)
    pm_proposals = {}
    for pm_name, pm_personality in PM_PERSONALITIES.items():
        # Apply adaptive feedback to PM weights
        try:
            from skills.feedback.adapter import get_weight_adapter
            adapter = get_weight_adapter()
            pm_personality = adapter.apply_to_pm_personality(pm_personality, pm_name)
        except Exception:
            pass  # feedback not available yet

        pm_proposals[pm_name] = pm_propose(
            pm_personality, analyst_theses, briefing
        )

    # 2. CIO selects
    selected_pm, final_params, cio_rationale = cio_decide(
        pm_proposals, analyst_theses, briefing, portfolio_state
    )

    # 3. Compute conviction summary
    convictions = {
        name: thesis.get("conviction", 0)
        for name, thesis in analyst_theses.items()
    }
    avg_conviction = sum(convictions.values()) / max(len(convictions), 1)

    # 4. Detect changes
    changes = _detect_changes(current_params, final_params)

    # 5. Check approval
    requires_approval = _needs_approval(changes, current_params, mode)

    resolution = {
        "analyst_convictions": convictions,
        "avg_conviction": round(avg_conviction, 3),
        "pm_proposals": {
            name: {k: v for k, v in params.items() if k in ("max_positions_long", "max_positions_short", "max_gross_leverage")}
            for name, params in pm_proposals.items()
        },
        "selected_pm": selected_pm,
        "method": f"cio_select_{selected_pm}",
        "rationale": cio_rationale,
    }

    return {
        "final_params": final_params,
        "resolution": resolution,
        "changes_from_current": changes,
        "requires_approval": requires_approval,
    }


def _majority_vote(theses, weights, param, default):
    """Weighted majority vote for a categorical parameter."""
    votes = {}
    for name, w in weights.items():
        thesis = theses.get(name)
        if thesis and "recommended_params" in thesis:
            val = thesis["recommended_params"].get(param, default)
            votes[val] = votes.get(val, 0) + w * thesis.get("conviction", 0.5)
    return max(votes, key=votes.get) if votes else default


def _majority_vote_bool(theses, weights, param, default):
    """Weighted majority vote for a boolean parameter."""
    true_weight = 0.0
    false_weight = 0.0
    for name, w in weights.items():
        thesis = theses.get(name)
        if thesis and "recommended_params" in thesis:
            val = thesis["recommended_params"].get(param, default)
            ew = w * thesis.get("conviction", 0.5)
            if val:
                true_weight += ew
            else:
                false_weight += ew
    return true_weight >= false_weight


def _detect_changes(current, proposed):
    changes = {}
    for key, new_val in proposed.items():
        old_val = current.get(key)
        if old_val is None:
            changes[key] = {"from": None, "to": new_val}
        elif old_val != new_val:
            changes[key] = {"from": old_val, "to": new_val}
    return changes


def _needs_approval(changes, current_params, mode):
    if not current_params:
        return True
    for key, change in changes.items():
        threshold = APPROVAL_THRESHOLDS.get(key)
        if threshold is None:
            continue
        if isinstance(threshold, bool):
            if change["from"] != change["to"]:
                return True
        else:
            old = change["from"]
            new = change["to"]
            if old is not None and abs(new - old) > threshold:
                return True
    if mode == "intraday":
        return False
    return False
