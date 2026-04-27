"""
Microstructure features for intraday prediction.

These features capture market dynamics invisible in standard OHLCV:
1. Order flow imbalance — are buyers or sellers in control?
2. Kyle's lambda — price impact per dollar of volume
3. Amihud illiquidity — how much does price move per unit volume?
4. Signed volume — volume classified as buy or sell initiated
5. VWAP deviation persistence — does VWAP deviation predict mean-reversion?
6. Trade intensity — how many trades per bar? (proxy from volume/range)
7. Autocorrelation — mean-reversion vs momentum regime detection

These are the features that separate institutional intraday models
from retail technical analysis. Derivable from 1-min OHLCV bars
(we don't need Level 2 data).

Reference: López de Prado, "Advances in Financial Machine Learning"
           Cartea, Jaimungal & Penalva, "Algorithmic and High-Frequency Trading"
"""
import numpy as np

from skills.shared import get_logger

logger = get_logger("intraday.model.microstructure")


def compute_microstructure_features(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
) -> dict[str, float]:
    """
    Compute microstructure features from OHLCV bar data.

    These features approximate what you'd get from tick-level data
    using bar-level proxies.
    """
    n = len(closes)
    if n < 15:
        return {}

    features = {}
    returns = np.diff(closes) / closes[:-1]

    # ── 1. Order Flow Imbalance (OFI) ────────────────────────────────
    # Approximate buy/sell pressure from bar structure
    # Close near high → buyers in control, close near low → sellers
    bar_range = highs - lows
    bar_range = np.where(bar_range == 0, 0.001, bar_range)

    # Tick rule proxy: where in the bar did it close?
    close_position = (closes - lows) / bar_range  # 0 = closed at low, 1 = closed at high

    # Signed volume: volume × close_position (buyer) vs volume × (1 - close_position) (seller)
    buy_volume = volumes * close_position
    sell_volume = volumes * (1 - close_position)

    # OFI: cumulative buy - sell volume imbalance
    for period in [5, 15, 30]:
        if n >= period:
            ofi = (buy_volume[-period:].sum() - sell_volume[-period:].sum()) / volumes[-period:].sum()
            features[f"ofi_{period}m"] = float(np.clip(ofi, -1, 1))
        else:
            features[f"ofi_{period}m"] = 0.0

    # ── 2. Kyle's Lambda (Price Impact) ──────────────────────────────
    # Lambda = regression coefficient of return on signed volume
    # High lambda = illiquid, each trade moves price more
    if n >= 20:
        signed_vol = np.diff(volumes * (2 * close_position[1:] - 1))  # direction × volume
        rets = returns[-len(signed_vol):]
        min_len = min(len(signed_vol), len(rets))
        if min_len >= 10:
            signed_vol = signed_vol[-min_len:]
            rets = rets[-min_len:]
            # Regression: return = lambda × signed_volume + epsilon
            sv_var = np.var(signed_vol)
            if sv_var > 0:
                kyle_lambda = np.cov(rets, signed_vol)[0, 1] / sv_var
                features["kyle_lambda"] = float(kyle_lambda)
            else:
                features["kyle_lambda"] = 0.0
        else:
            features["kyle_lambda"] = 0.0
    else:
        features["kyle_lambda"] = 0.0

    # ── 3. Amihud Illiquidity ────────────────────────────────────────
    # |return| / dollar_volume — measures price impact per dollar traded
    for period in [5, 15]:
        if n >= period:
            abs_rets = np.abs(returns[-period:])
            dollar_vol = (closes[-period:] * volumes[-period:])[1:]
            dollar_vol = np.where(dollar_vol == 0, 1, dollar_vol)
            min_l = min(len(abs_rets), len(dollar_vol))
            amihud = np.mean(abs_rets[-min_l:] / dollar_vol[-min_l:])
            features[f"amihud_{period}m"] = float(amihud)
        else:
            features[f"amihud_{period}m"] = 0.0

    # ── 4. Realized Volatility Signature ─────────────────────────────
    # Vol at different sampling frequencies — reveals microstructure noise
    if n >= 30:
        # 1-min vol
        vol_1m = np.std(returns[-30:]) * np.sqrt(252 * 390)
        # 5-min vol (subsample every 5th bar)
        rets_5m = (closes[-30::5][1:] / closes[-30::5][:-1]) - 1
        vol_5m = np.std(rets_5m) * np.sqrt(252 * 78) if len(rets_5m) > 2 else vol_1m

        features["vol_1m"] = float(vol_1m)
        features["vol_5m"] = float(vol_5m)
        # Signature ratio: vol_1m / vol_5m > 1 means microstructure noise dominates
        features["vol_signature_ratio"] = float(vol_1m / vol_5m) if vol_5m > 0 else 1.0
    else:
        features["vol_1m"] = 0.0
        features["vol_5m"] = 0.0
        features["vol_signature_ratio"] = 1.0

    # ── 5. Autocorrelation at Multiple Lags ──────────────────────────
    # Positive autocorrelation = momentum, negative = mean-reversion
    for lag in [1, 5, 10]:
        if n >= lag + 15:
            ac = _autocorrelation(returns[-30:], lag)
            features[f"autocorr_lag{lag}"] = float(ac)
        else:
            features[f"autocorr_lag{lag}"] = 0.0

    # ── 6. Effective Spread Proxy ────────────────────────────────────
    # Roll's measure: spread ≈ 2 × sqrt(-cov(r_t, r_{t-1}))
    if n >= 20:
        cov = np.cov(returns[-20:-1], returns[-19:])[0, 1]
        if cov < 0:
            features["roll_spread"] = float(2 * np.sqrt(-cov))
        else:
            features["roll_spread"] = 0.0
    else:
        features["roll_spread"] = 0.0

    # ── 7. Volume-Weighted Price Momentum ────────────────────────────
    # Momentum weighted by volume — high-volume moves are more significant
    if n >= 10:
        weighted_rets = returns[-10:] * volumes[-10:][1:] / volumes[-10:][1:].mean()
        features["vw_momentum_10m"] = float(np.sum(weighted_rets))
    else:
        features["vw_momentum_10m"] = 0.0

    # ── 8. Toxicity (VPIN proxy) ─────────────────────────────────────
    # Volume-synchronized probability of informed trading
    # Approximated by imbalance of buy vs sell volume buckets
    if n >= 20:
        buy_pct = buy_volume[-20:].sum() / max(volumes[-20:].sum(), 1)
        features["vpin_proxy"] = float(abs(buy_pct - 0.5) * 2)  # 0 = balanced, 1 = all one-sided
    else:
        features["vpin_proxy"] = 0.0

    return features


def _autocorrelation(x: np.ndarray, lag: int) -> float:
    """Compute autocorrelation at a given lag."""
    if len(x) <= lag:
        return 0.0
    n = len(x)
    mean = np.mean(x)
    var = np.var(x)
    if var == 0:
        return 0.0
    cov = np.mean((x[lag:] - mean) * (x[:n - lag] - mean))
    return cov / var
