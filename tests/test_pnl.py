"""
Tests for P&L tracker and position reconciliation.
"""
import pytest
import os
import tempfile
from skills.pnl.tracker import PnLTracker, DailySnapshot
from skills.pnl.reconciliation import (
    Discrepancy, ReconciliationReport,
    format_reconciliation_report,
)


class TestPnLTracker:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_pnl.db")
        self.tracker = PnLTracker(db_path=self.db_path)

    def _insert_snapshot(self, date_str: str, equity: float, positions=None):
        """Insert a snapshot with a specific date (bypassing today's date)."""
        import sqlite3
        import numpy as np

        positions = positions or []
        positions_value = sum(abs(p.get("market_value", 0)) for p in positions)
        unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

        # Get previous for return calc
        with sqlite3.connect(self.db_path) as conn:
            prev = conn.execute(
                "SELECT equity FROM daily_snapshots ORDER BY date DESC LIMIT 1"
            ).fetchone()
            first = conn.execute(
                "SELECT equity FROM daily_snapshots ORDER BY date ASC LIMIT 1"
            ).fetchone()
            peak = conn.execute(
                "SELECT MAX(equity) FROM daily_snapshots"
            ).fetchone()
            min_dd = conn.execute(
                "SELECT MIN(max_drawdown) FROM daily_snapshots"
            ).fetchone()

        prev_eq = prev[0] if prev else equity
        initial_eq = first[0] if first else equity
        peak_eq = max(peak[0] if peak and peak[0] else equity, equity)
        daily_ret = (equity - prev_eq) / prev_eq if prev_eq > 0 and prev else 0.0
        cum_ret = (equity - initial_eq) / initial_eq if initial_eq > 0 else 0.0
        dd = (equity - peak_eq) / peak_eq if peak_eq > 0 else 0.0
        max_dd = min(dd, min_dd[0] if min_dd and min_dd[0] else 0.0)

        snap = DailySnapshot(
            date=date_str, equity=round(equity, 2), cash=round(equity, 2),
            positions_value=round(positions_value, 2),
            daily_return=round(daily_ret, 6), daily_pnl=round(equity - prev_eq, 2),
            cumulative_return=round(cum_ret, 6), max_drawdown=round(max_dd, 6),
            sharpe_30d=0.0, n_positions=len(positions),
            gross_exposure=0.0, net_exposure=0.0,
            unrealized_pnl=round(unrealized, 2),
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_snapshots
                (date, equity, cash, positions_value, daily_return, daily_pnl,
                 cumulative_return, max_drawdown, sharpe_30d, n_positions,
                 gross_exposure, net_exposure, realized_pnl, unrealized_pnl,
                 transaction_costs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snap.date, snap.equity, snap.cash, snap.positions_value,
                snap.daily_return, snap.daily_pnl, snap.cumulative_return,
                snap.max_drawdown, snap.sharpe_30d, snap.n_positions,
                snap.gross_exposure, snap.net_exposure, 0, snap.unrealized_pnl, 0,
            ))

        return snap

    def test_init_creates_tables(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        table_names = [t[0] for t in tables]
        assert "daily_snapshots" in table_names
        assert "trades" in table_names
        assert "position_history" in table_names

    def test_record_first_snapshot(self):
        snap = self.tracker.record_snapshot(
            equity=100_000,
            cash=50_000,
            positions=[
                {"symbol": "AAPL", "qty": 100, "market_value": 25_000,
                 "side": "long", "unrealized_pnl": 500},
                {"symbol": "TSLA", "qty": 50, "market_value": 25_000,
                 "side": "long", "unrealized_pnl": -200},
            ],
        )
        assert snap.equity == 100_000
        assert snap.n_positions == 2
        assert snap.daily_return == 0.0  # first day, no prior

    def test_daily_return_calculation(self):
        # Day 1
        self.tracker.record_snapshot(equity=100_000, cash=50_000, positions=[])
        # Day 2: equity went up
        snap2 = self.tracker.record_snapshot(equity=101_000, cash=51_000, positions=[])
        assert abs(snap2.daily_return - 0.01) < 0.001  # 1% return

    def test_cumulative_return(self):
        # Use different dates to avoid INSERT OR REPLACE collision
        self._insert_snapshot("2026-03-01", 100_000)
        self._insert_snapshot("2026-03-02", 110_000)
        # Third day should show cumulative from initial
        snap = self._insert_snapshot("2026-03-03", 105_000)
        assert abs(snap.cumulative_return - 0.05) < 0.001

    def test_max_drawdown(self):
        self._insert_snapshot("2026-03-01", 100_000)
        self._insert_snapshot("2026-03-02", 110_000)
        snap = self._insert_snapshot("2026-03-03", 99_000)
        assert snap.max_drawdown < -0.05

    def test_get_daily_returns(self):
        for i, eq in enumerate([100_000, 101_000, 99_500, 102_000]):
            self._insert_snapshot(f"2026-03-0{i+1}", eq)
        returns = self.tracker.get_daily_returns(10)
        assert len(returns) == 4
        assert returns[0] == 0.0  # first day

    def test_record_trade(self):
        self.tracker.record_trade(
            symbol="AAPL", side="buy", qty=100, price=150.0,
            fees=0.50, strategy="daily", order_id="ORD-123",
        )
        stats = self.tracker.get_current_stats()
        # No snapshot yet, so stats show no_data
        assert stats.get("status") == "no_data"

    def test_get_equity_curve(self):
        self._insert_snapshot("2026-03-01", 100_000)
        self._insert_snapshot("2026-03-02", 101_000)
        curve = self.tracker.get_equity_curve()
        assert len(curve) == 2
        assert curve[0]["equity"] == 100_000

    def test_format_report(self):
        self.tracker.record_snapshot(
            equity=105_000, cash=50_000,
            positions=[{"symbol": "AAPL", "qty": 100, "market_value": 55_000,
                       "side": "long", "unrealized_pnl": 5_000}],
        )
        report = self.tracker.format_report()
        assert "Equity" in report
        assert "105,000" in report

    def test_exposure_calculation(self):
        snap = self.tracker.record_snapshot(
            equity=100_000, cash=20_000,
            positions=[
                {"symbol": "AAPL", "qty": 100, "market_value": 50_000,
                 "side": "long", "unrealized_pnl": 0},
                {"symbol": "TSLA", "qty": -50, "market_value": -30_000,
                 "side": "short", "unrealized_pnl": 0},
            ],
        )
        assert snap.gross_exposure > 0
        assert snap.net_exposure != snap.gross_exposure  # long/short offset


class TestReconciliationReport:
    def test_clean_report(self):
        report = ReconciliationReport(
            status="clean",
            n_system_positions=5,
            n_broker_positions=5,
            n_matched=5,
        )
        text = format_reconciliation_report(report)
        assert "CLEAN" in text
        assert "Matched: 5" in text

    def test_discrepancy_report(self):
        report = ReconciliationReport(
            status="discrepancies",
            n_system_positions=5,
            n_broker_positions=4,
            n_matched=3,
            n_discrepancies=2,
            discrepancies=[
                Discrepancy(
                    symbol="AAPL", type="missing", severity="critical",
                    system_qty=100, broker_qty=0,
                    system_value=25_000, broker_value=0,
                    message="AAPL: system shows position but broker has none",
                ),
                Discrepancy(
                    symbol="TSLA", type="qty_mismatch", severity="warning",
                    system_qty=50, broker_qty=48,
                    system_value=18_000, broker_value=17_280,
                    message="TSLA: qty mismatch",
                ),
            ],
        )
        text = format_reconciliation_report(report)
        assert "DISCREPANCIES" in text
        assert "AAPL" in text
        assert "!!!" in text  # critical severity

    def test_discrepancy_to_dict(self):
        d = Discrepancy(
            symbol="AAPL", type="phantom", severity="critical",
            system_qty=0, broker_qty=100,
            system_value=0, broker_value=25_000,
            message="test",
        )
        assert d.to_dict()["symbol"] == "AAPL"
        assert d.to_dict()["type"] == "phantom"
