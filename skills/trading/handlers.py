"""
OpenClaw skill handlers for the Trading Agent.
These functions are invoked by the agent runtime when tools are called.
"""
import json
from pathlib import Path

from skills.shared import (
    get_logger, audit_log, approval_engine, ALLOWED_PAIRS, ALLOWED_EXCHANGES,
    DEFAULT_LIMITS,
)
from skills.shared.config import ALLOWED_STOCK_PAIRS
from .exchange_client import get_exchange_client, Ticker
from .strategy import (
    check_risk_limits, needs_approval, detect_arbitrage,
    simple_momentum_signal, format_market_update, format_trade_signal,
)

logger = get_logger("trading.handlers")

# Persistent state file
STATE_FILE = Path("./workspaces/trading-agent/state.json")


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "daily_volume": 0.0,
        "open_positions": [],
        "trade_history": [],
    }


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _is_stock_pair(pair: str) -> bool:
    """Check if a pair is a stock (USD denominated, not crypto)."""
    return pair.endswith("/USD") and pair.split("/")[0] in ("AAPL", "TSLA", "MSFT", "NVDA", "SPY")


def _pairs_for_exchange(exchange_name: str, pairs: list[str] = None) -> list[str]:
    """Return appropriate pairs for an exchange (stocks for Alpaca, crypto otherwise).
    Filters out stock pairs from crypto exchanges and crypto pairs from Alpaca."""
    if pairs:
        if exchange_name == "alpaca":
            return [p for p in pairs if _is_stock_pair(p)]
        else:
            return [p for p in pairs if not _is_stock_pair(p)]
    if exchange_name == "alpaca":
        return ALLOWED_STOCK_PAIRS
    return ALLOWED_PAIRS


async def get_prices(pairs: list[str] = None) -> list[dict]:
    """Fetch current prices from all configured exchanges."""
    all_tickers = []

    for exchange_name in ALLOWED_EXCHANGES:
        try:
            ex_pairs = _pairs_for_exchange(exchange_name, pairs)
            if not ex_pairs:
                continue  # skip exchanges with no matching pairs
            client = get_exchange_client(exchange_name)
            tickers = await client.get_all_tickers(ex_pairs)
            all_tickers.extend(tickers)
            await client.close()
        except Exception as e:
            logger.error(f"Failed to fetch from {exchange_name}: {e}")

    return [
        {
            "pair": t.pair,
            "price": t.price,
            "change_24h_pct": t.change_24h_pct,
            "exchange": t.exchange,
        }
        for t in all_tickers
    ]


async def execute_trade(
    pair: str,
    side: str,
    amount_usd: float,
    exchange: str = "binance",
) -> dict:
    """Execute a trade with full risk checks and approval workflow."""

    # Validate pair
    all_allowed = ALLOWED_PAIRS + ALLOWED_STOCK_PAIRS
    if pair not in all_allowed:
        return {"error": f"Pair {pair} not in allowed list: {all_allowed}"}

    # Validate exchange
    if exchange not in ALLOWED_EXCHANGES:
        return {"error": f"Exchange {exchange} not allowed: {ALLOWED_EXCHANGES}"}

    # Load state for limit checks
    state = _load_state()

    # Risk check
    ok, reason = check_risk_limits(
        amount_usd=amount_usd,
        daily_volume_used=state["daily_volume"],
        open_positions=len(state["open_positions"]),
    )
    if not ok:
        audit_log("trading-agent", "trade_rejected", {
            "pair": pair, "side": side, "amount": amount_usd, "reason": reason,
        })
        return {"error": f"Risk limit violated: {reason}"}

    # Approval check
    if needs_approval(amount_usd):
        req_id = approval_engine.create_request(
            agent="trading-agent",
            action="execute_trade",
            description=f"{side} {pair} for ${amount_usd:.2f} on {exchange}",
            amount=amount_usd,
            details={"pair": pair, "side": side, "exchange": exchange},
        )
        return {
            "status": "awaiting_approval",
            "request_id": req_id,
            "message": approval_engine.format_request_message(req_id),
        }

    # Execute
    try:
        client = get_exchange_client(exchange)
        # Convert USD amount to asset quantity
        ticker = await client.get_ticker(pair)
        qty = amount_usd / ticker.price

        result = await client.place_order(pair, side, qty)
        await client.close()

        # Update state
        state["daily_volume"] += amount_usd
        if side.upper() == "BUY":
            state["open_positions"].append({
                "pair": pair,
                "entry_price": result.price,
                "amount": result.amount,
                "exchange": exchange,
                "stop_loss": result.price * (1 - DEFAULT_LIMITS["stop_loss_pct"] / 100),
            })
        _save_state(state)

        return {
            "status": "executed",
            "order_id": result.order_id,
            "pair": pair,
            "side": side,
            "amount": result.amount,
            "price": result.price,
            "total": result.total,
            "fee": result.fee,
        }

    except Exception as e:
        logger.error(f"Trade execution failed: {e}")
        audit_log("trading-agent", "trade_error", {
            "pair": pair, "side": side, "amount": amount_usd, "error": str(e),
        })
        return {"error": f"Execution failed: {e}"}


