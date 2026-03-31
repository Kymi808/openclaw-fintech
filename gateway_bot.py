"""
Lightweight OpenClaw Gateway — Telegram Bot
Mimics the OpenClaw multi-agent routing pattern:
  Telegram message → Router → Specialist Agent → Response

Run: pip install python-telegram-bot && python gateway_bot.py
"""
import asyncio
import json
import re
import os
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv("gateway/.env")

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("openclaw.gateway")

# ─── Agent Registry ──────────────────────────────────────────────────────────

AGENTS = {
    "trading-agent": {
        "description": "Stock/crypto trading, prices, arbitrage, and market analysis",
        "patterns": [
            r"\b(buy|sell|trade)\b", r"\bprice\b", r"\bmarket\s*update\b",
            r"\barbitrage\b", r"\bpositions?\b", r"\bticker\b",
            r"\b(aapl|tsla|msft|nvda|spy|btc|eth|sol)\b",
        ],
    },
    "portfolio-agent": {
        "description": "Portfolio tracking, allocation, rebalancing, and performance",
        "patterns": [
            r"\bportfolio\b", r"\brebalanc", r"\ballocation\b",
            r"\bperformance\b", r"\bholdings?\b", r"\bdrift\b",
        ],
    },
    "defi-agent": {
        "description": "DeFi wallet management, swaps, governance, gas, liquidity",
        "patterns": [
            r"\bswap\b", r"\bwallet\b", r"\bdefi\b", r"\bgovernance\b",
            r"\bliquidity\b", r"\bgas\b", r"\byield\b", r"\bstaking\b",
            r"\buniswap\b", r"\baave\b",
        ],
    },
    "finance-agent": {
        "description": "Expenses, receipts, tax, budget, invoices, bookkeeping",
        "patterns": [
            r"\bexpense\b", r"\breceipt\b", r"\btax\b", r"\bbudget\b",
            r"\binvoice\b", r"\bbookkeeping\b", r"\bbank\b",
        ],
    },
    "legal-agent": {
        "description": "Contract analysis, SEC filings, GDPR compliance, legal research",
        "patterns": [
            r"\bcontract\b", r"\bsec\s*(filing)?\b", r"\bcompliance\b",
            r"\bgdpr\b", r"\blegal\b", r"\bregulat",
        ],
    },
}


def route_message(text: str) -> str | None:
    """Router agent — classify intent and pick the best agent."""
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for agent, info in AGENTS.items():
        score = 0
        for pat in info["patterns"]:
            if re.search(pat, text_lower):
                score += 1
        if score > 0:
            scores[agent] = score

    if not scores:
        return None

    return max(scores, key=scores.get)


# ─── Agent Handlers ──────────────────────────────────────────────────────────

