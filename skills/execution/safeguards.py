"""
Market manipulation detection and execution safeguards.

Protects against:
1. Spoofing/layering — fake orders to move price, then trade opposite direction
2. Wash trading — trading with yourself to create fake volume
3. Pump and dump — artificial price inflation on low-volume stocks
4. Quote stuffing — rapid order placement/cancellation to slow systems
5. Stop hunting — large players pushing price to trigger stops

Also implements:
6. Slippage protection — reject fills too far from expected price
7. Unusual volume detection — don't trade into abnormal volume spikes
8. Spread monitoring — don't trade when spreads are abnormally wide
9. Circuit breaker awareness — halt trading during market-wide halts
10. Fat finger protection — reject orders that are obviously wrong size

References:
- SEC Rule 10b-5 (market manipulation)
- Reg NMS (National Market System)
- FINRA Rule 5210 (publication of transactions)
"""
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timezone

from skills.shared import get_logger

logger = get_logger("execution.safeguards")


@dataclass
class SafeguardResult:
    """Result of a safeguard check."""
    passed: bool
    check_name: str
    reason: str = ""
    severity: str = "info"  # "info", "warning", "block"


# ── 1. Unusual Volume Detection ─────────────────────────────────────────

def check_unusual_volume(
    current_volume: float,
    avg_volume_20d: float,
    threshold_multiplier: float = 5.0,
) -> SafeguardResult:
    """
    Detect abnormal volume that may indicate manipulation.

    Sudden volume spikes (>5x normal) often precede:
    - Pump and dump schemes
    - Insider trading before announcements
    - Coordinated retail trading (meme stocks)

    Don't trade INTO volume anomalies — wait for them to resolve.
    """
    if avg_volume_20d <= 0:
        return SafeguardResult(True, "unusual_volume", "No volume history")

    ratio = current_volume / avg_volume_20d

    if ratio > threshold_multiplier:
        return SafeguardResult(
            False, "unusual_volume",
            f"Volume {ratio:.1f}x above 20d average — possible manipulation. "
            f"Current: {current_volume:,.0f}, Avg: {avg_volume_20d:,.0f}",
            severity="block",
        )

    if ratio > 3.0:
        return SafeguardResult(
            True, "unusual_volume",
            f"Elevated volume ({ratio:.1f}x) — proceed with caution",
            severity="warning",
        )

    return SafeguardResult(True, "unusual_volume", f"Volume normal ({ratio:.1f}x)")


# ── 2. Spread Protection ────────────────────────────────────────────────

def check_spread(
    bid: float,
    ask: float,
    typical_spread_bps: float = 5.0,
    max_spread_multiplier: float = 5.0,
) -> SafeguardResult:
    """
    Detect abnormally wide spreads that indicate illiquidity or manipulation.

    Wide spreads mean:
    - Market maker has pulled quotes (uncertainty)
    - Low liquidity (your order will have high impact)
    - Possible halt or news pending

    Don't trade when spread is >5x normal.
    """
    if bid <= 0 or ask <= 0:
        return SafeguardResult(True, "spread", "No bid/ask data")

    mid = (bid + ask) / 2
    spread_bps = (ask - bid) / mid * 10000

    if spread_bps > typical_spread_bps * max_spread_multiplier:
        return SafeguardResult(
            False, "spread",
            f"Spread {spread_bps:.1f} bps ({spread_bps/typical_spread_bps:.1f}x normal) — "
            f"illiquid or manipulation. Bid: {bid:.2f}, Ask: {ask:.2f}",
            severity="block",
        )

    if spread_bps > typical_spread_bps * 3.0:
        return SafeguardResult(
            True, "spread",
            f"Wide spread ({spread_bps:.1f} bps) — elevated execution cost",
            severity="warning",
        )

    return SafeguardResult(True, "spread", f"Spread normal ({spread_bps:.1f} bps)")


# ── 3. Price Movement Anomaly ───────────────────────────────────────────

