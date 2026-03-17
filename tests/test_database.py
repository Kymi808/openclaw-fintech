"""Tests for the SQLite database layer."""
import pytest
import json
import threading
from pathlib import Path
from skills.shared.database import Database


@pytest.fixture
def fresh_db(tmp_path):
    """Create a completely fresh database for each test."""
    import uuid
    db_path = str(tmp_path / f"test_{uuid.uuid4().hex[:8]}.db")
    database = Database(db_path=db_path)
    yield database
    # Cleanup thread-local connections
    if hasattr(Database._tls, "connections"):
        for key in list(Database._tls.connections.keys()):
            if db_path in key:
                try:
                    Database._tls.connections[key].close()
                except Exception:
                    pass
                del Database._tls.connections[key]


class TestAuditLog:
    def test_insert_and_query(self, fresh_db):
        row_id = fresh_db.log_audit("trading-agent", "trade_executed", {
            "pair": "BTC/USDT", "amount": 100.0,
        })
        assert row_id > 0

        logs = fresh_db.query_audit(agent="trading-agent")
        assert len(logs) == 1
        assert logs[0]["action"] == "trade_executed"
        assert "BTC/USDT" in logs[0]["details"]

    def test_query_filters(self, fresh_db):
        fresh_db.log_audit("trading-agent", "trade_executed", {"pair": "BTC"})
        fresh_db.log_audit("portfolio-agent", "rebalance", {"drift": 5.0})
        fresh_db.log_audit("trading-agent", "heartbeat", {})

        trading_logs = fresh_db.query_audit(agent="trading-agent")
        assert len(trading_logs) == 2

        heartbeat_logs = fresh_db.query_audit(action="heartbeat")
        assert len(heartbeat_logs) == 1


class TestTrades:
    def test_insert_and_query_volume(self, fresh_db):
        fresh_db.insert_trade({
            "trade_id": "T-001",
            "agent": "trading-agent",
            "exchange": "binance",
            "pair": "BTC/USDT",
            "side": "BUY",
            "amount": 0.01,
            "price": 50000.0,
            "total": 500.0,
            "status": "FILLED",
            "metadata": {"strategy": "momentum"},
        })

        volume = fresh_db.get_daily_volume("trading-agent")
        assert volume == 500.0

    def test_status_update(self, fresh_db):
        fresh_db.insert_trade({
            "trade_id": "T-002",
            "agent": "trading-agent",
            "exchange": "binance",
            "pair": "ETH/USDT",
            "side": "BUY",
            "amount": 1.0,
            "status": "PENDING",
        })

        fresh_db.update_trade_status("T-002", "FILLED", price=3000.0)

        conn = fresh_db._get_connection()
        row = conn.execute(
            "SELECT * FROM trades WHERE trade_id='T-002'"
        ).fetchone()
        assert dict(row)["status"] == "FILLED"
        assert dict(row)["price"] == 3000.0


class TestExpenses:
    def test_insert_and_query(self, fresh_db):
        fresh_db.insert_expense({
            "expense_id": "EXP-001",
            "merchant": "Starbucks",
            "amount": 5.75,
            "category": "Food & Dining",
            "date": "2026-03-16",
            "payment_method": "4111111111111234",
        })

        expenses = fresh_db.get_expenses(category="Food & Dining")
        assert len(expenses) == 1
        assert expenses[0]["merchant"] == "Starbucks"
        assert expenses[0]["payment_method"] == "4111111111111234"

    def test_payment_encrypted_in_db(self, fresh_db):
        fresh_db.insert_expense({
            "expense_id": "EXP-002",
            "merchant": "Test",
            "amount": 10.0,
            "category": "Other",
            "date": "2026-03-16",
            "payment_method": "secret-card-number",
        })

        conn = fresh_db._get_connection()
        row = conn.execute(
            "SELECT payment_method FROM expenses WHERE expense_id='EXP-002'"
        ).fetchone()
        raw = dict(row)["payment_method"]
        assert raw != "secret-card-number"
        assert raw != ""

    def test_monthly_spend(self, fresh_db):
        for i in range(3):
            fresh_db.insert_expense({
                "expense_id": f"EXP-{100+i}",
                "merchant": f"Store {i}",
                "amount": 50.0,
                "category": "Food & Dining",
                "date": "2026-03-15",
            })

        total = fresh_db.get_monthly_spend("2026-03", "Food & Dining")
        assert total == 150.0


class TestContracts:
    def test_insert_and_expiry(self, fresh_db):
        fresh_db.insert_contract({
            "contract_id": "CTR-001",
            "filename": "service-agreement.pdf",
            "parties": ["Company A", "Company B"],
            "contract_type": "Service Agreement",
            "effective_date": "2025-01-01",
            "expiration_date": "2026-04-01",
            "summary": "Confidential contract summary text",
            "risk_flags": ["auto-renewal", "no liability cap"],
        })

        expiring = fresh_db.get_expiring_contracts(within_days=30)
        assert len(expiring) == 1
        assert expiring[0]["contract_id"] == "CTR-001"
        assert expiring[0]["summary"] == "Confidential contract summary text"

    def test_summary_encrypted_in_db(self, fresh_db):
        fresh_db.insert_contract({
            "contract_id": "CTR-002",
            "filename": "nda.pdf",
            "summary": "This NDA covers proprietary algorithms",
        })

        conn = fresh_db._get_connection()
        row = conn.execute(
            "SELECT summary FROM contracts WHERE contract_id='CTR-002'"
        ).fetchone()
        raw = dict(row)["summary"]
        assert raw != "This NDA covers proprietary algorithms"


class TestRetention:
    def test_data_retention_deletes_old(self, fresh_db):
        conn = fresh_db._get_connection()
        conn.execute(
            "INSERT INTO audit_log (timestamp, agent, action) VALUES (datetime('now', '-400 days'), 'test', 'old_action')"
        )
        conn.execute(
            "INSERT INTO audit_log (timestamp, agent, action) VALUES (datetime('now'), 'test', 'new_action')"
        )
        conn.commit()

        result = fresh_db.enforce_retention(audit_days=365)
        assert result["audit_log"] == 1

        logs = fresh_db.query_audit()
        assert len(logs) == 1
        assert logs[0]["action"] == "new_action"


class TestConcurrency:
    def test_concurrent_writes(self, tmp_path):
        """Test that WAL mode handles concurrent writes from multiple threads."""
        import uuid
        db_path = str(tmp_path / f"conc_{uuid.uuid4().hex[:8]}.db")

        # Each thread gets its own Database instance pointing to the same file
        # This mirrors real production usage
        errors = []

        def write_expense(i):
            try:
                thread_db = Database(db_path=db_path)
                thread_db.insert_expense({
                    "expense_id": f"CONC-{i:04d}",
                    "merchant": f"Store {i}",
                    "amount": float(i),
                    "category": "Other",
                    "date": "2026-03-16",
                })
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_expense, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        # Verify all writes landed
        verify_db = Database(db_path=db_path)
        expenses = verify_db.get_expenses()
        assert len(expenses) == 20
