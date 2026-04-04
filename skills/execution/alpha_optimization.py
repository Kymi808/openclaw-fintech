"""
Alpha and return optimization — institutional techniques for maximizing
risk-adjusted returns.

Implements:
1. Kelly Criterion — mathematically optimal position sizing
2. Alpha decay tracking — trade urgently for fast-decaying signals
3. Transaction cost-aware filtering — skip trades where cost > alpha
4. Dynamic ensemble weighting — weight models by recent predictive power
5. Implementation shortfall estimation — estimate execution cost before trading

References:
- Kelly (1956), "A New Interpretation of Information Rate"
- Grinold & Kahn, "Active Portfolio Management" (2nd ed.)
- Almgren & Chriss (2001), "Optimal Execution of Portfolio Transactions"
"""
import numpy as np
import pandas as pd
from typing import Optional

from skills.shared import get_logger

logger = get_logger("execution.alpha_optimization")


# ── 1. Kelly Criterion Position Sizing ───────────────────────────────────

def kelly_fraction(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    kelly_multiplier: float = 0.25,
) -> float:
    """
    Compute the Kelly-optimal fraction of capital to risk.

    Full Kelly is too aggressive for real trading (assumes exact knowledge
    of probabilities). We use fractional Kelly (typically 1/4 Kelly)
    which captures ~75% of the growth rate with much lower variance.

    Args:
        win_rate: probability of profit (0 to 1)
        avg_win: average winning trade return (positive)
        avg_loss: average losing trade return (positive, will be negated)
        kelly_multiplier: fraction of Kelly to use (0.25 = quarter Kelly)

    Returns:
        Optimal fraction of equity to allocate (0.0 to ~0.20)
    """
    if avg_loss <= 0 or avg_win <= 0 or win_rate <= 0 or win_rate >= 1:
        return 0.02  # default 2% if inputs invalid

    # Kelly formula: f* = (p * b - q) / b
    # where p = win_rate, q = 1-p, b = avg_win/avg_loss
    b = avg_win / avg_loss
    q = 1 - win_rate
    full_kelly = (win_rate * b - q) / b

    if full_kelly <= 0:
        return 0.0  # negative edge = don't trade

    # Fractional Kelly (safer)
    fraction = full_kelly * kelly_multiplier

    # Cap at 20% of equity per position (even Kelly can be extreme)
    return min(0.20, max(0.0, fraction))


def kelly_position_sizes(
    predictions: dict[str, float],
    model_stats: dict = None,
    account_equity: float = 100_000,
    kelly_mult: float = 0.25,
) -> dict[str, float]:
    """
    Compute Kelly-optimal position sizes for a set of stock predictions.

    Stocks with higher predicted returns get larger positions,
    scaled by the model's historical win rate and avg win/loss.

    Returns dict of {symbol: position_notional}
    """
    if not model_stats:
        # Default stats if no historical data yet
        model_stats = {"win_rate": 0.52, "avg_win": 0.03, "avg_loss": 0.025}

    base_fraction = kelly_fraction(
        model_stats["win_rate"],
        model_stats["avg_win"],
        model_stats["avg_loss"],
        kelly_mult,
    )

    if base_fraction <= 0:
        return {}

    # Scale by prediction strength (higher conviction = larger position)
    pred_series = pd.Series(predictions)
    abs_preds = pred_series.abs()
    max_pred = abs_preds.max() if abs_preds.max() > 0 else 1.0

    sizes = {}
    for symbol, pred in predictions.items():
        # Scale: full Kelly for strongest signal, less for weaker
        conviction_scale = abs(pred) / max_pred
        position_frac = base_fraction * conviction_scale
        sizes[symbol] = round(position_frac * account_equity, 2)

    return sizes


# ── 2. Alpha Decay Tracking ─────────────────────────────────────────────

def estimate_alpha_decay(
    signal_returns: list[tuple[float, float]],
) -> dict:
    """
    Estimate how quickly alpha decays after signal generation.

    Tracks the correlation between signal strength and forward returns
    at different horizons (1d, 2d, 5d, 10d).

    If alpha decays fast (high at 1d, zero at 5d), we should trade immediately.
    If alpha decays slowly (stable across horizons), we can be patient.

    Args:
        signal_returns: list of (signal_value, forward_return_at_horizon)
                       for multiple historical signals

    Returns:
        {"half_life_days": float, "urgency": "immediate"|"normal"|"patient"}
    """
    if len(signal_returns) < 20:
        return {"half_life_days": 5.0, "urgency": "normal"}

    signals = np.array([s[0] for s in signal_returns])
    returns = np.array([s[1] for s in signal_returns])

    # IC at different lags
    ic = np.corrcoef(signals, returns)[0, 1] if len(signals) > 2 else 0

    if abs(ic) < 0.02:
        return {"half_life_days": 0.0, "urgency": "none"}  # no alpha

    # Estimate half-life from IC decay pattern
    # Simple heuristic: momentum signals decay in ~5d, fundamental in ~20d
    if abs(ic) > 0.1:
        half_life = 2.0  # strong signal = fast decay (momentum)
        urgency = "immediate"
    elif abs(ic) > 0.05:
        half_life = 5.0  # moderate signal
        urgency = "normal"
    else:
        half_life = 10.0  # weak signal = slow decay (fundamental)
        urgency = "patient"

    return {
        "half_life_days": half_life,
        "urgency": urgency,
        "ic": round(ic, 4),
    }


