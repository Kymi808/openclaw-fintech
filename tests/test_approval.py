"""Tests for the approval workflow engine."""
import pytest
from skills.shared.approval import ApprovalEngine, ApprovalStatus


class TestApprovalEngine:
    def test_create_request(self, approval_engine):
        req_id = approval_engine.create_request(
            agent="trading-agent",
            action="execute_trade",
            description="BUY BTC/USDT for $300",
            amount=300.0,
        )
        assert req_id.startswith("APR-")

        pending = approval_engine.get_pending()
        assert len(pending) == 1
        assert pending[0][0] == req_id

    def test_approve_request(self, approval_engine):
        req_id = approval_engine.create_request(
            agent="trading-agent",
            action="execute_trade",
            description="BUY ETH",
            amount=250.0,
        )

        result = approval_engine.approve(req_id)
        assert result is True

        pending = approval_engine.get_pending()
        assert len(pending) == 0

    def test_deny_request(self, approval_engine):
        req_id = approval_engine.create_request(
            agent="defi-agent",
            action="swap",
            description="Swap ETH → USDC",
            amount=400.0,
        )

        result = approval_engine.deny(req_id)
        assert result is True

        pending = approval_engine.get_pending()
        assert len(pending) == 0

    def test_cannot_approve_nonexistent(self, approval_engine):
        assert approval_engine.approve("APR-999999") is False

    def test_cannot_approve_twice(self, approval_engine):
        req_id = approval_engine.create_request(
            agent="trading-agent", action="trade",
            description="test", amount=100.0,
        )
        approval_engine.approve(req_id)
        assert approval_engine.approve(req_id) is False

    def test_auto_approve_logic(self, approval_engine):
        limits = {"approval_threshold": 200.0}

        assert approval_engine.should_auto_approve(
            "trading-agent", "execute_trade", 100.0, limits
        ) is True
        assert approval_engine.should_auto_approve(
            "trading-agent", "execute_trade", 300.0, limits
        ) is False
        assert approval_engine.should_auto_approve(
            "portfolio-agent", "rebalance", 50.0, limits
        ) is False  # Rebalance always requires approval
        assert approval_engine.should_auto_approve(
            "defi-agent", "governance_vote", 0.0, limits
        ) is False  # Governance always requires approval

    def test_format_message(self, approval_engine):
        req_id = approval_engine.create_request(
            agent="trading-agent",
            action="execute_trade",
            description="BUY 0.5 BTC at $50,000",
            amount=25000.0,
        )

        msg = approval_engine.format_request_message(req_id)
        assert "Approval Required" in msg
        assert req_id in msg
        assert "$25,000.00" in msg
        assert "approve" in msg.lower()
        assert "deny" in msg.lower()

    def test_multiple_pending_requests(self, approval_engine):
        ids = []
        for i in range(5):
            req_id = approval_engine.create_request(
                agent="trading-agent",
                action="trade",
                description=f"Trade {i}",
                amount=float(i * 100),
            )
            ids.append(req_id)

        pending = approval_engine.get_pending()
        assert len(pending) == 5

        # Approve first two
        approval_engine.approve(ids[0])
        approval_engine.approve(ids[1])

        pending = approval_engine.get_pending()
        assert len(pending) == 3
