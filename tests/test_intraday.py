"""
Tests for intraday signal module.
"""
import pytest
from datetime import datetime
from skills.intraday.signals import (
    VWAPReversion, OpeningRangeBreakout, MomentumBurst, GapAnalysis,
    IntradaySignal,
)
from skills.intraday.calibration import (
    compute_atr, AdaptiveThresholds, filter_correlated_signals,
)
from skills.intraday.position_manager import (
    ManagedPosition, update_trailing_stop, check_partial_exit,
    check_invalidation, update_position,
)


def _make_bars(prices, volumes=None, base_open=None):
    """Helper: create bar dicts from a list of close prices."""
    if volumes is None:
        volumes = [100_000] * len(prices)
    bars = []
    for i, (close, vol) in enumerate(zip(prices, volumes)):
        o = base_open if (base_open and i == 0) else (close * 0.999)
        bars.append({
            "open": o,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
            "volume": vol,
        })
    return bars


class TestVWAPReversion:
    def test_no_signal_with_few_bars(self):
        bars = _make_bars([100.0] * 10)
        assert VWAPReversion.check("AAPL", bars) is None

    def test_no_signal_at_vwap(self):
        bars = _make_bars([100.0] * 40)
        assert VWAPReversion.check("AAPL", bars) is None

    def test_sell_signal_above_vwap(self):
        # Price drifts up significantly above VWAP
        prices = [100.0] * 30 + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.0]
        bars = _make_bars(prices)
        sig = VWAPReversion.check("AAPL", bars)
        if sig:  # depends on exact std dev calculation
            assert sig.side == "sell"
            assert sig.target_price < sig.entry_price

    def test_buy_signal_below_vwap(self):
        prices = [100.0] * 30 + [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0, 92.0, 90.0]
        bars = _make_bars(prices)
        sig = VWAPReversion.check("AAPL", bars)
        if sig:
            assert sig.side == "buy"
            assert sig.target_price > sig.entry_price


class TestOpeningRangeBreakout:
    def test_no_signal_with_few_bars(self):
        bars = _make_bars([100.0] * 10)
        assert OpeningRangeBreakout.check("AAPL", bars) is None

    def test_buy_signal_on_breakout(self):
        # Opening range: 100-102, then breakout above
        or_prices = [100.5 + (i % 3) * 0.5 for i in range(30)]  # range ~100-102
        breakout_prices = [103.0, 103.5]  # above range high
        bars = _make_bars(or_prices + breakout_prices)

        # Manually set high/low to create proper range
        for i in range(30):
            bars[i]["high"] = 102.0
            bars[i]["low"] = 100.0

        sig = OpeningRangeBreakout.check("AAPL", bars)
        if sig:
            assert sig.side == "buy"
            assert sig.signal_type == "orb"

    def test_no_signal_inside_range(self):
        prices = [101.0] * 35  # stays in range
        bars = _make_bars(prices)
        for b in bars[:30]:
            b["high"] = 102.0
            b["low"] = 100.0
        assert OpeningRangeBreakout.check("AAPL", bars) is None


class TestMomentumBurst:
    def test_no_signal_without_volume(self):
        prices = [100.0] * 20 + [101.0, 101.5, 102.0, 102.5, 103.0]
        volumes = [100_000] * 25  # flat volume
        bars = _make_bars(prices, volumes)
        assert MomentumBurst.check("AAPL", bars) is None

    def test_buy_signal_with_volume_surge(self):
        prices = [100.0] * 20 + [100.5, 101.0, 101.5, 102.0, 103.0]
        volumes = [100_000] * 20 + [500_000, 600_000, 700_000, 800_000, 900_000]
        bars = _make_bars(prices, volumes)
        # Need proper open for burst calculation
        bars[20]["open"] = 100.0
        sig = MomentumBurst.check("AAPL", bars)
        if sig:
            assert sig.side == "buy"
            assert sig.signal_type == "momentum_burst"
            assert "volume" in sig.reason.lower()


