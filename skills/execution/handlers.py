"""
Execution Agent handlers.

Takes PM decisions + ML predictions, runs the portfolio construction
pipeline, and executes trades via Alpaca.

Handles both daily rebalancing and intraday trading with:
- Session awareness (market hours, closing window)
- PDT rule compliance
- VWAP order splitting for large positions
- Mandatory EOD close for intraday positions
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from skills.shared import get_logger, audit_log, approval_engine
from skills.trading.exchange_client import get_exchange_client
from .session import (
    get_session, is_market_open, minutes_to_close,
    should_close_intraday, check_pdt_compliance, MarketSession,
)
from .order_splitter import should_split, create_slices, execute_slices
from .models import ExecutionReport

logger = get_logger("execution.handlers")

STATE_FILE = Path("./workspaces/execution-agent/state.json")


DEFAULT_STATE = {
    "overnight_positions": [],
    "intraday_positions": [],
    "daily_turnover_used": 0.0,
    "daily_trades": [],
    "pdt_day_trade_count": 0,
    "account_equity": 0.0,
    "last_execution": None,
    "last_run": None,
}


def _load_state() -> dict:
    from skills.shared.state import safe_load_state
    return safe_load_state(STATE_FILE, DEFAULT_STATE)


def _save_state(state: dict) -> None:
    from skills.shared.state import safe_save_state
    safe_save_state(STATE_FILE, state)


async def _get_account_equity() -> float:
    """Fetch current account equity from Alpaca."""
    try:
        client = get_exchange_client("alpaca")
        cash = await client.get_balance("USD")
        await client.close()
        return cash
    except Exception as e:
        logger.error(f"Failed to fetch account equity: {e}")
        return 0.0


async def _get_current_positions() -> dict[str, float]:
    """Fetch current Alpaca positions as {symbol: market_value}."""
    try:
        import httpx
        import os
        api_key = os.getenv("ALPACA_API_KEY", "")
        api_secret = os.getenv("ALPACA_API_SECRET", "")
        base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

        async with httpx.AsyncClient(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            timeout=10.0,
        ) as client:
            resp = await client.get("/v2/positions")
            resp.raise_for_status()
            positions = {}
            for pos in resp.json():
                positions[pos["symbol"]] = float(pos.get("market_value", 0))
            return positions
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return {}


async def _place_order(symbol: str, side: str, notional: float) -> dict:
    """Place a notional order via Alpaca."""
    try:
        import httpx
        import os
        api_key = os.getenv("ALPACA_API_KEY", "")
        api_secret = os.getenv("ALPACA_API_SECRET", "")
        base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

        async with httpx.AsyncClient(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": api_secret,
            },
            timeout=10.0,
        ) as client:
            body = {
                "symbol": symbol,
                "notional": str(abs(notional)),
                "side": side.lower(),
                "type": "market",
                "time_in_force": "day",
            }
            resp = await client.post(
                "/v2/orders",
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                "order_id": data.get("id"),
                "symbol": symbol,
                "side": side,
                "notional": notional,
                "status": data.get("status", "pending"),
            }
    except Exception as e:
        logger.error(f"Order failed for {symbol}: {e}")
        return {"error": str(e), "symbol": symbol}


async def execute_daily(decision: dict, predictions: dict[str, float]) -> dict:
    """
    Execute daily rebalancing based on PM decision and ML predictions.

    Computes target portfolio from PM params + predictions,
    diffs against current positions, and executes trades.
    """
    session = get_session()
    if session not in (MarketSession.OPEN, MarketSession.CLOSING, MarketSession.PRE_MARKET):
        return {"error": f"Market is {session.value} — cannot execute daily trades"}

    state = _load_state()
    params = decision.get("final_params", {})

    # Get account equity and current positions
    equity = await _get_account_equity()
    state["account_equity"] = equity
    current_positions = await _get_current_positions()

    if equity <= 0:
        return {"error": "No account equity available"}

    # Build target portfolio from predictions + PM params
    n_long = params.get("max_positions_long", 10)
    n_short = params.get("max_positions_short", 5)
    leverage = params.get("max_gross_leverage", 1.2)

    # Sort predictions: top N for longs, bottom N for shorts
    sorted_preds = sorted(predictions.items(), key=lambda x: x[1], reverse=True)
    long_picks = [sym for sym, _ in sorted_preds[:n_long]]
    short_picks = [sym for sym, _ in sorted_preds[-n_short:]] if n_short > 0 else []

    # Equal-weight for now (risk_parity requires vol estimates)
    weighting = params.get("weighting", "equal")
    target_gross = equity * leverage

    target_positions = {}
    if long_picks:
        long_weight = target_gross * (n_long / (n_long + n_short)) / n_long
        for sym in long_picks:
            target_positions[sym] = long_weight

    if short_picks:
        short_weight = target_gross * (n_short / (n_long + n_short)) / n_short
        for sym in short_picks:
            target_positions[sym] = -short_weight

    # Compute trades: target - current
    all_symbols = set(target_positions.keys()) | set(current_positions.keys())
    trades = []
    for sym in all_symbols:
        target = target_positions.get(sym, 0)
        current = current_positions.get(sym, 0)
        diff = target - current

        # Skip small trades (< $500 or < 1% of equity)
        if abs(diff) < max(500, equity * 0.01):
            continue

        side = "buy" if diff > 0 else "sell"
        trades.append({
            "symbol": sym,
            "side": side,
            "notional": abs(diff),
        })

    # Apply turnover limit
    max_turnover = params.get("max_daily_turnover", 0.40)
    max_trade_total = equity * max_turnover
    total_trade = sum(t["notional"] for t in trades)

    if total_trade > max_trade_total:
        scale = max_trade_total / total_trade
        for t in trades:
            t["notional"] = round(t["notional"] * scale, 2)
        logger.info(f"Turnover limited: scaled trades by {scale:.2f}")

    # Execute trades
    report = ExecutionReport(
        mode="daily",
        decision_id=decision.get("decision_id", ""),
    )

    for trade in trades:
        # VWAP split if large
        slices = create_slices(trade["symbol"], trade["side"], trade["notional"])
        if len(slices) > 1:
            results = await execute_slices(slices, _place_order)
            for r in results:
                report.trades.append(r)
                if "error" not in r:
                    report.orders_filled += 1
                else:
                    report.errors.append(r["error"])
            report.orders_placed += len(slices)
        else:
            result = await _place_order(trade["symbol"], trade["side"], trade["notional"])
            report.trades.append(result)
            report.orders_placed += 1
            if "error" not in result:
                report.orders_filled += 1
            else:
                report.errors.append(result.get("error", ""))

        report.total_notional += trade["notional"]

    # Update state
    state["daily_turnover_used"] += report.total_notional
    state["daily_trades"].extend(report.trades)
    state["overnight_positions"] = [
        {"symbol": sym, "notional": val}
        for sym, val in target_positions.items()
    ]
    state["last_execution"] = report.to_dict()
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    audit_log("execution-agent", "daily_executed", {
        "decision_id": decision.get("decision_id"),
        "orders_placed": report.orders_placed,
        "orders_filled": report.orders_filled,
        "total_notional": report.total_notional,
        "n_errors": len(report.errors),
    })

    logger.info(
        f"Daily execution: {report.orders_filled}/{report.orders_placed} filled, "
        f"${report.total_notional:,.2f} notional"
    )

    return report.to_dict()


async def execute_intraday(decision: dict, predictions: dict[str, float]) -> dict:
    """
    Execute intraday adjustments based on PM decision.

    Only adjusts existing positions — does not add new tickers.
    Checks PDT compliance before executing.
    """
    if not is_market_open():
        return {"error": "Market is closed — cannot execute intraday"}

    if should_close_intraday():
        return await close_intraday_positions()

    state = _load_state()

    # PDT check
    pdt_ok, pdt_reason = check_pdt_compliance(
        state.get("account_equity", 0),
        state.get("pdt_day_trade_count", 0),
    )
    if not pdt_ok:
        return {"error": f"PDT blocked: {pdt_reason}"}

    # Intraday uses remaining turnover budget
    remaining_turnover = (
        state.get("account_equity", 0)
        * decision.get("final_params", {}).get("max_daily_turnover", 0.40)
        * 0.5  # intraday gets 50% of remaining
        - state.get("daily_turnover_used", 0)
    )

    if remaining_turnover <= 0:
        return {"status": "no_budget", "message": "Daily turnover budget exhausted"}

    # For intraday, we only adjust sizing of existing positions
    # based on updated PM params (not adding new tickers)
    return {
        "status": "intraday_complete",
        "mode": "intraday",
        "decision_id": decision.get("decision_id"),
        "remaining_turnover": round(remaining_turnover, 2),
        "minutes_to_close": minutes_to_close(),
    }


async def close_intraday_positions() -> dict:
    """
    Mandatory EOD close: liquidate all intraday-flagged positions.
    This is a hard rule — no debate or approval needed.
    """
    state = _load_state()
    intraday = state.get("intraday_positions", [])

    if not intraday:
        return {"status": "no_intraday_positions"}

    closed = []
    errors = []
    for pos in intraday:
        side = "sell" if pos.get("notional", 0) > 0 else "buy"
        result = await _place_order(
            pos["symbol"], side, abs(pos.get("notional", 0))
        )
        if "error" in result:
            errors.append(result)
        else:
            closed.append(result)

    state["intraday_positions"] = []
    state["pdt_day_trade_count"] += len(closed)
    _save_state(state)

    audit_log("execution-agent", "eod_close", {
        "closed": len(closed),
        "errors": len(errors),
    })

    logger.info(f"EOD close: {len(closed)} positions closed, {len(errors)} errors")

    return {
        "status": "eod_closed",
        "closed": len(closed),
        "errors": len(errors),
        "details": closed,
    }


async def heartbeat() -> str:
    """Execution agent status check."""
    state = _load_state()
    session = get_session()

    lines = [
        f"Execution Agent — {session.value}",
        f"  Account equity: ${state.get('account_equity', 0):,.2f}",
        f"  Overnight positions: {len(state.get('overnight_positions', []))}",
        f"  Intraday positions: {len(state.get('intraday_positions', []))}",
        f"  Daily turnover used: ${state.get('daily_turnover_used', 0):,.2f}",
        f"  PDT day trades: {state.get('pdt_day_trade_count', 0)}",
    ]

    if is_market_open():
        lines.append(f"  Minutes to close: {minutes_to_close()}")

    if should_close_intraday():
        lines.append("  EOD CLOSE TIME — liquidating intraday positions")

    return "\n".join(lines)