def adjust_for_decay(
    prediction: float,
    hours_since_signal: float,
    half_life_hours: float = 120,  # 5 trading days default
) -> float:
    """
    Discount a prediction based on time elapsed since signal generation.

    A signal generated 3 days ago is worth less than one generated today.
    """
    decay = 0.5 ** (hours_since_signal / half_life_hours)
    return prediction * decay


# ── 3. Transaction Cost-Aware Filtering ──────────────────────────────────

def filter_by_expected_profit(
    predictions: dict[str, float],
    account_equity: float,
    round_trip_cost_bps: float = 24.0,
    min_profit_multiple: float = 3.0,
) -> dict[str, float]:
    """
    Remove trades where expected profit doesn't sufficiently exceed cost.

    A trade with 0.5% expected return but 0.24% round-trip cost has
    only 0.26% net. At 3x minimum, we need at least 0.72% expected
    return to justify the trade.

    This prevents churning — trading for the sake of trading.

    Args:
        predictions: {symbol: predicted_return}
        round_trip_cost_bps: total cost in basis points (entry + exit)
        min_profit_multiple: required multiple of cost to trade

    Returns:
        Filtered predictions (only profitable after costs)
    """
    cost_threshold = round_trip_cost_bps / 10000 * min_profit_multiple

    filtered = {}
    skipped = 0
    for symbol, pred in predictions.items():
        if abs(pred) >= cost_threshold:
            filtered[symbol] = pred
        else:
            skipped += 1

    if skipped > 0:
        logger.info(
            f"Cost filter: removed {skipped} trades below {cost_threshold:.4f} "
            f"({round_trip_cost_bps:.0f}bps × {min_profit_multiple:.0f}x)"
        )

    return filtered


# ── 4. Dynamic Ensemble Weighting ────────────────────────────────────────

def compute_dynamic_weights(
    model_ics: dict[str, list[float]],
    lookback: int = 10,
    min_weight: float = 0.1,
) -> dict[str, float]:
    """
    Weight ensemble models by recent predictive power (Information Coefficient).

    Instead of fixed weights (CrossMamba 60%, LightGBM 20%, TST 20%),
    shift weight toward whichever model has been most accurate recently.

    Uses exponentially-weighted IC over the last N prediction periods.

    Args:
        model_ics: {model_name: [ic_period_1, ic_period_2, ...]}
        lookback: number of recent periods to consider
        min_weight: minimum weight floor (prevents zero-weighting)

    Returns:
        {model_name: weight} summing to 1.0
    """
    if not model_ics:
        return {}

    avg_ics = {}
    for model, ics in model_ics.items():
        recent = ics[-lookback:] if len(ics) >= lookback else ics
        if not recent:
            avg_ics[model] = 0.0
            continue
        # Exponential weighting: recent ICs matter more
        weights = np.exp(np.arange(len(recent)) * 0.1)
        weights /= weights.sum()
        avg_ics[model] = float(np.average(recent, weights=weights))

    # Convert to positive weights (IC can be negative = bad model)
    # Use max(IC, 0) so negative-IC models get minimum weight
    positive_ics = {m: max(ic, 0.001) for m, ic in avg_ics.items()}
    total = sum(positive_ics.values())

    if total <= 0:
        # All models performing poorly — equal weight
        n = len(model_ics)
        return {m: 1.0 / n for m in model_ics}

    weights = {m: ic / total for m, ic in positive_ics.items()}

    # Enforce minimum weight floor
    for m in weights:
        weights[m] = max(weights[m], min_weight)

    # Renormalize
    total = sum(weights.values())
    weights = {m: round(w / total, 4) for m, w in weights.items()}

    return weights


# ── 5. Implementation Shortfall Estimation ───────────────────────────────

def estimate_market_impact(
    order_notional: float,
    avg_daily_volume_usd: float,
    volatility: float = 0.02,
    participation_rate: float = 0.05,
) -> float:
    """
    Estimate the market impact of an order (Almgren-Chriss model simplified).

    Market impact = how much our order moves the price against us.
    Larger orders relative to volume have higher impact.

    Args:
        order_notional: order size in dollars
        avg_daily_volume_usd: stock's average daily dollar volume
        volatility: daily volatility of the stock
        participation_rate: what fraction of daily volume is our order

    Returns:
        Estimated impact in basis points
    """
    if avg_daily_volume_usd <= 0:
        return 100.0  # assume high impact for illiquid stocks

    # Participation rate (our order / daily volume)
    participation = order_notional / avg_daily_volume_usd

    # Square-root market impact model (standard in industry)
    # Impact ≈ σ × √(participation_rate)
    # Where σ = daily volatility
    impact = volatility * np.sqrt(participation) * 10000  # convert to bps

    return round(float(impact), 2)


def should_use_vwap(
    order_notional: float,
    avg_daily_volume_usd: float,
    impact_threshold_bps: float = 10.0,
) -> tuple[bool, float]:
    """
    Decide whether to use VWAP (split order over time) vs market order.

    If estimated impact > threshold, use VWAP to reduce cost.

    Returns (use_vwap, estimated_impact_bps)
    """
    impact = estimate_market_impact(order_notional, avg_daily_volume_usd)
    return impact > impact_threshold_bps, impact
