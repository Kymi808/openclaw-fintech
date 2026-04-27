"""
Tests for execution order manager.
"""
from skills.execution.order_manager import OrderStatus, FILL_PRICE_DEVIATION


class TestOrderStatus:
    def test_is_complete(self):
        for status in ("filled", "canceled", "rejected"):
            os = OrderStatus(
                order_id="123", client_order_id="c123",
                symbol="AAPL", side="buy", status=status,
                requested_notional=1000,
            )
            assert os.is_complete is True

    def test_not_complete(self):
        for status in ("new", "partially_filled", "timeout"):
            os = OrderStatus(
                order_id="123", client_order_id="c123",
                symbol="AAPL", side="buy", status=status,
                requested_notional=1000,
            )
            assert os.is_complete is False

    def test_is_success(self):
        os = OrderStatus(
            order_id="123", client_order_id="c123",
            symbol="AAPL", side="buy", status="filled",
            requested_notional=1000, filled_qty=10, filled_avg_price=100,
        )
        assert os.is_success is True

    def test_not_success_when_rejected(self):
        os = OrderStatus(
            order_id="123", client_order_id="c123",
            symbol="AAPL", side="buy", status="rejected",
            requested_notional=1000, error="Insufficient funds",
        )
        assert os.is_success is False

    def test_to_dict(self):
        os = OrderStatus(
            order_id="123", client_order_id="c123",
            symbol="AAPL", side="buy", status="filled",
            requested_notional=1000, filled_qty=10,
            filled_avg_price=100, filled_notional=1000,
        )
        d = os.to_dict()
        assert d["symbol"] == "AAPL"
        assert d["is_complete"] is True
        assert d["is_success"] is True

    def test_fill_deviation_threshold(self):
        # FILL_PRICE_DEVIATION is 0.02 (2%)
        assert FILL_PRICE_DEVIATION == 0.02

    def test_partial_fill(self):
        os = OrderStatus(
            order_id="123", client_order_id="c123",
            symbol="AAPL", side="buy", status="partially_filled",
            requested_notional=10000,
            filled_qty=50, filled_avg_price=150,
            filled_notional=7500, remaining_qty=16.67,
        )
        assert not os.is_complete
        assert os.filled_notional == 7500
        assert os.remaining_qty > 0

    def test_retry_tracking(self):
        os = OrderStatus(
            order_id="123", client_order_id="c123",
            symbol="AAPL", side="buy", status="rejected",
            requested_notional=1000, attempts=3,
            error="All 3 attempts failed",
        )
        assert os.attempts == 3
        assert "3 attempts" in os.error
