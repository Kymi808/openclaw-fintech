"""
Intraday trading signals — rule-based, no ML.

Each signal class:
1. Takes 1-minute bar data
2. Computes whether a setup exists
3. Returns entry price, stop loss, and target
4. Has a clear invalidation condition

These are systematic day-trading signals, NOT HFT.
Typical hold time: 15 minutes to 3 hours. All positions close by EOD.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from skills.shared import get_logger
from .calibration import AdaptiveThresholds

logger = get_logger("intraday.signals")


@dataclass
class IntradaySignal:
    """A single intraday trading signal."""
    signal_type: str       # "vwap_reversion", "orb", "momentum_burst", "gap"
    symbol: str
    side: str              # "buy" or "sell"
    entry_price: float
    stop_loss: float
    target_price: float
    confidence: float      # 0 to 1
    reason: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    max_hold_minutes: int = 120  # auto-close after this

    @property
    def risk_reward(self) -> float:
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.target_price - self.entry_price)
        return reward / risk if risk > 0 else 0

    def to_dict(self) -> dict:
        return {
            **self.__dict__,
            "risk_reward": round(self.risk_reward, 2),
        }


class VWAPReversion:
    """
    VWAP Reversion Signal

    When price deviates significantly from VWAP (volume-weighted average price),
    it tends to revert. This is one of the most reliable intraday signals.

    Setup:
    - Price > 1.5 std devs above VWAP → sell signal (expect reversion down)
    - Price < 1.5 std devs below VWAP → buy signal (expect reversion up)

    Stop: 2.5 std devs from VWAP (signal invalidated)
    Target: VWAP (full reversion)
    """
    ENTRY_THRESHOLD = 1.5   # std devs from VWAP to trigger
    STOP_THRESHOLD = 2.5    # std devs for stop loss
    MIN_BARS = 30           # need at least 30 minutes of data

    @staticmethod
    def compute_vwap(bars: list[dict]) -> tuple[float, float]:
        """
        Compute VWAP and standard deviation from 1-min bars.

        Args:
            bars: list of dicts with keys: close, volume, high, low

        Returns:
            (vwap, vwap_std)
        """
        if not bars:
            return 0.0, 0.0

        typical_prices = np.array([(b["high"] + b["low"] + b["close"]) / 3 for b in bars])
        volumes = np.array([b["volume"] for b in bars])

        total_vol = volumes.sum()
        if total_vol == 0:
            return typical_prices[-1], 0.0

        vwap = (typical_prices * volumes).sum() / total_vol

        # VWAP standard deviation (volume-weighted)
        squared_devs = (typical_prices - vwap) ** 2
        vwap_var = (squared_devs * volumes).sum() / total_vol
        vwap_std = np.sqrt(vwap_var)

        return float(vwap), float(vwap_std)

    @classmethod
    def check(cls, symbol: str, bars: list[dict]) -> Optional[IntradaySignal]:
        """Check for VWAP reversion setup with ATR-adaptive thresholds."""
        if len(bars) < cls.MIN_BARS:
            return None

        vwap, vwap_std = cls.compute_vwap(bars)
        if vwap_std == 0:
            return None

        # Use ATR-adaptive thresholds instead of fixed std dev multiples
        adaptive = AdaptiveThresholds(bars)
        entry_band = adaptive.vwap_entry_threshold if adaptive.atr > 0 else vwap_std * cls.ENTRY_THRESHOLD
        stop_band = adaptive.vwap_stop_threshold if adaptive.atr > 0 else vwap_std * cls.STOP_THRESHOLD

        current_price = bars[-1]["close"]
        distance = current_price - vwap
        deviation = distance / vwap_std if vwap_std > 0 else 0

        if distance > entry_band:
            stop = vwap + stop_band
            return IntradaySignal(
                signal_type="vwap_reversion",
                symbol=symbol,
                side="sell",
                entry_price=current_price,
                stop_loss=round(stop, 2),
                target_price=round(vwap, 2),
                confidence=min(0.9, 0.5 + (deviation - cls.ENTRY_THRESHOLD) * 0.2),
                reason=f"Price {deviation:.1f}σ above VWAP ({vwap:.2f}), ATR={adaptive.atr:.2f}",
                max_hold_minutes=90,
            )

        if distance < -entry_band:
            stop = vwap - stop_band
            return IntradaySignal(
                signal_type="vwap_reversion",
                symbol=symbol,
                side="buy",
                entry_price=current_price,
                stop_loss=round(stop, 2),
                target_price=round(vwap, 2),
                confidence=min(0.9, 0.5 + (abs(deviation) - cls.ENTRY_THRESHOLD) * 0.2),
                reason=f"Price {abs(deviation):.1f}σ below VWAP ({vwap:.2f}), ATR={adaptive.atr:.2f}",
                max_hold_minutes=90,
            )

        return None


class OpeningRangeBreakout:
    """
    Opening Range Breakout (ORB) Signal

    The first 30 minutes of trading establish the "opening range."
    A breakout above the high or below the low of this range signals
    directional momentum for the day.

    Setup:
    - After 10:00 AM ET, price breaks above the 9:30-10:00 high → buy
    - After 10:00 AM ET, price breaks below the 9:30-10:00 low → sell

    Stop: Opposite end of the opening range
    Target: 1.5x the opening range width
    """
    OPENING_RANGE_BARS = 30  # first 30 minutes (1-min bars)
    MIN_RANGE_PCT = 0.003    # minimum 0.3% range to be tradeable
    BREAKOUT_CONFIRM = 2     # need 2 consecutive bars beyond range

    @classmethod
    def check(cls, symbol: str, bars: list[dict]) -> Optional[IntradaySignal]:
        """Check for opening range breakout with ATR-adaptive minimum range."""
        if len(bars) < cls.OPENING_RANGE_BARS + cls.BREAKOUT_CONFIRM:
            return None

        # Compute opening range (first 30 bars)
        or_bars = bars[:cls.OPENING_RANGE_BARS]
        or_high = max(b["high"] for b in or_bars)
        or_low = min(b["low"] for b in or_bars)
        or_range = or_high - or_low

        # ATR-adaptive minimum range instead of fixed 0.3%
        adaptive = AdaptiveThresholds(bars)
        min_range = adaptive.orb_min_range if adaptive.atr > 0 else or_high * cls.MIN_RANGE_PCT

        if or_range < min_range:
            return None

        # Check recent bars for breakout
        recent = bars[-cls.BREAKOUT_CONFIRM:]
        all_above = all(b["close"] > or_high for b in recent)
        all_below = all(b["close"] < or_low for b in recent)

        if all_above:
            target = or_high + 1.5 * or_range
            return IntradaySignal(
                signal_type="orb",
                symbol=symbol,
                side="buy",
                entry_price=recent[-1]["close"],
                stop_loss=round(or_low, 2),
                target_price=round(target, 2),
                confidence=0.6,
                reason=f"Breakout above opening range high ({or_high:.2f})",
                max_hold_minutes=180,
            )

        if all_below:
            target = or_low - 1.5 * or_range
            return IntradaySignal(
                signal_type="orb",
                symbol=symbol,
                side="sell",
                entry_price=recent[-1]["close"],
                stop_loss=round(or_high, 2),
                target_price=round(target, 2),
                confidence=0.6,
                reason=f"Breakdown below opening range low ({or_low:.2f})",
                max_hold_minutes=180,
            )

        return None


class MomentumBurst:
    """
    Momentum Burst Signal

    Detects sudden acceleration in price with above-average volume.
    This captures the start of a directional move driven by institutional flow.

    Setup:
    - Last 5 bars: price moved > 0.5% in one direction
    - Volume in last 5 bars: > 2x the rolling 20-bar average
    - Entry on continuation

    Stop: Low of the burst candles (for buy), high (for sell)
    Target: Equal to the burst distance (1:1 risk/reward minimum)
    """
    BURST_BARS = 5
    MIN_MOVE_PCT = 0.005     # 0.5% minimum move
    VOLUME_MULTIPLIER = 2.0  # volume must be 2x average
    LOOKBACK_BARS = 20       # for volume average

    @classmethod
    def check(cls, symbol: str, bars: list[dict]) -> Optional[IntradaySignal]:
        """Check for momentum burst."""
        needed = cls.LOOKBACK_BARS + cls.BURST_BARS
        if len(bars) < needed:
            return None

        burst_bars = bars[-cls.BURST_BARS:]
        lookback_bars = bars[-(cls.LOOKBACK_BARS + cls.BURST_BARS):-cls.BURST_BARS]

        # Price move in burst — ATR-adaptive threshold
        burst_open = burst_bars[0]["open"]
        burst_close = burst_bars[-1]["close"]
        move = abs(burst_close - burst_open)
        move_pct = move / burst_open

        adaptive = AdaptiveThresholds(bars)
        min_move = adaptive.momentum_min_move if adaptive.atr > 0 else burst_open * cls.MIN_MOVE_PCT

        if move < min_move:
            return None

        # Volume check
        burst_vol = sum(b["volume"] for b in burst_bars)
        avg_vol = sum(b["volume"] for b in lookback_bars) / len(lookback_bars) * cls.BURST_BARS
        if avg_vol == 0 or burst_vol < avg_vol * cls.VOLUME_MULTIPLIER:
            return None

        vol_ratio = burst_vol / avg_vol
        current_price = burst_close

        if move_pct > 0:
            # Bullish burst
            burst_low = min(b["low"] for b in burst_bars)
            burst_distance = current_price - burst_low
            return IntradaySignal(
                signal_type="momentum_burst",
                symbol=symbol,
                side="buy",
                entry_price=current_price,
                stop_loss=round(burst_low, 2),
                target_price=round(current_price + burst_distance, 2),
                confidence=min(0.8, 0.5 + (vol_ratio - cls.VOLUME_MULTIPLIER) * 0.1),
                reason=f"Bullish burst: {move_pct:.2%} move on {vol_ratio:.1f}x volume",
                max_hold_minutes=60,
            )
        else:
            # Bearish burst
            burst_high = max(b["high"] for b in burst_bars)
            burst_distance = burst_high - current_price
            return IntradaySignal(
                signal_type="momentum_burst",
                symbol=symbol,
                side="sell",
                entry_price=current_price,
                stop_loss=round(burst_high, 2),
                target_price=round(current_price - burst_distance, 2),
                confidence=min(0.8, 0.5 + (vol_ratio - cls.VOLUME_MULTIPLIER) * 0.1),
                reason=f"Bearish burst: {move_pct:.2%} move on {vol_ratio:.1f}x volume",
                max_hold_minutes=60,
            )


class GapAnalysis:
    """
    Gap Fade / Gap Continuation Signal

    Analyzes overnight gaps (difference between previous close and today's open).

    - Small gaps (< 1%) tend to FILL (fade the gap)
    - Large gaps (> 2%) with volume tend to CONTINUE

    Setup (gap fade):
    - Gap up < 1% → sell (expect gap fill back to prev close)
    - Gap down < 1% → buy (expect gap fill back to prev close)

    Setup (gap continuation):
    - Gap up > 2% + first 15 min holds above open → buy
    - Gap down > 2% + first 15 min holds below open → sell
    """
    FADE_THRESHOLD = 0.01    # gaps < 1% tend to fill
    CONTINUE_THRESHOLD = 0.02  # gaps > 2% tend to continue
    CONFIRM_BARS = 15        # 15 min confirmation for continuation

    @classmethod
    def check(
        cls,
        symbol: str,
        bars: list[dict],
        prev_close: float,
    ) -> Optional[IntradaySignal]:
        """Check for gap signal with ATR-adaptive thresholds."""
        if not bars or prev_close <= 0:
            return None

        today_open = bars[0]["open"]
        gap_pct = (today_open - prev_close) / prev_close
        current_price = bars[-1]["close"]

        # ATR-adaptive gap thresholds instead of fixed 1%/2%
        adaptive = AdaptiveThresholds(bars)
        fade_threshold = adaptive.gap_fade_threshold if adaptive.atr > 0 else cls.FADE_THRESHOLD
        continue_threshold = adaptive.gap_continue_threshold if adaptive.atr > 0 else cls.CONTINUE_THRESHOLD

        # Gap fade: small gap, expect fill
        if 0 < gap_pct < fade_threshold:
            return IntradaySignal(
                signal_type="gap_fade",
                symbol=symbol,
                side="sell",
                entry_price=current_price,
                stop_loss=round(today_open * 1.005, 2),  # stop above open
                target_price=round(prev_close, 2),  # target = prev close (gap fill)
                confidence=0.55,
                reason=f"Gap up {gap_pct:.2%} — expecting fade to prev close ({prev_close:.2f})",
                max_hold_minutes=120,
            )

        if -fade_threshold < gap_pct < 0:
            return IntradaySignal(
                signal_type="gap_fade",
                symbol=symbol,
                side="buy",
                entry_price=current_price,
                stop_loss=round(today_open * 0.995, 2),
                target_price=round(prev_close, 2),
                confidence=0.55,
                reason=f"Gap down {gap_pct:.2%} — expecting fade to prev close ({prev_close:.2f})",
                max_hold_minutes=120,
            )

        # Gap continuation: large gap with confirmation
        if len(bars) >= cls.CONFIRM_BARS:
            confirm_bars = bars[:cls.CONFIRM_BARS]

            if gap_pct > continue_threshold:
                # Large gap up — check if first 15 min holds above open
                holds_above = all(b["low"] > today_open * 0.998 for b in confirm_bars)
                if holds_above:
                    gap_size = today_open - prev_close
                    return IntradaySignal(
                        signal_type="gap_continuation",
                        symbol=symbol,
                        side="buy",
                        entry_price=current_price,
                        stop_loss=round(today_open * 0.995, 2),
                        target_price=round(current_price + gap_size * 0.5, 2),
                        confidence=0.6,
                        reason=f"Gap up {gap_pct:.2%} holding — continuation expected",
                        max_hold_minutes=180,
                    )

            if gap_pct < -continue_threshold:
                holds_below = all(b["high"] < today_open * 1.002 for b in confirm_bars)
                if holds_below:
                    gap_size = prev_close - today_open
                    return IntradaySignal(
                        signal_type="gap_continuation",
                        symbol=symbol,
                        side="sell",
                        entry_price=current_price,
                        stop_loss=round(today_open * 1.005, 2),
                        target_price=round(current_price - gap_size * 0.5, 2),
                        confidence=0.6,
                        reason=f"Gap down {gap_pct:.2%} holding — continuation expected",
                        max_hold_minutes=180,
                    )

        return None