async def check_arbitrage() -> list[dict]:
    """Scan all exchanges for arbitrage opportunities."""
    tickers_by_exchange: dict[str, list[Ticker]] = {}

    for exchange_name in ALLOWED_EXCHANGES:
        try:
            client = get_exchange_client(exchange_name)
            ex_pairs = _pairs_for_exchange(exchange_name)
            tickers = await client.get_all_tickers(ex_pairs)
            tickers_by_exchange[exchange_name] = tickers
            await client.close()
        except Exception as e:
            logger.error(f"Failed to fetch from {exchange_name}: {e}")

    opps = detect_arbitrage(tickers_by_exchange)

    results = []
    for opp in opps:
        results.append({
            "pair": opp.pair,
            "buy_on": opp.buy_exchange,
            "buy_price": opp.buy_price,
            "sell_on": opp.sell_exchange,
            "sell_price": opp.sell_price,
            "spread_pct": round(opp.spread_pct, 3),
            "net_profit_usd": round(opp.net_profit_usd, 2),
        })
        audit_log("trading-agent", "arbitrage_detected", {
            "pair": opp.pair,
            "spread_pct": opp.spread_pct,
            "net_profit": opp.net_profit_usd,
        })

    return results


async def get_positions() -> dict:
    """Return current open positions and daily stats."""
    state = _load_state()
    return {
        "open_positions": state["open_positions"],
        "daily_volume_used": state["daily_volume"],
        "daily_limit": DEFAULT_LIMITS["max_daily_volume"],
        "remaining": DEFAULT_LIMITS["max_daily_volume"] - state["daily_volume"],
    }


async def market_update() -> str:
    """Generate a formatted market update message."""
    tickers = []
    for exchange_name in ALLOWED_EXCHANGES[:1]:  # Primary exchange only
        try:
            client = get_exchange_client(exchange_name)
            tickers = await client.get_all_tickers(ALLOWED_PAIRS)
            await client.close()
        except Exception as e:
            logger.error(f"Market update failed: {e}")

    state = _load_state()
    return format_market_update(
        tickers=tickers,
        open_positions=len(state["open_positions"]),
    )


async def heartbeat() -> str:
    """
    Cron-triggered heartbeat: check markets, evaluate signals, act.
    This is the main loop that runs every 5 minutes.
    """
    logger.info("Trading agent heartbeat starting")

    # 1. Fetch prices
    tickers = []
    tickers_by_exchange: dict[str, list[Ticker]] = {}
    for exchange_name in ALLOWED_EXCHANGES:
        try:
            client = get_exchange_client(exchange_name)
            ex_pairs = _pairs_for_exchange(exchange_name)
            ex_tickers = await client.get_all_tickers(ex_pairs)
            tickers.extend(ex_tickers)
            tickers_by_exchange[exchange_name] = ex_tickers
            await client.close()
        except Exception as e:
            logger.error(f"Heartbeat fetch failed for {exchange_name}: {e}")

    if not tickers:
        return "Heartbeat: no price data available"

    state = _load_state()
    messages = []

    # 2. Check stop-losses on open positions
    for pos in state["open_positions"]:
        for t in tickers:
            if t.pair == pos["pair"] and t.exchange == pos["exchange"]:
                if t.price <= pos.get("stop_loss", 0):
                    messages.append(
                        f"🛑 Stop-loss triggered: {pos['pair']} at ${t.price:.2f} "
                        f"(stop: ${pos['stop_loss']:.2f}). Selling."
                    )
                    # Would execute sell here
                    audit_log("trading-agent", "stop_loss_triggered", {
                        "pair": pos["pair"], "price": t.price,
                        "stop_loss": pos["stop_loss"],
                    })

    # 3. Check for arbitrage
    arb_opps = detect_arbitrage(tickers_by_exchange)
    for opp in arb_opps:
        messages.append(
            f"💰 Arbitrage: {opp.pair} — buy on {opp.buy_exchange} "
            f"(${opp.buy_price:,.2f}), sell on {opp.sell_exchange} "
            f"(${opp.sell_price:,.2f}) — net profit: ${opp.net_profit_usd:.2f}"
        )

    # 4. Check momentum signals
    signals = simple_momentum_signal(tickers)
    for sig in signals:
        messages.append(format_trade_signal(sig))

    # 5. Alert on big moves
    for t in tickers:
        if abs(t.change_24h_pct) >= 5.0:
            arrow = "🟢" if t.change_24h_pct > 0 else "🔴"
            messages.append(
                f"{arrow} Big move: {t.pair} {t.change_24h_pct:+.1f}% "
                f"(${t.price:,.2f})"
            )

    audit_log("trading-agent", "heartbeat", {
        "pairs_checked": len(tickers),
        "signals": len(signals),
        "arbitrage_opps": len(arb_opps),
        "alerts": len(messages),
    })

    if messages:
        return "\n\n".join(messages)
    return "Heartbeat: markets stable, no signals."