async def handle_trading(text: str) -> str:
    """Trading agent — dispatch to the right skill handler."""
    from skills.trading.handlers import (
        get_prices, execute_trade, check_arbitrage, get_positions, market_update,
    )
    from skills.shared.config import ALLOWED_STOCK_PAIRS

    text_lower = text.lower().strip()

    # Price queries
    if re.search(r"\bprices?\b", text_lower) or re.search(
        r"\b(aapl|tsla|msft|nvda|spy|btc|eth|sol)\b", text_lower
    ):
        # Check if asking about a specific ticker
        match = re.search(r"\b(aapl|tsla|msft|nvda|spy)\b", text_lower)
        if match:
            symbol = match.group(1).upper()
            results = await get_prices([f"{symbol}/USD"])
        elif re.search(r"\b(btc|eth|sol)\b", text_lower):
            match = re.search(r"\b(btc|eth|sol)\b", text_lower)
            symbol = match.group(1).upper()
            results = await get_prices([f"{symbol}/USDT"])
        else:
            results = await get_prices()

        if not results:
            return "Could not fetch prices. Markets may be closed."

        lines = ["📊 **Current Prices**\n"]
        for r in results:
            arrow = "🟢" if r["change_24h_pct"] >= 0 else "🔴"
            sign = "+" if r["change_24h_pct"] >= 0 else ""
            lines.append(
                f"{arrow} **{r['pair']}**: ${r['price']:,.2f} "
                f"({sign}{r['change_24h_pct']:.2f}%) — _{r['exchange']}_"
            )
        return "\n".join(lines)

    # Positions
    if re.search(r"\bpositions?\b", text_lower):
        pos = await get_positions()
        lines = [
            "📋 **Open Positions**\n",
            f"Daily volume used: ${pos['daily_volume_used']:,.2f} / ${pos['daily_limit']:,.2f}",
            f"Remaining: ${pos['remaining']:,.2f}\n",
        ]
        if pos["open_positions"]:
            for p in pos["open_positions"]:
                lines.append(
                    f"• {p['pair']} — entry ${p['entry_price']:,.2f}, "
                    f"qty {p['amount']:.4f} on {p['exchange']}"
                )
        else:
            lines.append("No open positions.")
        return "\n".join(lines)

    # Arbitrage
    if re.search(r"\barbitrage\b", text_lower):
        opps = await check_arbitrage()
        if not opps:
            return "No arbitrage opportunities found right now."
        lines = ["💰 **Arbitrage Opportunities**\n"]
        for o in opps:
            lines.append(
                f"• {o['pair']}: Buy on {o['buy_on']} (${o['buy_price']:,.2f}), "
                f"sell on {o['sell_on']} (${o['sell_price']:,.2f}) — "
                f"net profit: ${o['net_profit_usd']:.2f}"
            )
        return "\n".join(lines)

    # Market update
    if re.search(r"\bmarket\s*update\b", text_lower):
        return await market_update()

    # Buy/Sell
    buy_match = re.search(
        r"\b(buy|sell)\s+\$?(\d+(?:\.\d+)?)\s+(?:of\s+)?(\w+)", text_lower
    )
    if buy_match:
        side = buy_match.group(1).upper()
        amount = float(buy_match.group(2))
        symbol = buy_match.group(3).upper()

        # Determine pair and exchange
        if symbol in ("AAPL", "TSLA", "MSFT", "NVDA", "SPY"):
            pair = f"{symbol}/USD"
            exchange = "alpaca"
        elif symbol in ("BTC", "ETH", "SOL"):
            pair = f"{symbol}/USDT"
            exchange = "binance"
        else:
            return f"Unknown symbol: {symbol}"

        result = await execute_trade(pair, side, amount, exchange)

        if "error" in result:
            return f"⚠️ {result['error']}"
        if result.get("status") == "awaiting_approval":
            return result["message"]

        return (
            f"✅ **Trade Executed**\n\n"
            f"Order ID: `{result['order_id']}`\n"
            f"{result['side']} {result['pair']}\n"
            f"Amount: {result['amount']:.4f}\n"
            f"Price: ${result.get('price', 0):,.2f}\n"
            f"Fee: ${result.get('fee', 0):.2f}"
        )

    # Fallback: show prices
    results = await get_prices()
    if results:
        lines = ["📊 **Market Overview**\n"]
        for r in results:
            arrow = "🟢" if r["change_24h_pct"] >= 0 else "🔴"
            sign = "+" if r["change_24h_pct"] >= 0 else ""
            lines.append(
                f"{arrow} **{r['pair']}**: ${r['price']:,.2f} "
                f"({sign}{r['change_24h_pct']:.2f}%)"
            )
        return "\n".join(lines)

    return "Trading agent ready. Try: `price AAPL`, `buy $50 TSLA`, `positions`, `arbitrage`"


async def handle_portfolio(text: str) -> str:
    """Portfolio agent — dispatch to the right skill handler."""
    from skills.portfolio.handlers import (
        get_portfolio, propose_rebalance, performance_report,
    )

    text_lower = text.lower()

    if re.search(r"\brebalanc", text_lower):
        result = await propose_rebalance()
        if "error" in result:
            return f"⚠️ {result['error']}"
        return result.get("message", "No rebalance needed.")

    if re.search(r"\bperformance\b", text_lower):
        return await performance_report()

    # Default: show portfolio
    portfolio = await get_portfolio()
    lines = [
        f"💼 **Portfolio Overview**\n",
        f"Total Value: **${portfolio['total_value']:,.2f}**\n",
        "| Asset | Value | Actual | Target | Drift |",
        "|-------|-------|--------|--------|-------|",
    ]
    for h in portfolio["holdings"]:
        drift_sign = "+" if h["drift_pct"] >= 0 else ""
        lines.append(
            f"| {h['asset']} | ${h['value_usd']:,.2f} | "
            f"{h['allocation_pct']:.1f}% | {h['target_pct']:.1f}% | "
            f"{drift_sign}{h['drift_pct']:.1f}% |"
        )

    rebal = "⚠️ YES" if portfolio["needs_rebalance"] else "✅ NO"
    lines.append(f"\nRebalance needed: {rebal}")
    return "\n".join(lines)


async def handle_defi(text: str) -> str:
    """DeFi agent — governance is free, others need config."""
    text_lower = text.lower()

    if re.search(r"\bgovernance\b", text_lower):
        try:
            from skills.defi.handlers import check_governance
            result = await check_governance()
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"DeFi governance check failed: {e}"

    if re.search(r"\bgas\b", text_lower):
        try:
            from skills.defi.handlers import get_gas_prices
            result = await get_gas_prices()
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Gas price check failed: {e}"

    return (
        "🔗 **DeFi Agent**\n\n"
        "Available commands:\n"
        "• `governance` — check active proposals\n"
        "• `gas` — current gas prices\n"
        "• `swap`, `wallet`, `liquidity` — require Alchemy API key"
    )