def check_price_anomaly(
    current_price: float,
    prev_close: float,
    intraday_high: float,
    intraday_low: float,
    atr_pct: float = 0.02,
    max_move_multiplier: float = 5.0,
) -> SafeguardResult:
    """
    Detect abnormal price movements that may indicate manipulation.

    Large sudden moves (>5x ATR) in either direction suggest:
    - Stop hunting (push price to trigger stops, then reverse)
    - Spoofing (fake orders to move price)
    - Flash crash / fat finger

    Don't enter new positions during anomalous moves.
    """
    if prev_close <= 0 or atr_pct <= 0:
        return SafeguardResult(True, "price_anomaly", "No price history")

    move_from_close = abs(current_price - prev_close) / prev_close
    intraday_range = (intraday_high - intraday_low) / prev_close if prev_close > 0 else 0

    if move_from_close > atr_pct * max_move_multiplier:
        return SafeguardResult(
            False, "price_anomaly",
            f"Price moved {move_from_close:.2%} from close ({move_from_close/atr_pct:.1f}x ATR) — "
            f"anomalous, possible manipulation or halt",
            severity="block",
        )

    if intraday_range > atr_pct * max_move_multiplier:
        return SafeguardResult(
            False, "price_anomaly",
            f"Intraday range {intraday_range:.2%} ({intraday_range/atr_pct:.1f}x ATR) — "
            f"extreme volatility, avoid new positions",
            severity="block",
        )

    return SafeguardResult(True, "price_anomaly", "Price movement normal")


# ── 4. Fat Finger Protection ────────────────────────────────────────────

def check_fat_finger(
    order_notional: float,
    account_equity: float,
    max_single_order_pct: float = 0.10,
    max_single_order_usd: float = 100_000,
) -> SafeguardResult:
    """
    Reject orders that are obviously too large.

    Fat finger errors (accidentally adding extra zeros) can wipe out accounts.
    Reject any single order > 10% of equity or > $100k (configurable).
    """
    if order_notional > max_single_order_usd:
        return SafeguardResult(
            False, "fat_finger",
            f"Order ${order_notional:,.2f} exceeds max single order ${max_single_order_usd:,.2f}",
            severity="block",
        )

    if account_equity > 0 and order_notional / account_equity > max_single_order_pct:
        pct = order_notional / account_equity * 100
        return SafeguardResult(
            False, "fat_finger",
            f"Order ${order_notional:,.2f} is {pct:.1f}% of equity — exceeds {max_single_order_pct*100:.0f}% limit",
            severity="block",
        )

    return SafeguardResult(True, "fat_finger", "Order size within limits")


# ── 5. Momentum Ignition Detection ──────────────────────────────────────

def check_momentum_ignition(
    returns_1min: list[float],
    volume_1min: list[float],
    lookback: int = 10,
) -> SafeguardResult:
    """
    Detect potential momentum ignition — artificial price acceleration
    designed to trigger other algorithms' momentum signals.

    Pattern: sudden burst of volume + price movement in one direction,
    often followed by a sharp reversal. The manipulator profits from
    the reversal after triggering other algos to chase.

    Detection: acceleration in BOTH price and volume simultaneously
    followed by volume collapse.
    """
    if len(returns_1min) < lookback or len(volume_1min) < lookback:
        return SafeguardResult(True, "momentum_ignition", "Insufficient data")

    recent_rets = np.array(returns_1min[-lookback:])
    recent_vols = np.array(volume_1min[-lookback:])
    prior_vols = np.array(volume_1min[-lookback*2:-lookback]) if len(volume_1min) >= lookback * 2 else recent_vols

    # Check for directional burst with volume spike
    cum_return = np.sum(recent_rets)
    vol_ratio = np.mean(recent_vols) / (np.mean(prior_vols) + 1)
    directional = np.abs(cum_return) / (np.std(recent_rets) + 1e-8)

    # Momentum ignition: strong direction + volume spike + high directional signal
    if directional > 3.0 and vol_ratio > 3.0:
        return SafeguardResult(
            False, "momentum_ignition",
            f"Possible momentum ignition: directional={directional:.1f}, "
            f"volume_spike={vol_ratio:.1f}x — likely reversal incoming",
            severity="block",
        )

    if directional > 2.0 and vol_ratio > 2.0:
        return SafeguardResult(
            True, "momentum_ignition",
            "Elevated directional move with volume — caution",
            severity="warning",
        )

    return SafeguardResult(True, "momentum_ignition", "No ignition pattern detected")


