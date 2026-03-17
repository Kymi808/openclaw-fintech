"""
Trading strategies: signal generation, arbitrage detection, and risk checks.
"""
from dataclasses import dataclass
from typing import Optional

from skills.shared import get_logger, DEFAULT_LIMITS, ALLOWED_PAIRS
from .exchange_client import Ticker

logger = get_logger("strategy")


@dataclass
class TradeSignal:
    pair: str
    side: str  # BUY or SELL
    price: float
    amount_usd: float
    reasoning: str
    risk: str  # LOW, MEDIUM, HIGH
    requires_approval: bool
    source: str  # "momentum", "arbitrage", "mean_reversion", etc.


@dataclass
class ArbitrageOpportunity:
    pair: str
    buy_exchange: str
    buy_price: float
    sell_exchange: str
    sell_price: float
    spread_pct: float
    estimated_profit_usd: float
    estimated_fees_usd: float
    net_profit_usd: float


def check_risk_limits(
    amount_usd: float,
    daily_volume_used: float,
    open_positions: int,
    limits: dict = None,
) -> tuple[bool, str]:
    """Validate a proposed trade against risk limits. Returns (ok, reason)."""
    lim = limits or DEFAULT_LIMITS

    if amount_usd > lim["max_single_trade"]:
        return False, f"Exceeds max single trade (${lim['max_single_trade']})"

    if daily_volume_used + amount_usd > lim["max_daily_volume"]:
        remaining = lim["max_daily_volume"] - daily_volume_used
        return False, f"Exceeds daily limit. Remaining: ${remaining:.2f}"

    if open_positions >= lim["max_open_positions"]:
        return False, f"Max open positions reached ({lim['max_open_positions']})"

    return True, "OK"


def needs_approval(amount_usd: float, limits: dict = None) -> bool:
    """Check if a trade amount requires human approval."""
    lim = limits or DEFAULT_LIMITS
    return amount_usd > lim["approval_threshold"]


def detect_arbitrage(
    tickers_by_exchange: dict[str, list[Ticker]],
    min_spread_pct: float = 0.5,
    max_trade_size: float = 200.0,
    estimated_fee_pct: float = 0.2,
) -> list[ArbitrageOpportunity]:
    """
    Detect arbitrage opportunities across exchanges.
    tickers_by_exchange: {"binance": [Ticker, ...], "coinbase": [Ticker, ...]}
    """
    opportunities = []

    # Build price maps: pair -> {exchange: price}
    price_map: dict[str, dict[str, float]] = {}
    for exchange, tickers in tickers_by_exchange.items():
        for t in tickers:
            if t.pair not in price_map:
                price_map[t.pair] = {}
            price_map[t.pair][exchange] = t.price

    for pair, prices in price_map.items():
        if pair not in ALLOWED_PAIRS:
            continue
        exchanges = list(prices.keys())
        if len(exchanges) < 2:
            continue

        for i, ex_a in enumerate(exchanges):
            for ex_b in exchanges[i + 1:]:
                price_a = prices[ex_a]
                price_b = prices[ex_b]

                if price_a < price_b:
                    buy_ex, buy_price = ex_a, price_a
                    sell_ex, sell_price = ex_b, price_b
                else:
                    buy_ex, buy_price = ex_b, price_b
                    sell_ex, sell_price = ex_a, price_a

                spread_pct = ((sell_price - buy_price) / buy_price) * 100

                if spread_pct >= min_spread_pct:
                    trade_amount = min(max_trade_size, 200.0)
                    qty = trade_amount / buy_price
                    gross_profit = qty * (sell_price - buy_price)
                    fees = trade_amount * (estimated_fee_pct / 100) * 2  # both sides
                    net_profit = gross_profit - fees

                    if net_profit > 0:
                        opportunities.append(ArbitrageOpportunity(
                            pair=pair,
                            buy_exchange=buy_ex,
                            buy_price=buy_price,
                            sell_exchange=sell_ex,
                            sell_price=sell_price,
                            spread_pct=spread_pct,
                            estimated_profit_usd=gross_profit,
                            estimated_fees_usd=fees,
                            net_profit_usd=net_profit,
                        ))

    opportunities.sort(key=lambda o: o.net_profit_usd, reverse=True)
    return opportunities


def simple_momentum_signal(
    tickers: list[Ticker],
    momentum_threshold_pct: float = 3.0,
    trade_amount_usd: float = 50.0,
) -> list[TradeSignal]:
    """
    Simple momentum strategy: buy if 24h change is strongly positive,
    sell if strongly negative. This is a basic example — replace with
    your own strategy logic.
    """
    signals = []

    for t in tickers:
        if t.pair not in ALLOWED_PAIRS:
            continue

        if t.change_24h_pct >= momentum_threshold_pct:
            signals.append(TradeSignal(
                pair=t.pair,
                side="BUY",
                price=t.price,
                amount_usd=trade_amount_usd,
                reasoning=(
                    f"{t.pair} up {t.change_24h_pct:.1f}% in 24h on "
                    f"{t.exchange}. Momentum buy signal."
                ),
                risk="MEDIUM",
                requires_approval=needs_approval(trade_amount_usd),
                source="momentum",
            ))
        elif t.change_24h_pct <= -momentum_threshold_pct:
            signals.append(TradeSignal(
                pair=t.pair,
                side="SELL",
                price=t.price,
                amount_usd=trade_amount_usd,
                reasoning=(
                    f"{t.pair} down {abs(t.change_24h_pct):.1f}% in 24h on "
                    f"{t.exchange}. Defensive sell signal."
                ),
                risk="MEDIUM",
                requires_approval=needs_approval(trade_amount_usd),
                source="momentum",
            ))

    return signals


def format_market_update(tickers: list[Ticker], daily_pnl: float = 0.0,
                         open_positions: int = 0) -> str:
    """Format a market update message for sending to user."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    lines = [f"🔄 Market Update [{now}]"]

    for t in tickers:
        arrow = "▲" if t.change_24h_pct >= 0 else "▼"
        base = t.pair.split("/")[0]
        lines.append(
            f"{base}: ${t.price:,.2f} ({arrow} {abs(t.change_24h_pct):.1f}%)"
        )

    lines.append(f"Open positions: {open_positions}")
    lines.append(f"Daily P&L: ${daily_pnl:+,.2f}")

    return "\n".join(lines)


def format_trade_signal(signal: TradeSignal) -> str:
    """Format a trade signal message for sending to user."""
    approval_text = "YES" if signal.requires_approval else "NO"
    return (
        f"📊 Trade Signal: {signal.side} {signal.pair}\n"
        f"Price: ${signal.price:,.2f}\n"
        f"Reasoning: {signal.reasoning}\n"
        f"Risk: {signal.risk}\n"
        f"Amount: ${signal.amount_usd:.2f}\n"
        f"⚠️ Requires approval: {approval_text}"
    )
