"""
Intraday feature engineering from 1-minute and 5-minute bars.

Features designed for predicting 1-hour forward returns:
1. VWAP features — distance from VWAP, VWAP slope, time-weighted deviation
2. Volume features — relative volume, volume acceleration, buy/sell imbalance
3. Momentum features — short-term momentum at multiple timeframes
4. Volatility features — realized vol, vol expansion/contraction
5. Microstructure — spread proxy, trade intensity, bar range
6. Session features — time of day, opening gap, distance from day high/low
7. Cross-asset context — SPY correlation, sector ETF momentum
"""
import numpy as np
import pandas as pd
from pathlib import Path

from skills.shared import get_logger

logger = get_logger("intraday.model.features")


def build_intraday_features(
    bars_1min: list[dict],
    prev_close: float = 0.0,
    spy_bars: list[dict] = None,
) -> dict[str, float]:
    """
    Build feature vector from 1-minute bars for a single stock at current time.

    Args:
        bars_1min: list of 1-min bar dicts with open/high/low/close/volume
        prev_close: previous day's close price (for gap features)
        spy_bars: SPY 1-min bars (for cross-asset correlation)

    Returns:
        dict of feature_name -> feature_value
    """
    if len(bars_1min) < 15:
        return {}

    closes = np.array([b["close"] for b in bars_1min])
    highs = np.array([b["high"] for b in bars_1min])
    lows = np.array([b["low"] for b in bars_1min])
    opens = np.array([b["open"] for b in bars_1min])
    volumes = np.array([b["volume"] for b in bars_1min], dtype=float)
    typical = (highs + lows + closes) / 3

    current = closes[-1]
    n = len(closes)

    features = {}

    # ── VWAP Features ────────────────────────────────────────────────
    cum_vol = np.cumsum(volumes)
    cum_tp_vol = np.cumsum(typical * volumes)
    vwap = cum_tp_vol[-1] / cum_vol[-1] if cum_vol[-1] > 0 else current

    # Distance from VWAP (normalized by price)
    features["vwap_distance_pct"] = (current - vwap) / vwap if vwap > 0 else 0

    # VWAP standard deviation bands
    if cum_vol[-1] > 0:
        vwap_var = np.sum((typical - vwap) ** 2 * volumes) / cum_vol[-1]
        vwap_std = np.sqrt(vwap_var)
        features["vwap_zscore"] = (current - vwap) / vwap_std if vwap_std > 0 else 0
    else:
        features["vwap_zscore"] = 0

    # VWAP slope (is VWAP trending up or down?)
    if n >= 30:
        vwap_30ago = cum_tp_vol[n - 30] / cum_vol[n - 30] if cum_vol[n - 30] > 0 else vwap
        features["vwap_slope_30m"] = (vwap - vwap_30ago) / vwap_30ago if vwap_30ago > 0 else 0
    else:
        features["vwap_slope_30m"] = 0

    # ── Volume Features ──────────────────────────────────────────────
    avg_vol_all = np.mean(volumes)

    features["relative_volume_5m"] = (
        np.mean(volumes[-5:]) / avg_vol_all if avg_vol_all > 0 else 1
    )
    features["relative_volume_15m"] = (
        np.mean(volumes[-15:]) / avg_vol_all if avg_vol_all > 0 and n >= 15 else 1
    )

    # Volume acceleration (is volume increasing or decreasing?)
    if n >= 10:
        vol_recent = np.mean(volumes[-5:])
        vol_prior = np.mean(volumes[-10:-5])
        features["volume_acceleration"] = (vol_recent - vol_prior) / vol_prior if vol_prior > 0 else 0
    else:
        features["volume_acceleration"] = 0

    # Buy/sell pressure proxy (close vs open within bar)
    bar_directions = closes - opens
    features["buy_pressure_5m"] = np.mean(bar_directions[-5:] > 0) if n >= 5 else 0.5
    features["buy_pressure_15m"] = np.mean(bar_directions[-15:] > 0) if n >= 15 else 0.5

    # ── Momentum Features ────────────────────────────────────────────
    for period in [5, 10, 15, 30]:
        if n >= period:
            ret = (closes[-1] - closes[-period]) / closes[-period]
            features[f"momentum_{period}m"] = ret
        else:
            features[f"momentum_{period}m"] = 0

    # Momentum acceleration
    if n >= 20:
        mom_recent = (closes[-1] - closes[-5]) / closes[-5] if closes[-5] > 0 else 0
        mom_prior = (closes[-5] - closes[-10]) / closes[-10] if closes[-10] > 0 else 0
        features["momentum_accel"] = mom_recent - mom_prior
    else:
        features["momentum_accel"] = 0

    # ── Volatility Features ──────────────────────────────────────────
    returns = np.diff(closes) / closes[:-1]

    features["realized_vol_5m"] = np.std(returns[-5:]) * np.sqrt(252 * 390) if n > 5 else 0
    features["realized_vol_15m"] = np.std(returns[-15:]) * np.sqrt(252 * 390) if n > 15 else 0

    # Vol expansion/contraction (recent vol vs session avg)
    if n > 20:
        vol_recent = np.std(returns[-10:])
        vol_session = np.std(returns)
        features["vol_ratio"] = vol_recent / vol_session if vol_session > 0 else 1
    else:
        features["vol_ratio"] = 1

    # ── Microstructure Features ──────────────────────────────────────
    # Bar range (high - low) as spread proxy
    ranges = highs - lows
    features["avg_range_pct"] = np.mean(ranges[-5:]) / current if current > 0 else 0

    # Trade intensity (volume per range unit)
    if np.mean(ranges[-5:]) > 0:
        features["trade_intensity"] = np.mean(volumes[-5:]) / np.mean(ranges[-5:])
    else:
        features["trade_intensity"] = 0

    # ── Session Features ─────────────────────────────────────────────
    # Time of day (0 = open, 1 = close, 390 minutes in session)
    features["session_progress"] = min(1.0, n / 390)

    # Opening gap
    if prev_close > 0:
        features["opening_gap_pct"] = (opens[0] - prev_close) / prev_close
    else:
        features["opening_gap_pct"] = 0

    # Distance from day high/low
    day_high = np.max(highs)
    day_low = np.min(lows)
    day_range = day_high - day_low
    if day_range > 0:
        features["dist_from_high"] = (day_high - current) / day_range
        features["dist_from_low"] = (current - day_low) / day_range
    else:
        features["dist_from_high"] = 0.5
        features["dist_from_low"] = 0.5

    # Opening range (first 30 min)
    if n >= 30:
        or_high = np.max(highs[:30])
        or_low = np.min(lows[:30])
        or_range = or_high - or_low
        features["above_opening_range"] = 1.0 if current > or_high else (-1.0 if current < or_low else 0.0)
        features["opening_range_pct"] = or_range / opens[0] if opens[0] > 0 else 0
    else:
        features["above_opening_range"] = 0
        features["opening_range_pct"] = 0

    # ── Cross-Asset Features ─────────────────────────────────────────
    if spy_bars and len(spy_bars) >= 15:
        spy_closes = np.array([b["close"] for b in spy_bars[-min(n, len(spy_bars)):]])
        stock_rets = np.diff(closes[-len(spy_closes):]) / closes[-len(spy_closes):-1]
        spy_rets = np.diff(spy_closes) / spy_closes[:-1]

        min_len = min(len(stock_rets), len(spy_rets))
        if min_len >= 10:
            correlation = np.corrcoef(stock_rets[-min_len:], spy_rets[-min_len:])[0, 1]
            features["spy_correlation"] = correlation if not np.isnan(correlation) else 0

            # Relative strength vs SPY
            stock_ret = (closes[-1] - closes[-min_len]) / closes[-min_len]
            spy_ret = (spy_closes[-1] - spy_closes[-min_len]) / spy_closes[-min_len]
            features["relative_strength_vs_spy"] = stock_ret - spy_ret
        else:
            features["spy_correlation"] = 0
            features["relative_strength_vs_spy"] = 0
    else:
        features["spy_correlation"] = 0
        features["relative_strength_vs_spy"] = 0

    # ── Microstructure Features ────────────────────────────────────
    try:
        from .microstructure import compute_microstructure_features
        micro = compute_microstructure_features(opens, highs, lows, closes, volumes)
        features.update(micro)
    except Exception:
        pass  # microstructure features are additive, not required

    return features


def build_features_batch(
    all_bars: dict[str, list[dict]],
    prev_closes: dict[str, float],
    spy_bars: list[dict] = None,
) -> pd.DataFrame:
    """
    Build feature matrix for multiple stocks.

    Returns DataFrame with index=symbols, columns=features.
    This is the input to the intraday model's predict().
    """
    rows = {}
    for symbol, bars in all_bars.items():
        if len(bars) < 15:
            continue
        prev = prev_closes.get(symbol, 0)
        features = build_intraday_features(bars, prev, spy_bars)
        if features:
            rows[symbol] = features

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame.from_dict(rows, orient="index")

    # Add LLM sentiment features per symbol (if available)
    try:
        from skills.news.llm_sentiment import compute_llm_sentiment_features
        from skills.shared.state import safe_load_state

        cache = safe_load_state(Path("./data/sentiment_cache.json"), {"articles": {}})
        analyses = list(cache.get("articles", {}).values())
        if analyses:
            for symbol in df.index:
                llm_feats = compute_llm_sentiment_features(analyses, symbol)
                for feat_name, feat_val in llm_feats.items():
                    df.loc[symbol, feat_name] = feat_val
    except Exception:
        pass  # LLM sentiment is additive, not required

    return df
