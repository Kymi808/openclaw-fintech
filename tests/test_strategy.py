"""Tests for trading strategy logic."""
from skills.trading.strategy import (
    check_risk_limits,
    needs_approval,
    detect_arbitrage,
    simple_momentum_signal,
    format_market_update,
    format_trade_signal,
)
from skills.trading.exchange_client import Ticker


def make_ticker(pair="BTC/USDT", price=50000.0, change=0.0,
                exchange="binance") -> Ticker:
    return Ticker(
        pair=pair, price=price, volume_24h=1000000,
        change_24h_pct=change, exchange=exchange, timestamp=0,
    )


class TestRiskLimits:
    def test_within_limits(self):
        ok, reason = check_risk_limits(50.0, 0.0, 0)
        assert ok is True
        assert reason == "OK"

    def test_exceeds_single_trade(self):
        ok, reason = check_risk_limits(150.0, 0.0, 0)
        assert ok is False
        assert "max single trade" in reason.lower()

    def test_exceeds_daily_volume(self):
        ok, reason = check_risk_limits(50.0, 470.0, 0)
        assert ok is False
        assert "daily limit" in reason.lower()

    def test_max_positions(self):
        ok, reason = check_risk_limits(50.0, 0.0, 5)
        assert ok is False
        assert "position" in reason.lower()

    def test_custom_limits(self):
        limits = {"max_single_trade": 1000, "max_daily_volume": 5000, "max_open_positions": 10}
        ok, _ = check_risk_limits(500.0, 0.0, 0, limits)
        assert ok is True


class TestApprovalThreshold:
    def test_below_threshold(self):
        assert needs_approval(100.0) is False

    def test_above_threshold(self):
        assert needs_approval(250.0) is True

    def test_at_threshold(self):
        assert needs_approval(200.0) is False  # ≤ threshold, not >


class TestArbitrage:
    def test_detects_spread(self):
        tickers = {
            "binance": [make_ticker("BTC/USDT", 50000, exchange="binance")],
            "coinbase": [make_ticker("BTC/USDT", 50300, exchange="coinbase")],
        }
        opps = detect_arbitrage(tickers, min_spread_pct=0.5)
        assert len(opps) == 1
        assert opps[0].buy_exchange == "binance"
        assert opps[0].sell_exchange == "coinbase"
        assert opps[0].net_profit_usd > 0

    def test_ignores_small_spread(self):
        tickers = {
            "binance": [make_ticker("BTC/USDT", 50000, exchange="binance")],
            "coinbase": [make_ticker("BTC/USDT", 50100, exchange="coinbase")],
        }
        opps = detect_arbitrage(tickers, min_spread_pct=0.5)
        assert len(opps) == 0

    def test_ignores_unlisted_pairs(self):
        tickers = {
            "binance": [make_ticker("DOGE/USDT", 0.10, exchange="binance")],
            "coinbase": [make_ticker("DOGE/USDT", 0.20, exchange="coinbase")],
        }
        opps = detect_arbitrage(tickers)
        assert len(opps) == 0  # DOGE not in ALLOWED_PAIRS

    def test_sorts_by_profit(self):
        tickers = {
            "binance": [
                make_ticker("BTC/USDT", 50000, exchange="binance"),
                make_ticker("ETH/USDT", 3000, exchange="binance"),
            ],
            "coinbase": [
                make_ticker("BTC/USDT", 50500, exchange="coinbase"),
                make_ticker("ETH/USDT", 3100, exchange="coinbase"),
            ],
        }
        opps = detect_arbitrage(tickers, min_spread_pct=0.1)
        if len(opps) >= 2:
            assert opps[0].net_profit_usd >= opps[1].net_profit_usd


class TestMomentumSignal:
    def test_generates_buy_signal(self):
        tickers = [make_ticker("BTC/USDT", 55000, change=5.0)]
        signals = simple_momentum_signal(tickers, momentum_threshold_pct=3.0)
        assert len(signals) == 1
        assert signals[0].side == "BUY"

    def test_generates_sell_signal(self):
        tickers = [make_ticker("ETH/USDT", 2800, change=-4.0)]
        signals = simple_momentum_signal(tickers, momentum_threshold_pct=3.0)
        assert len(signals) == 1
        assert signals[0].side == "SELL"

    def test_no_signal_in_range(self):
        tickers = [make_ticker("BTC/USDT", 50000, change=1.5)]
        signals = simple_momentum_signal(tickers, momentum_threshold_pct=3.0)
        assert len(signals) == 0


class TestFormatting:
    def test_market_update_format(self):
        tickers = [
            make_ticker("BTC/USDT", 50000, change=2.5),
            make_ticker("ETH/USDT", 3000, change=-1.2),
        ]
        msg = format_market_update(tickers, daily_pnl=150.0, open_positions=2)
        assert "BTC" in msg
        assert "ETH" in msg
        assert "50,000" in msg
        assert "150.00" in msg

    def test_trade_signal_format(self):
        from skills.trading.strategy import TradeSignal
        signal = TradeSignal(
            pair="BTC/USDT", side="BUY", price=50000,
            amount_usd=100, reasoning="Strong momentum",
            risk="MEDIUM", requires_approval=False, source="momentum",
        )
        msg = format_trade_signal(signal)
        assert "BUY" in msg
        assert "BTC/USDT" in msg
        assert "MEDIUM" in msg
        assert "NO" in msg  # approval not required
