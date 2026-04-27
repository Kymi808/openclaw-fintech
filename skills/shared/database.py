"""
SQLite database layer — replaces JSON file storage.
Handles concurrent writes safely, provides queryable storage,
and supports encrypted fields for sensitive data.
"""
import sqlite3
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from .config import get_logger
from .encryption import EncryptionManager

logger = get_logger("database")

DB_PATH = Path("./data/fintech.db")


class Database:
    """Thread-safe SQLite database with per-thread connections."""

    _tls = threading.local()
    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._encryption = EncryptionManager()
        self._schema_sql = self._get_schema_sql()
        self._init_schema()

    @classmethod
    def _get_path_lock(cls, db_path: str) -> threading.RLock:
        """Return a process-local lock for one SQLite file."""
        with cls._locks_guard:
            if db_path not in cls._locks:
                cls._locks[db_path] = threading.RLock()
            return cls._locks[db_path]

    def _parse_reference_date(self, as_of: date | datetime | str | None = None) -> date:
        """Normalize user/test supplied dates to a UTC calendar date."""
        if as_of is None:
            return datetime.now(timezone.utc).date()
        if isinstance(as_of, datetime):
            return as_of.date()
        if isinstance(as_of, date):
            return as_of
        return datetime.fromisoformat(as_of).date()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local connection keyed by database path."""
        if not hasattr(Database._tls, "connections"):
            Database._tls.connections = {}
        key = self.db_path
        lock = self._get_path_lock(key)
        with lock:
            if key not in Database._tls.connections:
                conn = sqlite3.connect(
                    self.db_path,
                    timeout=30.0,
                    check_same_thread=False,
                    isolation_level=None,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=30000")
                Database._tls.connections[key] = conn
        return Database._tls.connections[key]

    @staticmethod
    def _get_schema_sql() -> str:
        """Return the schema DDL."""
        return """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                agent TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                severity TEXT DEFAULT 'INFO'
            );
            CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT UNIQUE NOT NULL,
                agent TEXT NOT NULL,
                exchange TEXT NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL NOT NULL,
                price REAL,
                total REAL,
                fee REAL DEFAULT 0,
                status TEXT NOT NULL,
                reasoning TEXT,
                approval_id TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                executed_at TEXT,
                metadata TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trades_pair ON trades(pair);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at);

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                exchange TEXT NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL,
                amount REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                pnl REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'OPEN',
                opened_at TEXT NOT NULL DEFAULT (datetime('now')),
                closed_at TEXT,
                trade_id TEXT REFERENCES trades(trade_id)
            );
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now')),
                total_value REAL NOT NULL,
                holdings TEXT NOT NULL,
                drift_detected INTEGER DEFAULT 0,
                rebalance_proposed INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                expense_id TEXT UNIQUE NOT NULL,
                merchant TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                date TEXT NOT NULL,
                payment_method TEXT,
                source TEXT DEFAULT 'manual',
                receipt_path TEXT,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category);
            CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date);

            CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_id TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                parties TEXT,
                contract_type TEXT,
                effective_date TEXT,
                expiration_date TEXT,
                summary TEXT,
                risk_flags TEXT,
                analyzed_at TEXT NOT NULL DEFAULT (datetime('now')),
                renewal_alert_sent INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_contracts_expiry ON contracts(expiration_date);

            CREATE TABLE IF NOT EXISTS sec_filings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                accession TEXT UNIQUE NOT NULL,
                company TEXT NOT NULL,
                cik TEXT NOT NULL,
                form_type TEXT NOT NULL,
                filing_date TEXT NOT NULL,
                description TEXT,
                url TEXT,
                is_material INTEGER DEFAULT 0,
                summary TEXT,
                detected_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sec_cik ON sec_filings(cik);
            CREATE INDEX IF NOT EXISTS idx_sec_date ON sec_filings(filing_date);

            CREATE TABLE IF NOT EXISTS gdpr_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                scan_date TEXT NOT NULL DEFAULT (datetime('now')),
                issues TEXT NOT NULL,
                issue_count INTEGER NOT NULL DEFAULT 0,
                high_severity_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL,
                agent TEXT NOT NULL,
                action TEXT NOT NULL,
                description TEXT,
                amount REAL,
                details TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                resolved_at TEXT,
                resolved_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

            CREATE TABLE IF NOT EXISTS defi_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                protocol TEXT NOT NULL,
                chain TEXT NOT NULL,
                position_type TEXT NOT NULL,
                tokens TEXT NOT NULL,
                value_usd REAL,
                apy_pct REAL,
                health_factor REAL,
                impermanent_loss_pct REAL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                status TEXT DEFAULT 'ACTIVE'
            );

            CREATE TABLE IF NOT EXISTS merchant_categories (
                merchant_pattern TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                learned_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_limits (
                date TEXT NOT NULL,
                agent TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (date, agent, metric)
            );

            CREATE TABLE IF NOT EXISTS retention_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                records_deleted INTEGER NOT NULL,
                retention_days INTEGER NOT NULL,
                executed_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """

    @contextmanager
    def transaction(self):
        """Context manager for atomic transactions."""
        lock = self._get_path_lock(self.db_path)
        with lock:
            conn = self._get_connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _init_schema(self):
        """Initialize all tables using stored schema SQL."""
        lock = self._get_path_lock(self.db_path)
        with lock:
            conn = self._get_connection()
            conn.executescript(self._schema_sql)
            conn.commit()
        logger.info(f"Database initialized at {self.db_path}")

    # === Audit Log ===

    def log_audit(self, agent: str, action: str, details: dict = None,
                  severity: str = "INFO") -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_log (agent, action, details, severity) VALUES (?, ?, ?, ?)",
                (agent, action, json.dumps(details or {}), severity),
            )
            return cursor.lastrowid

    def query_audit(self, agent: str = None, action: str = None,
                    since: str = None, limit: int = 100) -> list[dict]:
        conn = self._get_connection()
        query = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if agent:
            query += " AND agent = ?"
            params.append(agent)
        if action:
            query += " AND action = ?"
            params.append(action)
        if since:
            query += " AND timestamp >= ?"
            params.append(since)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in conn.execute(query, params).fetchall()]

    # === Trades ===

    def insert_trade(self, trade: dict) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO trades
                   (trade_id, agent, exchange, pair, side, amount, price, total,
                    fee, status, reasoning, approval_id, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    trade["trade_id"], trade.get("agent", "trading-agent"),
                    trade["exchange"], trade["pair"], trade["side"],
                    trade["amount"], trade.get("price"), trade.get("total"),
                    trade.get("fee", 0), trade["status"],
                    trade.get("reasoning"), trade.get("approval_id"),
                    self._encryption.encrypt(json.dumps(trade.get("metadata", {}))),
                ),
            )

    def update_trade_status(self, trade_id: str, status: str,
                            price: float = None) -> None:
        with self.transaction() as conn:
            if price:
                conn.execute(
                    "UPDATE trades SET status=?, price=?, executed_at=datetime('now') WHERE trade_id=?",
                    (status, price, trade_id),
                )
            else:
                conn.execute(
                    "UPDATE trades SET status=? WHERE trade_id=?",
                    (status, trade_id),
                )

    def get_daily_volume(self, agent: str, date: str = None) -> float:
        conn = self._get_connection()
        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute(
            "SELECT COALESCE(SUM(total), 0) as vol FROM trades WHERE agent=? AND date(created_at)=? AND status='FILLED'",
            (agent, date),
        ).fetchone()
        return row["vol"] if row else 0.0

    def get_open_positions(self, agent: str = None) -> list[dict]:
        conn = self._get_connection()
        query = "SELECT * FROM positions WHERE status='OPEN'"
        params = []
        if agent:
            query += " AND agent=?"
            params.append(agent)
        return [dict(row) for row in conn.execute(query, params).fetchall()]

    # === Expenses ===

    def insert_expense(self, expense: dict) -> None:
        with self.transaction() as conn:
            payment = expense.get("payment_method", "")
            conn.execute(
                """INSERT INTO expenses
                   (expense_id, merchant, amount, category, date,
                    payment_method, source, receipt_path, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    expense["expense_id"], expense["merchant"],
                    expense["amount"], expense["category"], expense["date"],
                    self._encryption.encrypt(payment) if payment else None,
                    expense.get("source", "manual"),
                    expense.get("receipt_path"),
                    expense.get("notes"),
                ),
            )

    def get_expenses(self, start_date: str = None, end_date: str = None,
                     category: str = None) -> list[dict]:
        conn = self._get_connection()
        query = "SELECT * FROM expenses WHERE 1=1"
        params = []
        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)
        if category:
            query += " AND category = ?"
            params.append(category)
        query += " ORDER BY date DESC"
        rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        # Decrypt payment methods
        for row in rows:
            if row.get("payment_method"):
                row["payment_method"] = self._encryption.decrypt(row["payment_method"])
        return rows

    def get_monthly_spend(self, year_month: str, category: str = None) -> float:
        conn = self._get_connection()
        query = "SELECT COALESCE(SUM(amount), 0) as total FROM expenses WHERE strftime('%Y-%m', date) = ?"
        params = [year_month]
        if category:
            query += " AND category = ?"
            params.append(category)
        row = conn.execute(query, params).fetchone()
        return row["total"] if row else 0.0

    # === Contracts ===

    def insert_contract(self, contract: dict) -> None:
        with self.transaction() as conn:
            summary = contract.get("summary", "")
            conn.execute(
                """INSERT INTO contracts
                   (contract_id, filename, parties, contract_type,
                    effective_date, expiration_date, summary, risk_flags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contract["contract_id"], contract["filename"],
                    json.dumps(contract.get("parties", [])),
                    contract.get("contract_type"),
                    contract.get("effective_date"),
                    contract.get("expiration_date"),
                    self._encryption.encrypt(summary) if summary else None,
                    json.dumps(contract.get("risk_flags", [])),
                ),
            )

    def get_expiring_contracts(
        self,
        within_days: int = 30,
        as_of: date | datetime | str | None = None,
    ) -> list[dict]:
        """Return contracts expiring soon, including recently expired agreements.

        The seven-day lookback catches contracts that expired during downtime. `as_of`
        exists so tests and operational checks can use a deterministic date.
        """
        reference_date = self._parse_reference_date(as_of)
        start_date = reference_date - timedelta(days=7)
        end_date = reference_date + timedelta(days=within_days)

        conn = self._get_connection()
        rows = conn.execute(
            """SELECT * FROM contracts
               WHERE expiration_date IS NOT NULL
               AND date(expiration_date) >= date(?)
               AND date(expiration_date) <= date(?)
               ORDER BY expiration_date ASC""",
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            if row.get("summary"):
                row["summary"] = self._encryption.decrypt(row["summary"])
        return result

    # === Data Retention ===

    def enforce_retention(self, audit_days: int = 365, snapshot_days: int = 90,
                          scan_days: int = 180) -> dict:
        """Delete old records per retention policy."""
        deleted = {}
        with self.transaction() as conn:
            for table, days in [
                ("audit_log", audit_days),
                ("portfolio_snapshots", snapshot_days),
                ("gdpr_scans", scan_days),
            ]:
                ts_col = "timestamp" if table == "audit_log" else "scan_date" if table == "gdpr_scans" else "timestamp"
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE date({ts_col}) < date('now', '-{days} days')"
                )
                deleted[table] = cursor.rowcount
                if cursor.rowcount > 0:
                    conn.execute(
                        "INSERT INTO retention_log (table_name, records_deleted, retention_days) VALUES (?, ?, ?)",
                        (table, cursor.rowcount, days),
                    )
        logger.info(f"Retention enforced: {deleted}")
        return deleted


# Singleton
db = Database()
