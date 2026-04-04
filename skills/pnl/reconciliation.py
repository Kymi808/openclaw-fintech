"""
Position reconciliation — verifies system state matches Alpaca.

Runs daily after market close to detect drift:
- Missing positions (system thinks we have it, Alpaca doesn't)
- Phantom positions (Alpaca has it, system doesn't know)
- Quantity mismatches
- Value discrepancies > threshold

Produces a reconciliation report with discrepancies and recommended actions.
"""
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from skills.shared import get_logger, audit_log

logger = get_logger("pnl.reconciliation")

# Drift threshold — flag if position value differs by more than this
VALUE_DRIFT_THRESHOLD = 0.05  # 5%
QTY_DRIFT_THRESHOLD = 0.01   # 1% qty difference


@dataclass
class Discrepancy:
    """A single position discrepancy between system and broker."""
    symbol: str
    type: str       # "missing", "phantom", "qty_mismatch", "value_mismatch"
    severity: str   # "critical", "warning", "info"
    system_qty: float
    broker_qty: float
    system_value: float
    broker_value: float
    message: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class ReconciliationReport:
    """Full reconciliation report."""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "clean"  # "clean", "discrepancies", "error"
    n_system_positions: int = 0
    n_broker_positions: int = 0
    n_matched: int = 0
    n_discrepancies: int = 0
    total_system_value: float = 0.0
    total_broker_value: float = 0.0
    discrepancies: list[Discrepancy] = field(default_factory=list)
    broker_equity: float = 0.0
    broker_cash: float = 0.0

    def to_dict(self) -> dict:
        return {
            **{k: v for k, v in self.__dict__.items() if k != "discrepancies"},
            "discrepancies": [d.to_dict() for d in self.discrepancies],
        }


async def _fetch_alpaca_positions() -> tuple[list[dict], float, float]:
    """Fetch all positions and account info from Alpaca."""
    api_key = os.getenv("ALPACA_API_KEY", "")
    api_secret = os.getenv("ALPACA_API_SECRET", "")
    base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        },
        timeout=15.0,
    ) as client:
        # Get account
        acct_resp = await client.get("/v2/account")
        acct_resp.raise_for_status()
        acct = acct_resp.json()
        equity = float(acct.get("equity", 0))
        cash = float(acct.get("cash", 0))

        # Get positions
        pos_resp = await client.get("/v2/positions")
        pos_resp.raise_for_status()
        positions = []
        for p in pos_resp.json():
            positions.append({
                "symbol": p["symbol"],
                "qty": float(p.get("qty", 0)),
                "market_value": float(p.get("market_value", 0)),
                "avg_entry_price": float(p.get("avg_entry_price", 0)),
                "unrealized_pnl": float(p.get("unrealized_pl", 0)),
                "side": "long" if float(p.get("qty", 0)) > 0 else "short",
                "current_price": float(p.get("current_price", 0)),
            })

        return positions, equity, cash