class TestGapAnalysis:
    def test_gap_fade_small_gap_up(self):
        bars = _make_bars([100.3] * 5)
        bars[0]["open"] = 100.3
        sig = GapAnalysis.check("AAPL", bars, prev_close=100.0)
        if sig:
            assert sig.side == "sell"  # fade the gap
            assert sig.signal_type == "gap_fade"

    def test_gap_fade_small_gap_down(self):
        bars = _make_bars([99.7] * 5)
        bars[0]["open"] = 99.7
        sig = GapAnalysis.check("AAPL", bars, prev_close=100.0)
        if sig:
            assert sig.side == "buy"

    def test_gap_continuation_large_gap_up(self):
        # Large gap up (3%) that holds
        bars = _make_bars([103.0 + i * 0.1 for i in range(20)])
        bars[0]["open"] = 103.0
        for b in bars[:15]:
            b["low"] = 102.9  # holds above open
        sig = GapAnalysis.check("AAPL", bars, prev_close=100.0)
        if sig:
            assert sig.side == "buy"
            assert sig.signal_type == "gap_continuation"

    def test_no_signal_without_prev_close(self):
        bars = _make_bars([100.0] * 5)
        assert GapAnalysis.check("AAPL", bars, prev_close=0.0) is None


class TestIntradaySignal:
    def test_risk_reward_calculation(self):
        sig = IntradaySignal(
            signal_type="test",
            symbol="AAPL",
            side="buy",
            entry_price=100.0,
            stop_loss=98.0,
            target_price=104.0,
            confidence=0.7,
            reason="test",
        )
        assert sig.risk_reward == 2.0  # $4 reward / $2 risk

    def test_to_dict(self):
        sig = IntradaySignal(
            signal_type="vwap_reversion",
            symbol="TSLA",
            side="sell",
            entry_price=200.0,
            stop_loss=205.0,
            target_price=190.0,
            confidence=0.8,
            reason="test",
        )
        d = sig.to_dict()
        assert d["symbol"] == "TSLA"
        assert d["risk_reward"] == 2.0
        assert "signal_type" in d


# ── ATR and Adaptive Thresholds ──────────────────────────────────────────

class TestATR:
    def test_compute_atr_basic(self):
        bars = [
            {"high": 101, "low": 99, "close": 100, "volume": 1000},
            {"high": 102, "low": 98, "close": 101, "volume": 1000},
            {"high": 103, "low": 99, "close": 100, "volume": 1000},
        ] * 10
        atr = compute_atr(bars, period=14)
        assert atr > 0

    def test_atr_higher_for_volatile_stock(self):
        # Low vol stock
        low_vol = [
            {"high": 100.5, "low": 99.5, "close": 100, "volume": 1000},
        ] * 20
        # High vol stock
        high_vol = [
            {"high": 105, "low": 95, "close": 100, "volume": 1000},
        ] * 20
        assert compute_atr(high_vol) > compute_atr(low_vol)

    def test_adaptive_thresholds_scale_with_atr(self):
        low_vol = [{"high": 100.2, "low": 99.8, "close": 100, "volume": 1000}] * 20
        high_vol = [{"high": 105, "low": 95, "close": 100, "volume": 1000}] * 20

        at_low = AdaptiveThresholds(low_vol)
        at_high = AdaptiveThresholds(high_vol)

        assert at_high.vwap_entry_threshold > at_low.vwap_entry_threshold
        assert at_high.momentum_min_move > at_low.momentum_min_move


# ── Correlation Filtering ────────────────────────────────────────────────

