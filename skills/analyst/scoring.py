"""
Deterministic conviction scoring for analyst personalities.

No LLM involved — pure quantitative signal extraction.
Each signal function returns a score between 0.0 and 1.0.

The personality_conviction() function is the main entry point:
it takes a personality's signal_weights and computes a weighted
conviction score from all available signals.
"""
import numpy as np

from skills.shared import get_logger

logger = get_logger("analyst.scoring")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


# ── Individual Signal Scores ────────────────────────────────────────────

def score_model_dispersion(predictions: dict[str, float]) -> float:
    """
    High dispersion in model scores = strong alpha signal.
    If all stocks score similarly, the model isn't finding much.
    """
    if not predictions or len(predictions) < 10:
        return 0.5
    scores = np.array(list(predictions.values()))
    spread = np.percentile(scores, 90) - np.percentile(scores, 10)
    return _clamp(spread / 0.10)


def score_sentiment(aggregate_score: float) -> float:
    """Positive sentiment → higher score."""
    return _clamp((aggregate_score + 1.0) / 2.0)


def score_breadth(advance_pct: float) -> float:
    """Market breadth: >65% advancing = strong."""
    return _clamp(advance_pct)


def score_vol_regime(vix_level: float, bias: str = "neutral") -> float:
    """
    VIX-based regime score.
    Bull/neutral bias: low VIX = high score (favorable)
    Bear bias: high VIX = high score (danger signal)
    """
    if bias == "bear":
        return _clamp((vix_level - 10.0) / 30.0)
    return _clamp(1.0 - (vix_level - 10.0) / 30.0)


def score_credit_stress(credit_spread: float) -> float:
    """
    Credit spread change. Negative = tightening = stress.
    -0.01 → 1.0 (stress), 0 → 0.5 (neutral), +0.01 → 0.0 (easing)
    """
    return _clamp(0.5 - credit_spread * 50)


def score_drawdown_proximity(
    current_drawdown: float, max_threshold: float = -0.08
) -> float:
    """How close to drawdown limit. 0% → 0.0, at threshold → 1.0."""
    if max_threshold >= 0:
        return 0.0
    return _clamp(current_drawdown / max_threshold)


# ── Signal Registry ─────────────────────────────────────────────────────

def compute_all_signals(
    predictions: dict[str, float],
    regime: dict,
    breadth: dict,
    sentiment: dict,
    portfolio_state: dict,
    bias: str = "neutral",
) -> dict[str, float]:
    """
    Compute all available signal scores.

    Returns dict mapping signal_name -> score (0 to 1).
    This is the raw input that personality weights are applied to.
    """
    vix = regime.get("vix_level", 20.0)

    return {
        "model_dispersion": score_model_dispersion(predictions),
        "breadth": score_breadth(breadth.get("advance_pct", 0.5)),
        "sentiment": score_sentiment(sentiment.get("aggregate_score", 0)),
        "vol_regime": score_vol_regime(vix, bias),
        "credit_stress": score_credit_stress(regime.get("credit_spread", 0)),
        "drawdown_proximity": score_drawdown_proximity(
            portfolio_state.get("current_drawdown", 0.0)
        ),
    }


# ── Personality-Based Conviction ─────────────────────────────────────────

def personality_conviction(
    personality: dict,
    predictions: dict[str, float],
    regime: dict,
    breadth: dict,
    sentiment: dict,
    portfolio_state: dict,
) -> tuple[float, list[dict]]:
    """
    Compute conviction for any analyst personality.

    The personality dict must contain:
    - signal_weights: dict mapping signal_name -> weight (must sum to 1.0)
    - bias: "bull", "bear", or "neutral"

    Returns (conviction: float, reasoning: list[dict])
    """
    bias = personality.get("bias", "neutral")
    signal_weights = personality["signal_weights"]

    # Compute raw signals
    signals = compute_all_signals(
        predictions, regime, breadth, sentiment, portfolio_state, bias
    )

    # Apply personality weights
    conviction = 0.0
    for signal_name, weight in signal_weights.items():
        score = signals.get(signal_name, 0.5)
        conviction += score * weight

    # Bias adjustment: bear analysts invert their conviction
    # (high conviction for a bear = more defensive)
    if bias == "bear":
        # Bear scores are already oriented correctly via score_vol_regime(bias="bear")
        pass

    conviction = _clamp(conviction)

    # Build reasoning sorted by contribution
    reasoning = []
    for signal_name, weight in sorted(
        signal_weights.items(),
        key=lambda x: -signals.get(x[0], 0) * x[1],
    ):
        score = signals.get(signal_name, 0.5)
        reasoning.append({
            "factor": signal_name,
            "score": round(score, 3),
            "weight": weight,
            "contribution": round(score * weight, 3),
        })

    return conviction, reasoning


def blend_model_predictions(
    model_predictions: dict[str, dict[str, float]],
    model_weights: dict[str, float],
) -> dict[str, float]:
    """
    Blend predictions from multiple ML models using personality-specific weights.

    Args:
        model_predictions: {"lightgbm": {ticker: score}, "tst": {...}, "crossmamba": {...}}
        model_weights: {"lightgbm": 0.5, "tst": 0.25, "crossmamba": 0.25}

    Returns:
        Blended predictions: {ticker: weighted_score}
    """
    all_tickers = set()
    for preds in model_predictions.values():
        all_tickers.update(preds.keys())

    blended = {}
    for ticker in all_tickers:
        score = 0.0
        total_weight = 0.0
        for model_name, weight in model_weights.items():
            if model_name in model_predictions and ticker in model_predictions[model_name]:
                score += model_predictions[model_name][ticker] * weight
                total_weight += weight
        if total_weight > 0:
            blended[ticker] = score / total_weight

    return blended


# ── Legacy compatibility (bull/bear still work) ─────────────────────────

def bull_conviction(predictions, regime, breadth, sentiment, portfolio_state):
    """Legacy bull conviction using momentum personality weights."""
    from .personalities import ANALYST_PERSONALITIES
    return personality_conviction(
        ANALYST_PERSONALITIES["momentum"],
        predictions, regime, breadth, sentiment, portfolio_state,
    )


def bear_conviction(predictions, regime, breadth, sentiment, portfolio_state):
    """Legacy bear conviction using risk personality weights."""
    from .personalities import ANALYST_PERSONALITIES
    return personality_conviction(
        ANALYST_PERSONALITIES["risk"],
        predictions, regime, breadth, sentiment, portfolio_state,
    )