async def reconcile_positions(
    system_positions: list[dict] = None,
) -> ReconciliationReport:
    """
    Reconcile system state against Alpaca broker positions.

    Args:
        system_positions: list of {symbol, qty, market_value, side}
                         from the execution agent's state.
                         If None, reads from execution agent state file.

    Returns:
        ReconciliationReport with any discrepancies.
    """
    report = ReconciliationReport()

    # Load system positions
    if system_positions is None:
        system_positions = _load_system_positions()

    # Fetch broker positions
    try:
        broker_positions, equity, cash = await _fetch_alpaca_positions()
        report.broker_equity = equity
        report.broker_cash = cash
    except Exception as e:
        logger.error(f"Failed to fetch Alpaca positions: {e}")
        report.status = "error"
        report.discrepancies.append(Discrepancy(
            symbol="N/A", type="error", severity="critical",
            system_qty=0, broker_qty=0, system_value=0, broker_value=0,
            message=f"Could not fetch broker positions: {e}",
        ))
        return report

    # Index by symbol
    sys_map = {p["symbol"]: p for p in system_positions}
    broker_map = {p["symbol"]: p for p in broker_positions}

    report.n_system_positions = len(sys_map)
    report.n_broker_positions = len(broker_map)
    report.total_system_value = sum(abs(p.get("market_value", 0)) for p in system_positions)
    report.total_broker_value = sum(abs(p.get("market_value", 0)) for p in broker_positions)

    all_symbols = set(sys_map.keys()) | set(broker_map.keys())
    matched = 0

    for symbol in sorted(all_symbols):
        sys_pos = sys_map.get(symbol)
        broker_pos = broker_map.get(symbol)

        if sys_pos and not broker_pos:
            # System thinks we have it, broker doesn't
            report.discrepancies.append(Discrepancy(
                symbol=symbol, type="missing", severity="critical",
                system_qty=sys_pos.get("qty", 0), broker_qty=0,
                system_value=sys_pos.get("market_value", 0), broker_value=0,
                message=f"{symbol}: system shows position but broker has none",
            ))
            continue

        if broker_pos and not sys_pos:
            # Broker has it, system doesn't know
            report.discrepancies.append(Discrepancy(
                symbol=symbol, type="phantom", severity="critical",
                system_qty=0, broker_qty=broker_pos.get("qty", 0),
                system_value=0, broker_value=broker_pos.get("market_value", 0),
                message=f"{symbol}: broker has position but system is unaware",
            ))
            continue

        # Both exist — check for qty/value mismatch
        sys_qty = sys_pos.get("qty", 0)
        broker_qty = broker_pos.get("qty", 0)
        sys_val = abs(sys_pos.get("market_value", 0))
        broker_val = abs(broker_pos.get("market_value", 0))

        if sys_qty != 0 and abs(sys_qty - broker_qty) / abs(sys_qty) > QTY_DRIFT_THRESHOLD:
            report.discrepancies.append(Discrepancy(
                symbol=symbol, type="qty_mismatch", severity="warning",
                system_qty=sys_qty, broker_qty=broker_qty,
                system_value=sys_val, broker_value=broker_val,
                message=f"{symbol}: qty mismatch — system={sys_qty:.4f}, broker={broker_qty:.4f}",
            ))
        elif sys_val > 0 and abs(sys_val - broker_val) / sys_val > VALUE_DRIFT_THRESHOLD:
            report.discrepancies.append(Discrepancy(
                symbol=symbol, type="value_mismatch", severity="info",
                system_qty=sys_qty, broker_qty=broker_qty,
                system_value=sys_val, broker_value=broker_val,
                message=f"{symbol}: value drift — system=${sys_val:,.2f}, broker=${broker_val:,.2f}",
            ))
        else:
            matched += 1

    report.n_matched = matched
    report.n_discrepancies = len(report.discrepancies)
    report.status = "clean" if report.n_discrepancies == 0 else "discrepancies"

    # Log results
    if report.discrepancies:
        for d in report.discrepancies:
            log_fn = logger.error if d.severity == "critical" else logger.warning
            log_fn(f"RECONCILIATION: {d.message}")

    audit_log("pnl-reconciliation", "reconcile", {
        "status": report.status,
        "matched": report.n_matched,
        "discrepancies": report.n_discrepancies,
        "system_positions": report.n_system_positions,
        "broker_positions": report.n_broker_positions,
    })

    logger.info(
        f"Reconciliation: {report.n_matched} matched, "
        f"{report.n_discrepancies} discrepancies "
        f"(system={report.n_system_positions}, broker={report.n_broker_positions})"
    )

    return report


def format_reconciliation_report(report: ReconciliationReport) -> str:
    """Human-readable reconciliation report."""
    lines = [
        f"Position Reconciliation — {report.timestamp[:10]}",
        f"  Status: {report.status.upper()}",
        f"  Broker equity: ${report.broker_equity:,.2f} (cash: ${report.broker_cash:,.2f})",
        f"  System positions: {report.n_system_positions}",
        f"  Broker positions: {report.n_broker_positions}",
        f"  Matched: {report.n_matched}",
    ]

    if report.discrepancies:
        lines.append(f"  Discrepancies: {report.n_discrepancies}")
        lines.append("")
        for d in report.discrepancies:
            severity_icon = {"critical": "!!!", "warning": " ! ", "info": "   "}
            lines.append(f"  [{severity_icon.get(d.severity, '   ')}] {d.message}")
    else:
        lines.append("  All positions reconciled.")

    return "\n".join(lines)


def _load_system_positions() -> list[dict]:
    """Load system positions from execution agent state."""
    import json
    from pathlib import Path
    state_file = Path("./workspaces/execution-agent/state.json")
    if not state_file.exists():
        return []
    state = json.loads(state_file.read_text())
    # Combine overnight + intraday positions
    positions = []
    for p in state.get("overnight_positions", []):
        positions.append({
            "symbol": p.get("symbol", ""),
            "qty": p.get("qty", 0),
            "market_value": p.get("notional", 0),
            "side": "long" if p.get("notional", 0) > 0 else "short",
        })
    for p in state.get("intraday_positions", []):
        positions.append({
            "symbol": p.get("symbol", ""),
            "qty": p.get("qty", 0),
            "market_value": p.get("notional", 0),
            "side": "long" if p.get("notional", 0) > 0 else "short",
        })
    return positions
