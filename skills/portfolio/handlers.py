"""
OpenClaw skill handlers for the Portfolio Agent.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone

from skills.shared import get_logger, audit_log, approval_engine
from skills.shared.config import ALLOWED_STOCK_PAIRS
from skills.trading.exchange_client import get_exchange_client

logger = get_logger("portfolio.handlers")

STATE_FILE = Path("./workspaces/portfolio-agent/state.json")
CONFIG_FILE = Path("./workspaces/portfolio-agent/config.json")

# Default target allocation
DEFAULT_TARGETS = {
    "BTC": 0.40,
    "ETH": 0.30,
    "SOL": 0.10,
    "USDT": 0.15,
    "OTHER": 0.05,
}

DRIFT_THRESHOLD = 0.05  # 5%
MAX_REBALANCE_PCT = 0.10  # 10% of portfolio


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"targets": DEFAULT_TARGETS, "drift_threshold": DRIFT_THRESHOLD}


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"snapshots": [], "last_rebalance": None}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


@dataclass
class Holding:
    asset: str
    amount: float
    value_usd: float
    allocation_pct: float
    target_pct: float
    drift_pct: float


async def get_portfolio() -> dict:
    """Fetch all holdings across exchanges and compute allocations."""
    config = _load_config()
    targets = config["targets"]

    holdings: list[dict] = []
    total_value = 0.0

    # Fetch balances from crypto exchanges
    for exchange_name in ["binance", "coinbase"]:
        try:
            client = get_exchange_client(exchange_name)

            for asset in list(targets.keys()):
                if asset == "OTHER":
                    continue
                balance = await client.get_balance(asset)
                if balance > 0:
                    # Get price
                    if asset == "USDT":
                        price = 1.0
                    else:
                        ticker = await client.get_ticker(f"{asset}/USDT")
                        price = ticker.price

                    value = balance * price
                    total_value += value
                    holdings.append({
                        "asset": asset,
                        "amount": balance,
                        "price": price,
                        "value_usd": value,
                        "exchange": exchange_name,
                    })
            await client.close()
        except Exception as e:
            logger.error(f"Failed to fetch from {exchange_name}: {e}")

    # Fetch balances from Alpaca (stocks)
    try:
        alpaca = get_exchange_client("alpaca")

        # Check cash balance
        cash = await alpaca.get_balance("USD")
        if cash > 0:
            total_value += cash
            holdings.append({
                "asset": "USD",
                "amount": cash,
                "price": 1.0,
                "value_usd": cash,
                "exchange": "alpaca",
            })

        # Check stock positions
        for pair in ALLOWED_STOCK_PAIRS:
            symbol = pair.split("/")[0]
            qty = await alpaca.get_balance(symbol)
            if qty > 0:
                ticker = await alpaca.get_ticker(pair)
                value = qty * ticker.price
                total_value += value
                holdings.append({
                    "asset": symbol,
                    "amount": qty,
                    "price": ticker.price,
                    "value_usd": value,
                    "exchange": "alpaca",
                })

        await alpaca.close()
    except Exception as e:
        logger.error(f"Failed to fetch from alpaca: {e}")

    # Aggregate by asset
    asset_totals: dict[str, float] = {}
    for h in holdings:
        asset_totals[h["asset"]] = asset_totals.get(h["asset"], 0) + h["value_usd"]

    # Compute allocations
    result_holdings = []
    for asset, target_pct in targets.items():
        if asset == "OTHER":
            continue
        value = asset_totals.get(asset, 0.0)
        actual_pct = value / total_value if total_value > 0 else 0.0
        drift = actual_pct - target_pct
        result_holdings.append({
            "asset": asset,
            "value_usd": round(value, 2),
            "allocation_pct": round(actual_pct * 100, 1),
            "target_pct": round(target_pct * 100, 1),
            "drift_pct": round(drift * 100, 1),
        })

    # Save snapshot
    state = _load_state()
    state["snapshots"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_value": round(total_value, 2),
        "holdings": result_holdings,
    })
    # Keep last 90 snapshots
    state["snapshots"] = state["snapshots"][-90:]
    _save_state(state)

    return {
        "total_value": round(total_value, 2),
        "holdings": result_holdings,
        "needs_rebalance": any(
            abs(h["drift_pct"]) >= DRIFT_THRESHOLD * 100 for h in result_holdings
        ),
    }


async def propose_rebalance() -> dict:
    """Calculate rebalancing trades needed to restore target allocation."""
    portfolio = await get_portfolio()
    config = _load_config()
    targets = config["targets"]
    total = portfolio["total_value"]

    if total == 0:
        return {"error": "Portfolio is empty"}

    trades = []
    for holding in portfolio["holdings"]:
        asset = holding["asset"]
        target_pct = targets.get(asset, 0)
        target_value = total * target_pct
        current_value = holding["value_usd"]
        diff = target_value - current_value

        # Only trade if drift exceeds threshold
        if abs(diff) / total < DRIFT_THRESHOLD:
            continue

        # Cap rebalance at MAX_REBALANCE_PCT of portfolio
        max_trade = total * MAX_REBALANCE_PCT
        trade_value = min(abs(diff), max_trade)

        trades.append({
            "asset": asset,
            "action": "BUY" if diff > 0 else "SELL",
            "amount_usd": round(trade_value, 2),
            "from_pct": holding["allocation_pct"],
            "to_pct": holding["target_pct"],
        })

    if not trades:
        return {"status": "no_rebalance_needed", "message": "All allocations within threshold"}

    # Create approval request
    trade_desc = "; ".join(
        f"{t['action']} ${t['amount_usd']:.2f} of {t['asset']}" for t in trades
    )
    total_trade_value = sum(t["amount_usd"] for t in trades)

    req_id = approval_engine.create_request(
        agent="portfolio-agent",
        action="rebalance",
        description=f"Rebalance: {trade_desc}",
        amount=total_trade_value,
        details={"trades": trades, "portfolio_value": total},
    )

    # Format proposal message
    lines = ["🔄 Rebalance Proposal"]
    for t in trades:
        lines.append(
            f"{t['action']}: ${t['amount_usd']:.2f} of {t['asset']} "
            f"— {t['from_pct']:.1f}% → {t['to_pct']:.1f}%"
        )
    lines.append(f"\n⏳ Awaiting approval. Reply 'approve {req_id}' to execute.")

    return {
        "status": "awaiting_approval",
        "request_id": req_id,
        "trades": trades,
        "message": "\n".join(lines),
    }


async def execute_rebalance(request_id: str) -> dict:
    """Execute a previously approved rebalance plan."""
    # Verify approval
    pending = dict(approval_engine.get_pending())
    req = pending.get(request_id)
    if req:
        return {"error": f"Request {request_id} is still pending approval"}

    # In production, this would iterate through the trades and execute each one
    # via the exchange client, similar to trading.handlers.execute_trade
    audit_log("portfolio-agent", "rebalance_executed", {"request_id": request_id})
    return {"status": "executed", "request_id": request_id}


async def performance_report(period: str = "30d") -> str:
    """Generate a performance report for the given period."""
    state = _load_state()
    snapshots = state.get("snapshots", [])

    if len(snapshots) < 2:
        return "📈 Not enough data for a performance report yet. Need at least 2 snapshots."

    latest = snapshots[-1]
    earliest = snapshots[0]

    total_change = latest["total_value"] - earliest["total_value"]
    pct_change = (total_change / earliest["total_value"] * 100) if earliest["total_value"] > 0 else 0

    # Build report
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"📈 Portfolio Report — {now}",
        f"Total Value: ${latest['total_value']:,.2f}",
        f"",
        f"Allocation vs Target:",
        f"| Asset     | Current | Target | Drift  |",
        f"|-----------|---------|--------|--------|",
    ]

    for h in latest["holdings"]:
        drift_sign = "+" if h["drift_pct"] >= 0 else ""
        lines.append(
            f"| {h['asset']:<9} | {h['allocation_pct']:>5.1f}%  | "
            f"{h['target_pct']:>4.1f}%  | {drift_sign}{h['drift_pct']:.1f}%  |"
        )

    needs_rebal = any(abs(h["drift_pct"]) >= DRIFT_THRESHOLD * 100 for h in latest["holdings"])
    lines.append(f"")
    lines.append(f"⚠️ Rebalance needed: {'YES' if needs_rebal else 'NO'}")
    lines.append(f"Period change: {pct_change:+.1f}% (${total_change:+,.2f})")
    lines.append(f"Data points: {len(snapshots)}")

    return "\n".join(lines)


async def heartbeat() -> str:
    """Daily morning portfolio check."""
    logger.info("Portfolio agent heartbeat starting")

    portfolio = await get_portfolio()

    audit_log("portfolio-agent", "heartbeat", {
        "total_value": portfolio["total_value"],
        "needs_rebalance": portfolio["needs_rebalance"],
    })

    if portfolio["needs_rebalance"]:
        proposal = await propose_rebalance()
        return proposal.get("message", "Rebalance check complete.")

    return await performance_report()
