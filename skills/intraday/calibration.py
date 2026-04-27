"""
Per-symbol adaptive thresholds and correlation filtering.

Addresses:
1. Fixed thresholds don't work across volatility regimes — TSLA needs different
   thresholds than JNJ. We use ATR (Average True Range) to normalize.
2. Correlated signals are one bet, not many — we cluster signals by sector
   and limit exposure per cluster.
"""
import numpy as np

from skills.shared import get_logger

logger = get_logger("intraday.calibration")


# ── Per-Symbol ATR-Based Thresholds ──────────────────────────────────────

# GICS sector mapping for correlation filtering
SECTOR_MAP = {
    # Technology
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "AVGO": "tech",
    "AMD": "tech", "QCOM": "tech", "CRM": "tech", "ORCL": "tech",
    # Consumer
    "AMZN": "consumer", "TSLA": "consumer", "HD": "consumer",
    "COST": "consumer", "NFLX": "consumer",
    # Communication
    "GOOGL": "comm", "META": "comm",
    # Finance
    "JPM": "finance", "V": "finance", "MA": "finance", "GS": "finance",
    # Healthcare
    "UNH": "health", "LLY": "health",
    # Energy
    "XOM": "energy", "CVX": "energy",
    # Industrials
    "BA": "industrial", "CAT": "industrial",
    # ETFs
    "SPY": "index", "QQQ": "index", "IWM": "index",
}


def compute_atr(bars: list[dict], period: int = 14) -> float:
    """
    Compute Average True Range from 1-min bars.

    ATR measures a stock's typical price range per bar — a volatility-
    normalized measure that adapts to each symbol's behavior.
    """
    if len(bars) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(bars)):
        high = bars[i]["high"]
        low = bars[i]["low"]
        prev_close = bars[i - 1]["close"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return float(np.mean(true_ranges)) if true_ranges else 0.0

    # Exponential moving average of true range
    atr = np.mean(true_ranges[:period])
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period

    return float(atr)


def compute_atr_pct(bars: list[dict], period: int = 14) -> float:
    """ATR as a percentage of current price."""
    atr = compute_atr(bars, period)
    if not bars or bars[-1]["close"] == 0:
        return 0.0
    return atr / bars[-1]["close"]


class AdaptiveThresholds:
    """
    Per-symbol adaptive thresholds based on ATR.

    Instead of fixed thresholds (e.g., "0.5% move"), we use ATR multiples:
    - VWAP: trigger at 1.5 ATR from VWAP (not 1.5 std devs)
    - ORB: minimum range = 0.5 ATR (not 0.3%)
    - Momentum: move = 1.0 ATR in burst window (not 0.5%)
    - Gap: fade if gap < 0.5 ATR, continue if > 1.5 ATR
    """

    def __init__(self, bars: list[dict]):
        self.atr = compute_atr(bars)
        self.atr_pct = compute_atr_pct(bars)
        self.price = bars[-1]["close"] if bars else 0.0

    @property
    def vwap_entry_threshold(self) -> float:
        """ATR multiplier for VWAP reversion entry (in price units)."""
        return self.atr * 1.5

    @property
    def vwap_stop_threshold(self) -> float:
        """ATR multiplier for VWAP reversion stop."""
        return self.atr * 2.5

    @property
    def orb_min_range(self) -> float:
        """Minimum opening range size (in price units)."""
        return self.atr * 0.5

    @property
    def momentum_min_move(self) -> float:
        """Minimum price move for momentum burst (in price units)."""
        return self.atr * 1.0

    @property
    def gap_fade_threshold(self) -> float:
        """Gap size below which we expect a fade (as fraction of price)."""
        return self.atr_pct * 0.5

    @property
    def gap_continue_threshold(self) -> float:
        """Gap size above which we expect continuation (as fraction of price)."""
        return self.atr_pct * 1.5

    def to_dict(self) -> dict:
        return {
            "atr": round(self.atr, 4),
            "atr_pct": round(self.atr_pct, 4),
            "vwap_entry": round(self.vwap_entry_threshold, 4),
            "vwap_stop": round(self.vwap_stop_threshold, 4),
            "orb_min_range": round(self.orb_min_range, 4),
            "momentum_min_move": round(self.momentum_min_move, 4),
            "gap_fade": round(self.gap_fade_threshold, 4),
            "gap_continue": round(self.gap_continue_threshold, 4),
        }


# ── Correlation Filtering ────────────────────────────────────────────────

def filter_correlated_signals(signals: list, max_per_sector: int = 2) -> list:
    """
    Filter signals to avoid correlated bets.

    If 5 tech stocks all trigger VWAP reversion at the same time,
    that's a sector-wide move — take the best 2, not all 5.

    Rules:
    - Max 2 signals per sector per signal type
    - Max 1 signal per sector for the same direction (all buys or all sells)
      of the same signal type
    - Index ETFs (SPY, QQQ) are exempt (they ARE the diversification)
    """
    # Group by (sector, signal_type, side)
    groups: dict[tuple, list] = {}
    for sig in signals:
        symbol = sig.symbol if hasattr(sig, "symbol") else sig.get("symbol", "")
        signal_type = sig.signal_type if hasattr(sig, "signal_type") else sig.get("signal_type", "")
        side = sig.side if hasattr(sig, "side") else sig.get("side", "")

        sector = SECTOR_MAP.get(symbol, "other")

        # Index ETFs are exempt
        if sector == "index":
            continue

        key = (sector, signal_type, side)
        groups.setdefault(key, []).append(sig)

    # For each group, keep only the highest-confidence signals
    filtered_out = set()
    for key, group_signals in groups.items():
        if len(group_signals) <= max_per_sector:
            continue

        # Sort by confidence descending
        sorted_sigs = sorted(
            group_signals,
            key=lambda s: s.confidence if hasattr(s, "confidence") else s.get("confidence", 0),
            reverse=True,
        )

        # Mark excess signals for removal
        for sig in sorted_sigs[max_per_sector:]:
            sym = sig.symbol if hasattr(sig, "symbol") else sig.get("symbol", "")
            filtered_out.add(id(sig))
            logger.info(
                f"Correlation filter: dropped {sym} ({key[0]} sector, "
                f"{key[1]} {key[2]}) — already have {max_per_sector} in group"
            )

    return [s for s in signals if id(s) not in filtered_out]
