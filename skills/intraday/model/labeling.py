"""
Triple Barrier Labeling — institutional standard for ML in finance.

Instead of "what's the return in exactly 60 minutes?", triple barrier asks:
"Did price hit the profit target, stop loss, or time limit first?"

This produces three possible labels:
  +1 = hit upper barrier (profit target) first → successful trade
  -1 = hit lower barrier (stop loss) first → failed trade
   0 = hit time barrier (timeout) → inconclusive

Why this is better than fixed-horizon returns:
1. Matches how actual trades work (you have a target and a stop, not just time)
2. Captures path dependency (a stock that drops 5% then recovers to +1% at the
   60-min mark would be labeled "positive" with fixed horizon but "stop hit" with
   triple barrier — which is what actually matters for trading)
3. Accounts for volatility (wide barriers for volatile stocks, tight for calm ones)

Reference: López de Prado, "Advances in Financial Machine Learning" (2018), Ch. 3
"""
import numpy as np
from typing import Optional

from skills.shared import get_logger

logger = get_logger("intraday.model.labeling")


def compute_daily_volatility(closes: np.ndarray, span: int = 20) -> float:
    """Compute exponentially weighted volatility for barrier sizing."""
    if len(closes) < 2:
        return 0.01
    returns = np.diff(closes) / closes[:-1]
    if len(returns) < span:
        return float(np.std(returns)) if len(returns) > 1 else 0.01
    # EWM std
    weights = np.exp(-np.arange(len(returns))[::-1] / span)
    weights /= weights.sum()
    mean = np.average(returns, weights=weights)
    var = np.average((returns - mean) ** 2, weights=weights)
    return float(np.sqrt(var))


def triple_barrier_label(
    closes: np.ndarray,
    entry_idx: int,
    upper_mult: float = 2.0,
    lower_mult: float = 2.0,
    max_holding_bars: int = 60,
    vol_span: int = 20,
) -> tuple[int, int, float]:
    """
    Apply triple barrier method to a single entry point.

    Args:
        closes: array of close prices (full session)
        entry_idx: index of the entry bar
        upper_mult: upper barrier = entry × (1 + vol × upper_mult)
        lower_mult: lower barrier = entry × (1 - vol × lower_mult)
        max_holding_bars: time barrier in bars (60 = 60 minutes)
        vol_span: lookback for volatility estimation

    Returns:
        (label, exit_idx, return_pct)
        label: +1 (hit upper), -1 (hit lower), 0 (timeout)
        exit_idx: bar index where exit occurred
        return_pct: actual return at exit
    """
    if entry_idx >= len(closes) - 1:
        return 0, entry_idx, 0.0

    entry_price = closes[entry_idx]

    # Compute volatility-adaptive barriers
    lookback = closes[max(0, entry_idx - vol_span * 5):entry_idx]
    vol = compute_daily_volatility(lookback, vol_span) if len(lookback) > 5 else 0.01
    vol = max(vol, 0.001)  # floor

    upper_barrier = entry_price * (1 + vol * upper_mult)
    lower_barrier = entry_price * (1 - vol * lower_mult)

    # Scan forward
    end_idx = min(entry_idx + max_holding_bars, len(closes) - 1)

    for i in range(entry_idx + 1, end_idx + 1):
        price = closes[i]

        # Upper barrier hit → profit target
        if price >= upper_barrier:
            ret = (price - entry_price) / entry_price
            return 1, i, ret

        # Lower barrier hit → stop loss
        if price <= lower_barrier:
            ret = (price - entry_price) / entry_price
            return -1, i, ret

    # Time barrier hit → timeout
    exit_price = closes[end_idx]
    ret = (exit_price - entry_price) / entry_price
    return 0, end_idx, ret


def label_session(
    closes: np.ndarray,
    sample_interval: int = 30,
    upper_mult: float = 2.0,
    lower_mult: float = 2.0,
    max_holding_bars: int = 60,
) -> list[dict]:
    """
    Label an entire trading session using triple barrier method.

    Samples entries at every `sample_interval` bars and computes
    the label for each.

    Returns list of {entry_idx, label, exit_idx, return_pct, holding_bars}
    """
    labels = []
    for entry_idx in range(sample_interval, len(closes) - max_holding_bars, sample_interval):
        label, exit_idx, ret = triple_barrier_label(
            closes, entry_idx, upper_mult, lower_mult, max_holding_bars,
        )
        labels.append({
            "entry_idx": entry_idx,
            "label": label,
            "exit_idx": exit_idx,
            "return_pct": round(ret, 6),
            "holding_bars": exit_idx - entry_idx,
        })
    return labels


def compute_sample_weights(labels: list[dict], closes: np.ndarray) -> np.ndarray:
    """
    Compute sample uniqueness weights.

    Overlapping samples (where the holding periods intersect) carry
    less unique information. We downweight them to prevent overfitting
    to the same market event counted multiple times.

    Based on López de Prado's average uniqueness concept.
    """
    n = len(labels)
    if n == 0:
        return np.array([])

    # Build concurrency matrix: for each bar, how many active positions overlap
    max_bar = max(l["exit_idx"] for l in labels)
    concurrency = np.zeros(max_bar + 1)
    for l in labels:
        concurrency[l["entry_idx"]:l["exit_idx"] + 1] += 1

    # Average uniqueness per sample
    weights = np.zeros(n)
    for i, l in enumerate(labels):
        entry, exit = l["entry_idx"], l["exit_idx"]
        if exit > entry:
            avg_conc = np.mean(concurrency[entry:exit + 1])
            weights[i] = 1.0 / avg_conc if avg_conc > 0 else 1.0
        else:
            weights[i] = 1.0

    # Normalize weights to sum to n (so average weight = 1)
    total = weights.sum()
    if total > 0:
        weights = weights * n / total

    return weights
