"""
Market session awareness for US equity markets.

Handles:
- Session detection (pre-market, open, closing, after-hours, closed)
- PDT rule compliance ($25k minimum for pattern day trading)
- Time-to-close calculations for intraday mandatory liquidation
"""
from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# US market hours
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
CLOSING_WINDOW = time(15, 45)  # last 15 min = "closing" phase
PRE_MARKET_START = time(4, 0)
AFTER_HOURS_END = time(20, 0)

# PDT rule
PDT_MIN_EQUITY = 25_000.0
PDT_MAX_DAY_TRADES = 3  # per 5 rolling business days for accounts < $25k

# Intraday liquidation
EOD_CLOSE_TIME = time(15, 45)  # close all intraday positions by this time


class MarketSession(Enum):
    CLOSED = "closed"
    PRE_MARKET = "pre_market"
    OPEN = "open"
    CLOSING = "closing"
    AFTER_HOURS = "after_hours"


def get_session(now: datetime = None) -> MarketSession:
    """Determine current market session."""
    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    # Weekend
    if now.weekday() >= 5:
        return MarketSession.CLOSED

    t = now.time()

    if t < PRE_MARKET_START:
        return MarketSession.CLOSED
    if t < MARKET_OPEN:
        return MarketSession.PRE_MARKET
    if t < CLOSING_WINDOW:
        return MarketSession.OPEN
    if t < MARKET_CLOSE:
        return MarketSession.CLOSING
    if t < AFTER_HOURS_END:
        return MarketSession.AFTER_HOURS
    return MarketSession.CLOSED


def is_market_open(now: datetime = None) -> bool:
    """Check if the market is currently open for trading."""
    session = get_session(now)
    return session in (MarketSession.OPEN, MarketSession.CLOSING)


def minutes_to_close(now: datetime = None) -> int:
    """Minutes remaining until market close (4:00 PM ET)."""
    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    delta = close_dt - now
    return max(0, int(delta.total_seconds() / 60))


def should_close_intraday(now: datetime = None) -> bool:
    """Check if it's time to close intraday positions (15:45 ET)."""
    if now is None:
        now = datetime.now(ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    return now.time() >= EOD_CLOSE_TIME


def check_pdt_compliance(
    account_equity: float,
    day_trade_count: int,
) -> tuple[bool, str]:
    """
    Check Pattern Day Trader rule compliance.

    Returns (allowed: bool, reason: str)
    """
    if account_equity >= PDT_MIN_EQUITY:
        return True, "Account equity above PDT threshold"

    if day_trade_count >= PDT_MAX_DAY_TRADES:
        return False, (
            f"PDT limit reached: {day_trade_count}/{PDT_MAX_DAY_TRADES} day trades "
            f"with equity ${account_equity:,.2f} (need ${PDT_MIN_EQUITY:,.2f})"
        )

    remaining = PDT_MAX_DAY_TRADES - day_trade_count
    return True, f"{remaining} day trades remaining (equity below PDT threshold)"