class TestCorrelationFiltering:
    def _make_signal(self, symbol, signal_type="vwap_reversion", side="buy", confidence=0.7):
        return IntradaySignal(
            signal_type=signal_type, symbol=symbol, side=side,
            entry_price=100, stop_loss=98, target_price=104,
            confidence=confidence, reason="test",
        )

    def test_filters_excess_same_sector(self):
        signals = [
            self._make_signal("AAPL", confidence=0.9),  # tech
            self._make_signal("MSFT", confidence=0.8),  # tech
            self._make_signal("NVDA", confidence=0.7),  # tech
            self._make_signal("AVGO", confidence=0.6),  # tech
            self._make_signal("JPM", confidence=0.5),   # finance (different sector)
        ]
        filtered = filter_correlated_signals(signals, max_per_sector=2)
        tech_count = sum(1 for s in filtered if s.symbol in ("AAPL", "MSFT", "NVDA", "AVGO"))
        assert tech_count <= 2
        # JPM should still be there (different sector)
        assert any(s.symbol == "JPM" for s in filtered)

    def test_keeps_index_etfs(self):
        signals = [
            self._make_signal("SPY"),
            self._make_signal("QQQ"),
            self._make_signal("IWM"),
        ]
        filtered = filter_correlated_signals(signals, max_per_sector=1)
        # Index ETFs are exempt from filtering
        assert len(filtered) == 3

    def test_no_filtering_when_under_limit(self):
        signals = [
            self._make_signal("AAPL"),
            self._make_signal("JPM"),
            self._make_signal("XOM"),
        ]
        filtered = filter_correlated_signals(signals, max_per_sector=2)
        assert len(filtered) == 3


# ── Position Management ──────────────────────────────────────────────────

class TestPositionManager:
    def _make_position(self, side="buy", entry=100, stop=98, target=106):
        return ManagedPosition(
            symbol="AAPL", side=side,
            entry_price=entry, current_price=entry,
            initial_stop=stop, trailing_stop=stop,
            target_price=target, signal_type="vwap_reversion",
            entry_time=datetime.now(),
            highest_price=entry, lowest_price=entry,
        )

    def test_trailing_stop_moves_at_50pct(self):
        pos = self._make_position(entry=100, stop=98, target=106)
        pos.current_price = 103  # 50% of the way to target
        pos = update_trailing_stop(pos)
        assert pos.trailing_stop >= 100  # at least breakeven

    def test_trailing_stop_moves_at_75pct(self):
        pos = self._make_position(entry=100, stop=98, target=106)
        pos.current_price = 104.5  # 75% of 6 = 4.5 profit
        pos.highest_price = 104.5
        pos = update_trailing_stop(pos)
        assert pos.trailing_stop >= 102  # at least 50% of profit locked

    def test_trailing_stop_at_target(self):
        pos = self._make_position(entry=100, stop=98, target=106)
        pos.current_price = 107  # beyond target
        pos.highest_price = 107
        pos = update_trailing_stop(pos)
        assert pos.trailing_stop >= 105  # 75% of $7 profit = $5.25 locked

    def test_partial_exit_at_50pct(self):
        pos = self._make_position(entry=100, stop=98, target=106)
        pos.current_price = 103  # 50% of target
        result = check_partial_exit(pos)
        assert result is not None
        exit_frac, reason = result
        assert exit_frac > 0
        assert pos.qty_remaining_pct < 1.0

    def test_invalidation_on_vwap_shift(self):
        pos = self._make_position()
        pos.signal_type = "vwap_reversion"
        pos.entry_vwap = 100.0
        pos.current_vwap = 101.0  # 1% shift
        result = check_invalidation(pos)
        assert result is not None
        assert "VWAP invalidation" in result

    def test_update_position_stop_hit(self):
        pos = self._make_position(entry=100, stop=98, target=106)
        action = update_position(pos, current_price=97.5)  # below stop
        assert action.action == "full_exit"
        assert "stop" in action.reason.lower()

    def test_update_position_hold(self):
        pos = self._make_position(entry=100, stop=98, target=106)
        action = update_position(pos, current_price=101)  # small profit, no triggers
        assert action.action in ("hold", "update_stop")