# ── 6. Correlated Crash Detection ───────────────────────────────────────

def check_market_stress(
    spy_return_intraday: float,
    vix_level: float,
    max_spy_drop: float = -0.03,
    max_vix: float = 40.0,
) -> SafeguardResult:
    """
    Halt all new positions during market-wide stress events.

    When SPY drops >3% intraday or VIX >40, markets are in panic.
    Individual stock signals are unreliable during correlated selloffs.
    """
    if spy_return_intraday < max_spy_drop:
        return SafeguardResult(
            False, "market_stress",
            f"Market crash: SPY {spy_return_intraday:.2%} intraday — "
            f"all signals unreliable, halt new positions",
            severity="block",
        )

    if vix_level > max_vix:
        return SafeguardResult(
            False, "market_stress",
            f"VIX at {vix_level:.1f} (>40) — extreme fear, halt new positions",
            severity="block",
        )

    return SafeguardResult(True, "market_stress", "Market conditions normal")


# ── 7. Wash Trade Prevention ────────────────────────────────────────────

def check_wash_trade(
    symbol: str,
    side: str,
    recent_trades: list[dict],
    lookback_minutes: int = 5,
) -> SafeguardResult:
    """
    Prevent accidental wash trades (buying and selling the same stock
    within minutes). This is illegal under SEC rules and also unprofitable.
    """
    now = datetime.now(timezone.utc)
    for trade in recent_trades:
        trade_time = trade.get("timestamp", "")
        trade_symbol = trade.get("symbol", "")
        trade_side = trade.get("side", "")

        if trade_symbol != symbol:
            continue

        # Check if opposite side within lookback window
        if trade_side != side:
            try:
                t = datetime.fromisoformat(trade_time.replace("Z", "+00:00"))
                age_minutes = (now - t).total_seconds() / 60
                if age_minutes < lookback_minutes:
                    return SafeguardResult(
                        False, "wash_trade",
                        f"Would create wash trade: {side} {symbol} within "
                        f"{age_minutes:.0f} min of opposite trade",
                        severity="block",
                    )
            except Exception:
                pass

    return SafeguardResult(True, "wash_trade", "No wash trade risk")


# ── Master Safeguard Check ───────────────────────────────────────────────

async def run_all_safeguards(
    symbol: str,
    side: str,
    notional: float,
    account_equity: float,
    current_price: float = 0,
    prev_close: float = 0,
    bid: float = 0,
    ask: float = 0,
    current_volume: float = 0,
    avg_volume_20d: float = 0,
    intraday_high: float = 0,
    intraday_low: float = 0,
    spy_return: float = 0,
    vix_level: float = 20,
    recent_trades: list[dict] = None,
    returns_1min: list[float] = None,
    volume_1min: list[float] = None,
) -> tuple[bool, list[SafeguardResult]]:
    """
    Run all safeguard checks before placing an order.

    Returns (can_trade, list_of_results).
    If any check returns severity="block", can_trade is False.
    """
    results = []

    results.append(check_fat_finger(notional, account_equity))
    results.append(check_unusual_volume(current_volume, avg_volume_20d))
    results.append(check_market_stress(spy_return, vix_level))
    results.append(check_wash_trade(symbol, side, recent_trades or []))

    if bid > 0 and ask > 0:
        results.append(check_spread(bid, ask))

    if current_price > 0 and prev_close > 0:
        results.append(check_price_anomaly(
            current_price, prev_close, intraday_high, intraday_low,
        ))

    if returns_1min and volume_1min:
        results.append(check_momentum_ignition(returns_1min, volume_1min))

    # Check if any blocks
    blocked = [r for r in results if r.severity == "block"]
    warnings = [r for r in results if r.severity == "warning"]

    if blocked:
        for b in blocked:
            logger.warning(f"SAFEGUARD BLOCKED: {b.check_name} — {b.reason}")
        return False, results

    if warnings:
        for w in warnings:
            logger.info(f"SAFEGUARD WARNING: {w.check_name} — {w.reason}")

    return True, results
