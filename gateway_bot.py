#!/usr/bin/env python3
"""Optional Telegram gateway for the shippable OpenClaw Quant surface.

The production surface is the scheduler plus CLI. This bot is a thin chat adapter
over the same handlers, so it must not reference packages that are not shipped in
this repository.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable

from dotenv import load_dotenv

try:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
except ModuleNotFoundError:  # pragma: no cover - exercised only without optional extra
    Update = object  # type: ignore[assignment]
    Application = None  # type: ignore[assignment]
    CommandHandler = None  # type: ignore[assignment]
    ContextTypes = object  # type: ignore[assignment]
    MessageHandler = None  # type: ignore[assignment]
    filters = None  # type: ignore[assignment]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("openclaw.gateway")

Handler = Callable[[str], Awaitable[str]]


async def handle_trading(text: str) -> str:
    """Trading adapter backed by existing trading handlers."""
    from skills.trading.handlers import check_arbitrage, execute_trade, get_positions, get_prices

    text_lower = text.lower().strip()

    if re.search(r"\bpositions?\b", text_lower):
        positions = await get_positions()
        lines = [
            "Open Positions",
            (
                f"Daily volume: ${positions['daily_volume_used']:,.2f} / "
                f"${positions['daily_limit']:,.2f}"
            ),
            f"Remaining: ${positions['remaining']:,.2f}",
            "",
        ]
        if positions["open_positions"]:
            for position in positions["open_positions"]:
                lines.append(
                    f"- {position['pair']}: entry ${position['entry_price']:,.2f}, "
                    f"qty {position['amount']:.4f} on {position['exchange']}"
                )
        else:
            lines.append("No open positions.")
        return "\n".join(lines)

    if re.search(r"\barbitrage\b", text_lower):
        opportunities = await check_arbitrage()
        if not opportunities:
            return "No arbitrage opportunities found right now."
        return "\n".join(
            [
                "Arbitrage Opportunities",
                *[
                    (
                        f"- {opp['pair']}: buy on {opp['buy_on']} "
                        f"(${opp['buy_price']:,.2f}), sell on {opp['sell_on']} "
                        f"(${opp['sell_price']:,.2f}); net ${opp['net_profit_usd']:.2f}"
                    )
                    for opp in opportunities
                ],
            ]
        )

    trade_match = re.search(
        r"\b(buy|sell)\s+\$?(\d+(?:\.\d+)?)\s+(?:of\s+)?([a-z]{2,5})\b",
        text_lower,
    )
    if trade_match:
        side = trade_match.group(1).upper()
        amount = float(trade_match.group(2))
        symbol = trade_match.group(3).upper()
        if symbol in {"AAPL", "TSLA", "MSFT", "NVDA", "SPY"}:
            pair = f"{symbol}/USD"
            exchange = "alpaca"
        elif symbol in {"BTC", "ETH", "SOL"}:
            pair = f"{symbol}/USDT"
            exchange = "binance"
        else:
            return f"Unknown symbol: {symbol}"

        result = await execute_trade(pair, side, amount, exchange)
        if "error" in result:
            return f"Trade rejected: {result['error']}"
        if result.get("status") == "awaiting_approval":
            return result["message"]
        return (
            "Trade Executed\n"
            f"Order ID: {result['order_id']}\n"
            f"{result['side']} {result['pair']}\n"
            f"Amount: {result['amount']:.4f}\n"
            f"Price: ${result.get('price', 0):,.2f}\n"
            f"Fee: ${result.get('fee', 0):.2f}"
        )

    symbol_match = re.search(r"\b(aapl|tsla|msft|nvda|spy|btc|eth|sol)\b", text_lower)
    if symbol_match:
        symbol = symbol_match.group(1).upper()
        pair = f"{symbol}/USDT" if symbol in {"BTC", "ETH", "SOL"} else f"{symbol}/USD"
        prices = await get_prices([pair])
    else:
        prices = await get_prices()

    if not prices:
        return "Could not fetch prices. Markets or data providers may be unavailable."

    lines = ["Current Prices"]
    for price in prices:
        sign = "+" if price["change_24h_pct"] >= 0 else ""
        lines.append(
            f"- {price['pair']}: ${price['price']:,.2f} "
            f"({sign}{price['change_24h_pct']:.2f}%) via {price['exchange']}"
        )
    return "\n".join(lines)


async def handle_portfolio(text: str) -> str:
    """Portfolio and approval adapter backed by CLI command handlers."""
    from cli import cmd_approve, cmd_deny, cmd_pending, cmd_pnl, cmd_portfolio, cmd_session

    text_lower = text.lower().strip()
    approve_match = re.match(r"approve\s+(APR-\d+)", text, re.IGNORECASE)
    deny_match = re.match(r"deny\s+(APR-\d+)", text, re.IGNORECASE)

    if approve_match:
        return await cmd_approve(approve_match.group(1).upper())
    if deny_match:
        return await cmd_deny(deny_match.group(1).upper())
    if "pending" in text_lower:
        return cmd_pending()
    if "pnl" in text_lower or "p&l" in text_lower:
        return await cmd_pnl()
    if "session" in text_lower:
        return await cmd_session()
    return await cmd_portfolio()


async def handle_legal(text: str) -> str:
    """Legal and compliance adapter backed by existing legal handlers."""
    from skills.legal.handlers import check_sec_filings, gdpr_scan, legal_research

    text_lower = text.lower().strip()

    if "gdpr" in text_lower:
        url_match = re.search(r"gdpr\s+(?:scan\s+)?(\S+)", text_lower)
        if not url_match:
            return "Usage: gdpr scan https://example.com"
        result = await gdpr_scan(url_match.group(1))
        return result if isinstance(result, str) else json.dumps(result, indent=2)

    if "legal research" in text_lower:
        query_match = re.search(r"legal\s*research\s+(.+)", text, re.IGNORECASE)
        result = await legal_research(query_match.group(1) if query_match else text)
        return result if isinstance(result, str) else json.dumps(result, indent=2)

    company_match = re.search(r"sec\s+(?:filing\w*\s+)?(?:for\s+)?([a-z.]+)", text_lower)
    result = await check_sec_filings(company_match.group(1).upper() if company_match else "AAPL")
    return result if isinstance(result, str) else json.dumps(result, indent=2)


async def handle_ops(text: str) -> str:
    """Operational commands backed by CLI command handlers."""
    from cli import cmd_briefing, cmd_health, cmd_news, cmd_pm_status, cmd_secrets

    text_lower = text.lower().strip()
    if "health" in text_lower:
        return await cmd_health()
    if "secrets" in text_lower:
        return cmd_secrets()
    if "news" in text_lower:
        return await cmd_news()
    if "pm" in text_lower:
        return await cmd_pm_status()
    return await cmd_briefing()


ROUTES: tuple[tuple[str, tuple[str, ...], Handler], ...] = (
    (
        "trading-agent",
        (
            r"\b(buy|sell|trade)\b",
            r"\bprices?\b",
            r"\barbitrage\b",
            r"\bpositions?\b",
            r"\b(aapl|tsla|msft|nvda|spy|btc|eth|sol)\b",
        ),
        handle_trading,
    ),
    (
        "portfolio-agent",
        (
            r"\bportfolio\b",
            r"\bpnl\b",
            r"\bp&l\b",
            r"\bapprove\s+apr-\d+",
            r"\bdeny\s+apr-\d+",
            r"\bpending\b",
            r"\bsession\b",
        ),
        handle_portfolio,
    ),
    (
        "legal-agent",
        (r"\bsec\b", r"\bgdpr\b", r"\blegal\b", r"\bcontract\b", r"\bcompliance\b"),
        handle_legal,
    ),
    (
        "ops-agent",
        (r"\bbriefing\b", r"\bhealth\b", r"\bsecrets\b", r"\bnews\b", r"\bpm status\b"),
        handle_ops,
    ),
)


def route_message(text: str) -> tuple[str, Handler] | None:
    """Classify intent and return the matching handler."""
    text_lower = text.lower()
    best: tuple[int, str, Handler] | None = None
    for agent_name, patterns, handler in ROUTES:
        score = sum(1 for pattern in patterns if re.search(pattern, text_lower))
        if score and (best is None or score > best[0]):
            best = (score, agent_name, handler)
    if best is None:
        return None
    return best[1], best[2]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a concise command summary."""
    del context
    await update.message.reply_text(
        "OpenClaw Quant gateway\n\n"
        "Examples:\n"
        "- price AAPL\n"
        "- portfolio\n"
        "- pending\n"
        "- approve APR-000001\n"
        "- sec filing AAPL\n"
        "- health"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a Telegram message to the supported product surface."""
    del context
    text = update.message.text or ""
    route = route_message(text)
    if route is None:
        await update.message.reply_text(
            "I can handle prices, positions, portfolio, approvals, SEC/legal, news, and health."
        )
        return

    agent_name, handler = route
    await update.message.reply_text(f"Routing to {agent_name}...")
    try:
        response = await handler(text)
    except Exception as exc:  # pragma: no cover - defensive chat boundary
        logger.exception("Agent handler failed")
        response = f"{agent_name} failed: {exc}"

    for start in range(0, len(response), 4000):
        await update.message.reply_text(response[start : start + 4000])


def main() -> int:
    """Run the Telegram bot."""
    load_dotenv("gateway/.env", override=True)

    if Application is None:
        print("python-telegram-bot is not installed. Run: python -m pip install -r requirements.txt")
        return 1

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token == "123456:ABC-xxxxx":
        print("TELEGRAM_BOT_TOKEN is not configured in gateway/.env")
        return 1

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("OpenClaw Telegram gateway running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