async def handle_finance(text: str) -> str:
    """Finance agent — budget and expense tracking."""
    text_lower = text.lower()

    if re.search(r"\bbudget\b", text_lower):
        try:
            from skills.finance.handlers import budget_status
            result = await budget_status()
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Budget check failed: {e}"

    if re.search(r"\bexpense\b", text_lower):
        try:
            from skills.finance.handlers import get_expenses
            result = await get_expenses()
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Expense query failed: {e}"

    return (
        "💰 **Finance Agent**\n\n"
        "Available commands:\n"
        "• `budget` — check budget status\n"
        "• `expense` — view expenses\n"
        "• Send a receipt photo to process it"
    )


async def handle_legal(text: str) -> str:
    """Legal agent — SEC filings and legal research."""
    text_lower = text.lower()

    if re.search(r"\bsec\b", text_lower):
        try:
            from skills.legal.handlers import check_sec_filings

            # Extract company if mentioned
            match = re.search(r"sec\s+(?:filing\w*\s+)?(?:for\s+)?(\w+)", text_lower)
            company = match.group(1) if match else "AAPL"
            result = await check_sec_filings(company)
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"SEC filing check failed: {e}"

    if re.search(r"\blegal\s*research\b", text_lower):
        try:
            from skills.legal.handlers import legal_research
            match = re.search(r"legal\s*research\s+(.+)", text_lower)
            query = match.group(1) if match else text
            result = await legal_research(query)
            if isinstance(result, str):
                return result
            return json.dumps(result, indent=2)
        except Exception as e:
            return f"Legal research failed: {e}"

    if re.search(r"\bgdpr\b", text_lower):
        try:
            from skills.legal.handlers import gdpr_scan
            match = re.search(r"gdpr\s+(?:scan\s+)?(\S+)", text_lower)
            if match:
                result = await gdpr_scan(match.group(1))
                if isinstance(result, str):
                    return result
                return json.dumps(result, indent=2)
            return "Usage: `gdpr scan https://example.com`"
        except Exception as e:
            return f"GDPR scan failed: {e}"

    return (
        "⚖️ **Legal Agent**\n\n"
        "Available commands:\n"
        "• `sec filing AAPL` — check SEC filings\n"
        "• `legal research <topic>` — search case law\n"
        "• `gdpr scan <url>` — GDPR compliance check\n"
        "• Send a PDF to analyze a contract"
    )


AGENT_HANDLERS = {
    "trading-agent": handle_trading,
    "portfolio-agent": handle_portfolio,
    "defi-agent": handle_defi,
    "finance-agent": handle_finance,
    "legal-agent": handle_legal,
}


# ─── Telegram Handlers ───────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **OpenClaw Fintech Bot**\n\n"
        "I'm a multi-agent fintech team. Just type naturally:\n\n"
        "📊 **Trading** — `price AAPL`, `buy $50 TSLA`, `arbitrage`\n"
        "💼 **Portfolio** — `portfolio`, `holdings`, `rebalance`\n"
        "🔗 **DeFi** — `governance`, `gas prices`\n"
        "💰 **Finance** — `budget`, `expenses`\n"
        "⚖️ **Legal** — `sec filing AAPL`, `gdpr scan url`\n\n"
        "All trades use Alpaca paper trading (no real money).",
        parse_mode="Markdown",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    agents_list = "\n".join(
        f"• **{name}**: {info['description']}" for name, info in AGENTS.items()
    )
    await update.message.reply_text(
        f"**Agent Team:**\n{agents_list}\n\n"
        f"The router automatically sends your message to the right agent.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler — routes through the agent system."""
    text = update.message.text
    if not text:
        return

    # Step 1: Router agent classifies intent
    agent = route_message(text)

    if not agent:
        await update.message.reply_text(
            "🤔 I'm not sure which agent should handle that. Try:\n"
            "`price AAPL`, `portfolio`, `governance`, `budget`, `sec filing AAPL`",
            parse_mode="Markdown",
        )
        return

    # Step 2: Show routing (like OpenClaw gateway does)
    agent_label = agent.replace("-", " ").title()
    routing_msg = await update.message.reply_text(
        f"→ _Routing to {agent_label}..._", parse_mode="Markdown"
    )

    # Step 3: Dispatch to agent handler
    try:
        handler = AGENT_HANDLERS[agent]
        response = await handler(text)
    except Exception as e:
        logger.error(f"Agent {agent} failed: {e}", exc_info=True)
        response = f"⚠️ {agent_label} encountered an error: {e}"

    # Step 4: Send response
    # Split long messages (Telegram limit is 4096 chars)
    if len(response) > 4000:
        chunks = [response[i : i + 4000] for i in range(0, len(response), 4000)]
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    else:
        await update.message.reply_text(response, parse_mode="Markdown")


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in gateway/.env")
        return

    print("=" * 60)
    print("  OpenClaw Fintech Gateway — Telegram Bot")
    print("=" * 60)
    print(f"  Agents: {', '.join(AGENTS.keys())}")
    print(f"  Routing: pattern-based (mimics OpenClaw router agent)")
    print(f"  Trading: Alpaca paper trading (no real money)")
    print(f"  LLM: Anthropic Claude API")
    print("=" * 60)
    print("  Bot is running. Send a message on Telegram.\n")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
