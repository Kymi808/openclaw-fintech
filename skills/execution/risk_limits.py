"""
Pre-trade risk limits — institutional-grade checks before every order.

These are hard limits that cannot be overridden by agents or PM decisions.
They protect against catastrophic loss regardless of what the model says.

Based on:
- SEC Regulation SHO (short selling rules)
- FINRA margin requirements
- Standard prop desk risk limits
"""
from skills.shared import get_logger

logger = get_logger("execution.risk_limits")


# ── Hard Limits (cannot be overridden) ───────────────────────────────────

# Maximum gross exposure as % of equity (including margin)
MAX_GROSS_EXPOSURE = 2.0  # 200% — standard Reg T margin

# Maximum net exposure (long - short) as % of equity
MAX_NET_EXPOSURE = 0.80  # 80% — prevents full directional bet

# Maximum single position as % of equity
MAX_SINGLE_POSITION = 0.10  # 10% — diversification requirement

# Maximum daily loss before halting all trading
MAX_DAILY_LOSS_PCT = -0.03  # -3% daily loss → halt

# Maximum number of open positions
MAX_TOTAL_POSITIONS = 30

# Minimum equity to trade (below this = maintenance margin call risk)
MIN_EQUITY = 25_000  # PDT rule minimum

# Maximum number of trades per day (prevents churning)
MAX_TRADES_PER_DAY = 50

# Maximum sector concentration
MAX_SECTOR_EXPOSURE = 0.30  # 30% in any one sector


def check_pre_trade_limits(
    order_notional: float,
    order_side: str,
    symbol: str,
    account_equity: float,
    current_positions: dict[str, float],
    daily_pnl: float = 0.0,
    trades_today: int = 0,
    sector_map: dict[str, str] = None,
) -> tuple[bool, str]:
    """
    Check all pre-trade risk limits.

    Returns (allowed, reason).
    These are HARD limits — the system must not trade if any fails.
    """
    # 1. Minimum equity
    if account_equity < MIN_EQUITY:
        return False, f"Equity ${account_equity:,.2f} below minimum ${MIN_EQUITY:,.2f} (PDT rule)"

    # 2. Daily loss limit
    daily_loss_pct = daily_pnl / account_equity if account_equity > 0 else 0
    if daily_loss_pct < MAX_DAILY_LOSS_PCT:
        return False, (
            f"Daily loss {daily_loss_pct:.2%} exceeds limit {MAX_DAILY_LOSS_PCT:.2%} — "
            f"trading halted for the day"
        )

    # 3. Max trades per day
    if trades_today >= MAX_TRADES_PER_DAY:
        return False, f"Max trades per day ({MAX_TRADES_PER_DAY}) reached — prevents churning"

    # 4. Max total positions
    if len(current_positions) >= MAX_TOTAL_POSITIONS and symbol not in current_positions:
        return False, f"Max positions ({MAX_TOTAL_POSITIONS}) reached"

    # 5. Single position size
    if account_equity > 0 and order_notional / account_equity > MAX_SINGLE_POSITION:
        pct = order_notional / account_equity
        return False, (
            f"Position ${order_notional:,.2f} is {pct:.1%} of equity — "
            f"exceeds {MAX_SINGLE_POSITION:.0%} single position limit"
        )

    # 6. Gross exposure
    long_exposure = sum(v for v in current_positions.values() if v > 0)
    short_exposure = sum(abs(v) for v in current_positions.values() if v < 0)
    new_gross = long_exposure + short_exposure + abs(order_notional)
    gross_pct = new_gross / account_equity if account_equity > 0 else 0

    if gross_pct > MAX_GROSS_EXPOSURE:
        return False, (
            f"Gross exposure would be {gross_pct:.1%} — "
            f"exceeds {MAX_GROSS_EXPOSURE:.0%} limit (Reg T)"
        )

    # 7. Net exposure
    if order_side == "buy":
        new_net = long_exposure + order_notional - short_exposure
    else:
        new_net = long_exposure - short_exposure - order_notional
    net_pct = abs(new_net) / account_equity if account_equity > 0 else 0

    if net_pct > MAX_NET_EXPOSURE:
        return False, (
            f"Net exposure would be {net_pct:.1%} — "
            f"exceeds {MAX_NET_EXPOSURE:.0%} limit"
        )

    # 8. Sector concentration
    if sector_map:
        sector = sector_map.get(symbol, "Unknown")
        sector_exposure = sum(
            abs(v) for sym, v in current_positions.items()
            if sector_map.get(sym) == sector
        ) + abs(order_notional)
        sector_pct = sector_exposure / account_equity if account_equity > 0 else 0

        if sector_pct > MAX_SECTOR_EXPOSURE:
            return False, (
                f"Sector '{sector}' exposure would be {sector_pct:.1%} — "
                f"exceeds {MAX_SECTOR_EXPOSURE:.0%} limit"
            )

    return True, "All pre-trade checks passed"


def check_daily_loss_halt(
    daily_pnl: float,
    account_equity: float,
) -> bool:
    """
    Check if daily loss limit has been breached.
    If True, ALL trading should halt for the remainder of the day.
    """
    if account_equity <= 0:
        return True
    return (daily_pnl / account_equity) < MAX_DAILY_LOSS_PCT
