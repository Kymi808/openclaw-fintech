"""
Production P&L tracker.

Tracks:
- Per-position P&L (unrealized + realized)
- Daily portfolio returns (net of costs)
- Cumulative equity curve
- Rolling Sharpe ratio
- Max drawdown (live, not just backtest)
- Per-strategy attribution (daily vs intraday)

Persists to SQLite for crash recovery and historical analysis.
"""
import json
import sqlite3
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Optional

import numpy as np

from skills.shared import get_logger, audit_log

logger = get_logger("pnl.tracker")

DB_PATH = Path("./data/pnl.db")


@dataclass
class DailySnapshot:
    """One day's P&L summary."""
    date: str
    equity: float
    cash: float
    positions_value: float
    daily_return: float
    daily_pnl: float
    cumulative_return: float
    max_drawdown: float
    sharpe_30d: float
    n_positions: int
    gross_exposure: float
    net_exposure: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    transaction_costs: float = 0.0


class PnLTracker:
    """
    Production-grade P&L tracking with SQLite persistence.

    Survives process restarts. Provides:
    - record_snapshot(): called daily after market close
    - get_daily_returns(): for Sharpe/drawdown computation
    - get_equity_curve(): full history
    - get_current_stats(): live summary
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    date TEXT PRIMARY KEY,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL,
                    positions_value REAL NOT NULL,
                    daily_return REAL NOT NULL,
                    daily_pnl REAL NOT NULL,
                    cumulative_return REAL NOT NULL,
                    max_drawdown REAL NOT NULL,
                    sharpe_30d REAL NOT NULL,
                    n_positions INTEGER NOT NULL,
                    gross_exposure REAL NOT NULL,
                    net_exposure REAL NOT NULL,
                    realized_pnl REAL DEFAULT 0,
                    unrealized_pnl REAL DEFAULT 0,
                    transaction_costs REAL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    notional REAL NOT NULL,
                    fees REAL DEFAULT 0,
                    strategy TEXT DEFAULT 'daily',
                    order_id TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS factor_attribution (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    factor TEXT NOT NULL,
                    contribution REAL NOT NULL,
                    cumulative REAL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    qty REAL NOT NULL,
                    market_value REAL NOT NULL,
                    avg_entry_price REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    side TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def record_snapshot(
        self,
        equity: float,
        cash: float,
        positions: list[dict],
        realized_pnl: float = 0.0,
        transaction_costs: float = 0.0,
    ) -> DailySnapshot:
        """
        Record a daily P&L snapshot.

        Args:
            equity: total account equity (cash + positions)
            cash: cash balance
            positions: list of {symbol, qty, market_value, side, unrealized_pnl}
            realized_pnl: realized P&L today from closed positions
            transaction_costs: total transaction costs today
        """
        today = date.today().isoformat()
        positions_value = sum(abs(p.get("market_value", 0)) for p in positions)
        unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

        # Get previous snapshot for return calculation
        prev = self._get_latest_snapshot()
        if prev:
            daily_return = (equity - prev.equity) / prev.equity if prev.equity > 0 else 0.0
            daily_pnl = equity - prev.equity
        else:
            daily_return = 0.0
            daily_pnl = 0.0

        # Cumulative return from initial equity
        initial = self._get_initial_equity()
        cumulative_return = (equity - initial) / initial if initial > 0 else 0.0

        # Max drawdown
        peak = self._get_peak_equity()
        peak = max(peak, equity)
        dd = (equity - peak) / peak if peak > 0 else 0.0
        max_dd = min(dd, self._get_max_drawdown())

        # Rolling 30-day Sharpe
        returns = self.get_daily_returns(30)
        if len(returns) >= 5:
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(252)) if np.std(returns) > 0 else 0.0
        else:
            sharpe = 0.0

        # Exposure
        long_val = sum(abs(p["market_value"]) for p in positions if p.get("side") == "long")
        short_val = sum(abs(p["market_value"]) for p in positions if p.get("side") == "short")
        gross = (long_val + short_val) / equity if equity > 0 else 0.0
        net = (long_val - short_val) / equity if equity > 0 else 0.0

        snapshot = DailySnapshot(
            date=today,
            equity=round(equity, 2),
            cash=round(cash, 2),
            positions_value=round(positions_value, 2),
            daily_return=round(daily_return, 6),
            daily_pnl=round(daily_pnl, 2),
            cumulative_return=round(cumulative_return, 6),
            max_drawdown=round(max_dd, 6),
            sharpe_30d=round(sharpe, 3),
            n_positions=len(positions),
            gross_exposure=round(gross, 4),
            net_exposure=round(net, 4),
            realized_pnl=round(realized_pnl, 2),
            unrealized_pnl=round(unrealized, 2),
            transaction_costs=round(transaction_costs, 2),
        )

        # Persist
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_snapshots
                (date, equity, cash, positions_value, daily_return, daily_pnl,
                 cumulative_return, max_drawdown, sharpe_30d, n_positions,
                 gross_exposure, net_exposure, realized_pnl, unrealized_pnl,
                 transaction_costs)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot.date, snapshot.equity, snapshot.cash,
                snapshot.positions_value, snapshot.daily_return, snapshot.daily_pnl,
                snapshot.cumulative_return, snapshot.max_drawdown, snapshot.sharpe_30d,
                snapshot.n_positions, snapshot.gross_exposure, snapshot.net_exposure,
                snapshot.realized_pnl, snapshot.unrealized_pnl, snapshot.transaction_costs,
            ))

            # Record positions
            for p in positions:
                conn.execute("""
                    INSERT INTO position_history
                    (date, symbol, qty, market_value, avg_entry_price, unrealized_pnl, side)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    today, p["symbol"], p.get("qty", 0), p.get("market_value", 0),
                    p.get("avg_entry_price", 0), p.get("unrealized_pnl", 0),
                    p.get("side", "long"),
                ))

        audit_log("pnl-tracker", "daily_snapshot", {
            "date": today,
            "equity": snapshot.equity,
            "daily_return": f"{snapshot.daily_return:.4%}",
            "cumulative": f"{snapshot.cumulative_return:.4%}",
            "sharpe_30d": snapshot.sharpe_30d,
            "max_dd": f"{snapshot.max_drawdown:.4%}",
        })

        logger.info(
            f"P&L snapshot: equity=${snapshot.equity:,.2f}, "
            f"daily={snapshot.daily_return:+.2%}, "
            f"cumulative={snapshot.cumulative_return:+.2%}, "
            f"Sharpe={snapshot.sharpe_30d:.2f}, "
            f"maxDD={snapshot.max_drawdown:.2%}"
        )

        return snapshot

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        fees: float = 0.0,
        strategy: str = "daily",
        order_id: str = "",
    ):
        """Record a single executed trade."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO trades
                (timestamp, symbol, side, qty, price, notional, fees, strategy, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                symbol, side, qty, price, qty * price, fees, strategy, order_id,
            ))

    def get_daily_returns(self, n_days: int = 252) -> list[float]:
        """Get last N daily returns."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT daily_return FROM daily_snapshots ORDER BY date DESC LIMIT ?",
                (n_days,),
            ).fetchall()
        return [r[0] for r in reversed(rows)]

    def get_equity_curve(self) -> list[dict]:
        """Get full equity curve history."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT date, equity, daily_return, cumulative_return, max_drawdown, sharpe_30d "
                "FROM daily_snapshots ORDER BY date"
            ).fetchall()
        return [
            {
                "date": r[0], "equity": r[1], "daily_return": r[2],
                "cumulative_return": r[3], "max_drawdown": r[4], "sharpe_30d": r[5],
            }
            for r in rows
        ]

    def get_current_stats(self) -> dict:
        """Get current P&L summary."""
        snap = self._get_latest_snapshot()
        if not snap:
            return {"status": "no_data", "message": "No P&L data recorded yet."}

        returns = self.get_daily_returns(252)
        total_trades = self._count_trades()

        return {
            "date": snap.date,
            "equity": snap.equity,
            "daily_return": f"{snap.daily_return:+.2%}",
            "daily_pnl": f"${snap.daily_pnl:+,.2f}",
            "cumulative_return": f"{snap.cumulative_return:+.2%}",
            "max_drawdown": f"{snap.max_drawdown:.2%}",
            "sharpe_30d": snap.sharpe_30d,
            "n_positions": snap.n_positions,
            "gross_exposure": f"{snap.gross_exposure:.1%}",
            "net_exposure": f"{snap.net_exposure:.1%}",
            "total_trades": total_trades,
            "n_days_tracked": len(returns),
        }

    def format_report(self) -> str:
        """Human-readable P&L report."""
        stats = self.get_current_stats()
        if stats.get("status") == "no_data":
            return stats["message"]

        lines = [
            f"P&L Report — {stats['date']}",
            f"  Equity:      ${stats['equity']:,.2f}",
            f"  Daily:       {stats['daily_return']} ({stats['daily_pnl']})",
            f"  Cumulative:  {stats['cumulative_return']}",
            f"  Sharpe (30d): {stats['sharpe_30d']:.2f}",
            f"  Max DD:      {stats['max_drawdown']}",
            f"  Positions:   {stats['n_positions']}",
            f"  Exposure:    gross={stats['gross_exposure']}, net={stats['net_exposure']}",
            f"  Total trades: {stats['total_trades']}",
            f"  Days tracked: {stats['n_days_tracked']}",
        ]
        return "\n".join(lines)

    def _get_latest_snapshot(self) -> Optional[DailySnapshot]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return DailySnapshot(
            date=row[0], equity=row[1], cash=row[2], positions_value=row[3],
            daily_return=row[4], daily_pnl=row[5], cumulative_return=row[6],
            max_drawdown=row[7], sharpe_30d=row[8], n_positions=row[9],
            gross_exposure=row[10], net_exposure=row[11],
            realized_pnl=row[12] or 0, unrealized_pnl=row[13] or 0,
            transaction_costs=row[14] or 0,
        )

    def _get_initial_equity(self) -> float:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT equity FROM daily_snapshots ORDER BY date ASC LIMIT 1"
            ).fetchone()
        return row[0] if row else 100_000.0

    def _get_peak_equity(self) -> float:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(equity) FROM daily_snapshots"
            ).fetchone()
        return row[0] if row and row[0] else 0.0

    def _get_max_drawdown(self) -> float:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MIN(max_drawdown) FROM daily_snapshots"
            ).fetchone()
        return row[0] if row and row[0] else 0.0

    def _count_trades(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM trades").fetchone()
        return row[0] if row else 0


# Singleton
_tracker: Optional[PnLTracker] = None


def get_pnl_tracker() -> PnLTracker:
    global _tracker
    if _tracker is None:
        _tracker = PnLTracker()
    return _tracker
