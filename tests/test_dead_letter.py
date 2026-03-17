"""Tests for the dead letter queue."""
import pytest
from skills.shared.dead_letter import DeadLetterQueue, FailureType, DLQStatus


@pytest.fixture
def fresh_dlq():
    q = DeadLetterQueue.__new__(DeadLetterQueue)
    q._entries = {}
    q._counter = 0
    return q


class TestDeadLetterQueue:
    def test_enqueue(self, fresh_dlq):
        entry_id = fresh_dlq.enqueue(
            agent="trading-agent",
            failure_type=FailureType.TRADE_EXECUTION,
            description="Binance order failed",
            original_action={"pair": "BTC/USDT", "side": "BUY", "amount": 100},
            error="Connection timeout",
        )
        assert entry_id.startswith("DLQ-")
        assert len(fresh_dlq.get_pending()) == 1

    def test_resolve(self, fresh_dlq):
        entry_id = fresh_dlq.enqueue(
            agent="trading-agent",
            failure_type=FailureType.TRADE_EXECUTION,
            description="Failed trade",
            original_action={"pair": "ETH/USDT"},
            error="Timeout",
        )

        result = fresh_dlq.resolve(entry_id, "Manually executed on exchange")
        assert result is True
        assert len(fresh_dlq.get_pending()) == 0

        entry = fresh_dlq.get_entry(entry_id)
        assert entry.status == DLQStatus.RESOLVED
        assert entry.resolved_at is not None

    def test_abandon(self, fresh_dlq):
        entry_id = fresh_dlq.enqueue(
            agent="defi-agent",
            failure_type=FailureType.SWAP_EXECUTION,
            description="Swap failed",
            original_action={"token_in": "ETH", "amount": 0.5},
            error="Gas too high",
        )

        fresh_dlq.abandon(entry_id, "Gas stayed too high, opportunity passed")
        entry = fresh_dlq.get_entry(entry_id)
        assert entry.status == DLQStatus.ABANDONED

    def test_escalate(self, fresh_dlq):
        entry_id = fresh_dlq.enqueue(
            agent="trading-agent",
            failure_type=FailureType.ARBITRAGE_PARTIAL,
            description="Buy on Binance succeeded, sell on Coinbase failed",
            original_action={"pair": "BTC/USDT", "buy_exchange": "binance"},
            partial_result={"buy_order_id": "12345", "amount": 0.01},
            error="Coinbase API error",
        )

        fresh_dlq.escalate(entry_id, "Partial arbitrage — open position on Binance")
        entry = fresh_dlq.get_entry(entry_id)
        assert entry.status == DLQStatus.ESCALATED

    def test_retry_counting(self, fresh_dlq):
        entry_id = fresh_dlq.enqueue(
            agent="trading-agent",
            failure_type=FailureType.TRADE_EXECUTION,
            description="Failed",
            original_action={},
            max_retries=3,
        )

        assert fresh_dlq.mark_retrying(entry_id) is True  # retry 1
        assert fresh_dlq.mark_retrying(entry_id) is True  # retry 2
        assert fresh_dlq.mark_retrying(entry_id) is True  # retry 3
        assert fresh_dlq.mark_retrying(entry_id) is False  # max reached → escalated

        entry = fresh_dlq.get_entry(entry_id)
        assert entry.status == DLQStatus.ESCALATED

    def test_filter_by_agent(self, fresh_dlq):
        fresh_dlq.enqueue("trading-agent", FailureType.TRADE_EXECUTION,
                          "Trade 1", {})
        fresh_dlq.enqueue("defi-agent", FailureType.SWAP_EXECUTION,
                          "Swap 1", {})
        fresh_dlq.enqueue("trading-agent", FailureType.TRADE_EXECUTION,
                          "Trade 2", {})

        trading = fresh_dlq.get_pending(agent="trading-agent")
        assert len(trading) == 2

        defi = fresh_dlq.get_pending(agent="defi-agent")
        assert len(defi) == 1

    def test_format_alert(self, fresh_dlq):
        entry_id = fresh_dlq.enqueue(
            agent="trading-agent",
            failure_type=FailureType.ARBITRAGE_PARTIAL,
            description="Partial arb execution",
            original_action={"pair": "BTC/USDT"},
            partial_result={"buy_order_id": "123"},
            error="Sell side failed",
        )

        entry = fresh_dlq.get_entry(entry_id)
        msg = fresh_dlq.format_alert(entry)
        assert "🚨" in msg  # Critical for arbitrage_partial
        assert entry_id in msg
        assert "retry" in msg.lower()
        assert "abandon" in msg.lower()

    def test_stats(self, fresh_dlq):
        fresh_dlq.enqueue("a", FailureType.TRADE_EXECUTION, "1", {})
        fresh_dlq.enqueue("a", FailureType.TRADE_EXECUTION, "2", {})
        entry_id = fresh_dlq.enqueue("a", FailureType.SWAP_EXECUTION, "3", {})
        fresh_dlq.resolve(entry_id)

        stats = fresh_dlq.get_stats()
        assert stats["total"] == 3
        assert stats["pending_count"] == 2
        assert stats["by_type"]["trade_execution"] == 2
        assert stats["by_type"]["swap_execution"] == 1
