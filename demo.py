#!/usr/bin/env python3
"""
OpenClaw Fintech — Interactive CLI Demo
Run this for a screenshare demo. No Telegram required.

Usage:
    PYTHONPATH=. python demo.py
"""
import asyncio
import json
import logging
import os
import re
import sys
from dotenv import load_dotenv

load_dotenv("gateway/.env")

# Suppress noisy HTTP/retry logs in demo mode — only show errors
logging.basicConfig(level=logging.ERROR, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("fintech").setLevel(logging.ERROR)

# ─── Banner ──────────────────────────────────────────────────────────────────

BANNER = """\
╔══════════════════════════════════════════════════════════════╗
║           OpenClaw Fintech — Multi-Agent Demo               ║
╠══════════════════════════════════════════════════════════════╣
║  Agents:                                                     ║
║    Trading   — prices, buy/sell, arbitrage, positions        ║
║    Portfolio — holdings, rebalance, performance              ║
║    DeFi      — governance, gas prices, swaps                 ║
║    Finance   — budget, expenses                              ║
║    Legal     — SEC filings, GDPR scan, legal research        ║
║                                                              ║
║  Type naturally or try these examples:                       ║
║    price AAPL          portfolio          governance          ║
║    buy $50 TSLA        rebalance          gas                 ║
║    arbitrage           budget             sec filing AAPL     ║
║    positions           expenses           gdpr scan <url>     ║
║                                                              ║
║  Commands:  health | agents | help | quit                    ║
╚══════════════════════════════════════════════════════════════╝
"""

# ─── Agent registry (same as gateway_bot.py, no Telegram dependency) ─────────

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
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for agent, info in AGENTS.items():
        score = sum(1 for pat in info["patterns"] if re.search(pat, text_lower))
        if score > 0:
            scores[agent] = score
    return max(scores, key=scores.get) if scores else None


# ─── Import agent handlers from gateway_bot (skip Telegram imports) ──────────

# We import the handler functions directly from the skill layer
# to avoid the `python-telegram-bot` dependency.

async def handle_trading(text: str) -> str:
    from skills.trading.handlers import (
        get_prices, execute_trade, check_arbitrage, get_positions, market_update,
    )
    text_lower = text.lower().strip()

    if re.search(r"\bprices?\b", text_lower) or re.search(
        r"\b(aapl|tsla|msft|nvda|spy|btc|eth|sol)\b", text_lower
    ):
        match = re.search(r"\b(aapl|tsla|msft|nvda|spy)\b", text_lower)
        if match:
            results = await get_prices([f"{match.group(1).upper()}/USD"])
        elif re.search(r"\b(btc|eth|sol)\b", text_lower):
            m = re.search(r"\b(btc|eth|sol)\b", text_lower)
            results = await get_prices([f"{m.group(1).upper()}/USDT"])
        else:
            results = await get_prices()

        if not results:
            return "Could not fetch prices. Markets may be closed."
        lines = ["Current Prices\n"]
        for r in results:
            arrow = "+" if r["change_24h_pct"] >= 0 else ""
            lines.append(
                f"  {r['pair']:<12} ${r['price']:>10,.2f}  "
                f"({arrow}{r['change_24h_pct']:.2f}%)  [{r['exchange']}]"
            )
        return "\n".join(lines)

    if re.search(r"\bpositions?\b", text_lower):
        pos = await get_positions()
        lines = [
            "Open Positions\n",
            f"  Daily volume: ${pos['daily_volume_used']:,.2f} / ${pos['daily_limit']:,.2f}",
        ]
        if pos["open_positions"]:
            for p in pos["open_positions"]:
                lines.append(
                    f"  {p['pair']} — entry ${p['entry_price']:,.2f}, "
                    f"qty {p['amount']:.4f} on {p['exchange']}"
                )
        else:
            lines.append("  No open positions.")
        return "\n".join(lines)

    if re.search(r"\barbitrage\b", text_lower):
        opps = await check_arbitrage()
        if not opps:
            return "No arbitrage opportunities found right now."
        lines = ["Arbitrage Opportunities\n"]
        for o in opps:
            lines.append(
                f"  {o['pair']}: Buy on {o['buy_on']} (${o['buy_price']:,.2f}), "
                f"sell on {o['sell_on']} (${o['sell_price']:,.2f}) — "
                f"net profit: ${o['net_profit_usd']:.2f}"
            )
        return "\n".join(lines)

    if re.search(r"\bmarket\s*update\b", text_lower):
        return await market_update()

    buy_match = re.search(
        r"\b(buy|sell)\s+\$?(\d+(?:\.\d+)?)\s+(?:of\s+)?(\w+)", text_lower
    )
    if buy_match:
        side = buy_match.group(1).upper()
        amount = float(buy_match.group(2))
        symbol = buy_match.group(3).upper()

        if symbol in ("AAPL", "TSLA", "MSFT", "NVDA", "SPY"):
            pair, exchange = f"{symbol}/USD", "alpaca"
        elif symbol in ("BTC", "ETH", "SOL"):
            pair, exchange = f"{symbol}/USDT", "binance"
        else:
            return f"Unknown symbol: {symbol}"

        result = await execute_trade(pair, side, amount, exchange)
        if "error" in result:
            return f"Trade rejected: {result['error']}"
        if result.get("status") == "awaiting_approval":
            return result["message"]
        return (
            f"Trade Executed\n"
            f"  Order: {result['order_id']}\n"
            f"  {result['side']} {result['pair']}\n"
            f"  Amount: {result['amount']:.4f} @ ${result.get('price', 0):,.2f}"
        )

    results = await get_prices()
    if results:
        lines = ["Market Overview\n"]
        for r in results:
            arrow = "+" if r["change_24h_pct"] >= 0 else ""
            lines.append(f"  {r['pair']:<12} ${r['price']:>10,.2f}  ({arrow}{r['change_24h_pct']:.2f}%)")
        return "\n".join(lines)

    return "Trading agent ready. Try: price AAPL, buy $50 TSLA, positions, arbitrage"


async def handle_portfolio(text: str) -> str:
    from skills.portfolio.handlers import get_portfolio, propose_rebalance, performance_report
    text_lower = text.lower()

    if re.search(r"\brebalanc", text_lower):
        result = await propose_rebalance()
        if "error" in result:
            return f"Error: {result['error']}"
        return result.get("message", "No rebalance needed.")

    if re.search(r"\bperformance\b", text_lower):
        return await performance_report()

    portfolio = await get_portfolio()
    lines = [
        f"Portfolio Overview\n",
        f"  Total Value: ${portfolio['total_value']:,.2f}\n",
        f"  {'Asset':<8} {'Value':>10} {'Actual':>8} {'Target':>8} {'Drift':>8}",
        f"  {'─'*8} {'─'*10} {'─'*8} {'─'*8} {'─'*8}",
    ]
    for h in portfolio["holdings"]:
        drift_sign = "+" if h["drift_pct"] >= 0 else ""
        lines.append(
            f"  {h['asset']:<8} ${h['value_usd']:>9,.2f} "
            f"{h['allocation_pct']:>7.1f}% {h['target_pct']:>7.1f}% "
            f"{drift_sign}{h['drift_pct']:>7.1f}%"
        )
    rebal = "YES" if portfolio["needs_rebalance"] else "NO"
    lines.append(f"\n  Rebalance needed: {rebal}")
    return "\n".join(lines)


async def handle_defi(text: str) -> str:
    text_lower = text.lower()
    if re.search(r"\bgovernance\b", text_lower):
        from skills.defi.handlers import check_governance
        result = await check_governance()
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    if re.search(r"\bgas\b", text_lower):
        from skills.defi.handlers import get_gas_prices
        result = await get_gas_prices()
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    return (
        "DeFi Agent\n\n"
        "  governance — check active proposals\n"
        "  gas        — current gas prices\n"
        "  swap, wallet, liquidity — require Alchemy API key"
    )


async def handle_finance(text: str) -> str:
    text_lower = text.lower()
    if re.search(r"\bbudget\b", text_lower):
        from skills.finance.handlers import budget_status
        result = await budget_status()
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    if re.search(r"\bexpense\b", text_lower):
        from skills.finance.handlers import get_expenses
        result = await get_expenses()
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    return (
        "Finance Agent\n\n"
        "  budget   — check budget status\n"
        "  expense  — view expenses"
    )


async def handle_legal(text: str) -> str:
    text_lower = text.lower()
    if re.search(r"\bsec\b", text_lower):
        from skills.legal.handlers import check_sec_filings
        result = await check_sec_filings()
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    if re.search(r"\blegal\s*research\b", text_lower):
        from skills.legal.handlers import legal_research
        match = re.search(r"legal\s*research\s+(.+)", text_lower)
        query = match.group(1) if match else text
        result = await legal_research(query)
        return result if isinstance(result, str) else json.dumps(result, indent=2)
    if re.search(r"\bgdpr\b", text_lower):
        from skills.legal.handlers import gdpr_scan
        match = re.search(r"gdpr\s+(?:scan\s+)?(\S+)", text_lower)
        if match:
            result = await gdpr_scan(match.group(1))
            return result.get("message", json.dumps(result, indent=2)) if isinstance(result, dict) else str(result)
        return "Usage: gdpr scan https://example.com"
    return (
        "Legal Agent\n\n"
        "  sec filing        — check SEC filings\n"
        "  legal research X  — search case law\n"
        "  gdpr scan <url>   — GDPR compliance check"
    )


AGENT_HANDLERS = {
    "trading-agent": handle_trading,
    "portfolio-agent": handle_portfolio,
    "defi-agent": handle_defi,
    "finance-agent": handle_finance,
    "legal-agent": handle_legal,
}


# ─── Health check ────────────────────────────────────────────────────────────

async def run_health_check():
    from skills.shared.health import health_checker
    print("\n  Running health checks...\n")
    results = await health_checker.check_all()
    print(health_checker.format_report(results))


# ─── Main loop ───────────────────────────────────────────────────────────────

async def main():
    print(BANNER)

    # Quick environment check
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if api_key and not api_key.startswith("sk-ant-xxx"):
        print("  Anthropic API key: configured")
    else:
        print("  Anthropic API key: NOT SET (set ANTHROPIC_API_KEY in gateway/.env)")

    print(f"  All trades use Alpaca paper trading (no real money)\n")

    while True:
        try:
            text = input("  You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!")
            break

        if not text:
            continue

        cmd = text.lower()

        if cmd in ("quit", "exit", "q"):
            print("  Goodbye!")
            break
        if cmd == "health":
            await run_health_check()
            print()
            continue
        if cmd == "agents":
            print("\n  Registered Agents:\n")
            for name, info in AGENTS.items():
                print(f"    {name:<20} {info['description']}")
            print()
            continue
        if cmd == "help":
            print(BANNER)
            continue

        try:
            agent = route_message(text)
            if not agent:
                print("\n  No agent matched. Try: price AAPL, portfolio, governance, budget, sec filing\n")
                continue

            agent_label = agent.replace("-", " ").title()
            print(f"\n  -> Routing to {agent_label}...")

            handler = AGENT_HANDLERS[agent]
            response = await handler(text)
            print(f"\n{response}\n")
        except Exception as e:
            print(f"\n  Error: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
