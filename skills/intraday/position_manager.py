"""
Intraday position management after entry.

Handles:
- Trailing stops (lock in profits as price moves in your favor)
- Partial profit-taking (scale out at milestones)
- Time-based exits (reduce size as EOD approaches)
- Signal invalidation (exit if setup thesis breaks)
- Asymmetric management for longs vs shorts (overnight premium effect)
  - Intraday longs: tighter stops, faster profit-taking (fighting intraday drag)
  - Intraday shorts: wider stops, let winners run (aligned with intraday weakness)
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from skills.shared import get_logger

logger = get_logger("intraday.position_manager")


@dataclass
class ManagedPosition:
    """An active intraday position with management state."""
    symbol: str
    side: str                    # "buy" or "sell"
    entry_price: float
    current_price: float
    initial_stop: float
    trailing_stop: float         # moves with price
    target_price: float
    signal_type: str
    entry_time: datetime
    qty_remaining_pct: float = 1.0  # 1.0 = full, 0.5 = half scaled out
    highest_price: float = 0.0   # for trailing stop (long)
    lowest_price: float = 0.0    # for trailing stop (short)
    partial_exits: list = field(default_factory=list)

    # VWAP invalidation tracking
    entry_vwap: float = 0.0
    current_vwap: float = 0.0

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.side == "buy":
            return (self.current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - self.current_price) / self.entry_price

    @property
    def is_profitable(self) -> bool:
        return self.unrealized_pnl_pct > 0


# ── Trailing Stop Logic ──────────────────────────────────────────────────

def update_trailing_stop(pos: ManagedPosition) -> ManagedPosition:
    """
    Update trailing stop based on current price.

    Asymmetric trailing for intraday overnight premium effect:
    - LONGS: tighter trailing (lock profits fast, intraday drag is real)
      Breakeven at 35%, lock 60% at 60%, lock 80% at target
    - SHORTS: wider trailing (let winners run, aligned with intraday weakness)
      Breakeven at 50%, lock 40% at 75%, lock 65% at target
    """
    if pos.side == "buy":
        pos.highest_price = max(pos.highest_price, pos.current_price)
        profit = pos.current_price - pos.entry_price
        target_distance = pos.target_price - pos.entry_price

        if target_distance <= 0:
            return pos

        progress = profit / target_distance

        # Longs: tighter stops — take profit before EOD weakness
        if progress >= 1.0:
            pos.trailing_stop = max(pos.trailing_stop, pos.entry_price + profit * 0.80)
        elif progress >= 0.60:
            pos.trailing_stop = max(pos.trailing_stop, pos.entry_price + profit * 0.60)
        elif progress >= 0.35:
            pos.trailing_stop = max(pos.trailing_stop, pos.entry_price)

    else:  # short
        pos.lowest_price = min(pos.lowest_price, pos.current_price) if pos.lowest_price > 0 else pos.current_price
        profit = pos.entry_price - pos.current_price
        target_distance = pos.entry_price - pos.target_price

        if target_distance <= 0:
            return pos

        progress = profit / target_distance

        # Shorts: wider stops — let it run, intraday weakness is your friend
        if progress >= 1.0:
            pos.trailing_stop = min(pos.trailing_stop, pos.entry_price - profit * 0.65)
        elif progress >= 0.75:
            pos.trailing_stop = min(pos.trailing_stop, pos.entry_price - profit * 0.40)
        elif progress >= 0.50:
            pos.trailing_stop = min(pos.trailing_stop, pos.entry_price)

    return pos


# ── Partial Profit-Taking ────────────────────────────────────────────────

# Scale-out milestones for intraday LONGS: aggressive profit-taking
# (fighting intraday drag — take profits quickly before EOD weakness)
SCALE_OUT_LONG = [
    (0.40, 0.33),  # At 40% of target: take 1/3 (earlier than shorts)
    (0.75, 0.50),  # At 75%: take half remaining
    # Final portion runs with tight trailing stop
]

# Scale-out milestones for intraday SHORTS: let winners run
# (aligned with intraday weakness — shorts naturally work intraday)
SCALE_OUT_SHORT = [
    (0.60, 0.25),  # At 60% of target: take only 1/4 (let it run)
    (1.00, 0.50),  # At target: take half
    # Final portion runs with wider trailing stop
]


def check_partial_exit(pos: ManagedPosition) -> Optional[tuple[float, str]]:
    """
    Check if a partial profit-taking exit is triggered.

    Uses asymmetric scale-out:
    - Longs: aggressive profit-taking (fighting intraday drag)
    - Shorts: let winners run (aligned with intraday weakness)

    Returns (exit_fraction, reason) or None.
    """
    if pos.side == "buy":
        profit = pos.current_price - pos.entry_price
        target_distance = pos.target_price - pos.entry_price
    else:
        profit = pos.entry_price - pos.current_price
        target_distance = pos.entry_price - pos.target_price

    if target_distance <= 0:
        return None

    progress = profit / target_distance

    # Asymmetric: longs take profit faster, shorts let it run
    scale_out = SCALE_OUT_LONG if pos.side == "buy" else SCALE_OUT_SHORT

    for level_pct, exit_frac in scale_out:
        level_key = f"scale_{level_pct:.0%}"
        if progress >= level_pct and level_key not in pos.partial_exits:
            pos.partial_exits.append(level_key)
            actual_exit = exit_frac * pos.qty_remaining_pct
            pos.qty_remaining_pct -= actual_exit
            return (
                actual_exit,
                f"Scale out {actual_exit:.0%} at {progress:.0%} of target "
                f"({pos.qty_remaining_pct:.0%} remaining)",
            )

    return None


# ── Signal Invalidation ─────────────────────────────────────────────────

def check_invalidation(pos: ManagedPosition) -> Optional[str]:
    """
    Check if the original signal thesis is still valid.

    Invalidation conditions:
    - VWAP reversion: VWAP shifted > 0.5% from entry VWAP (anchor moved)
    - ORB: Price re-enters opening range (breakout failed)
    - Momentum burst: Volume dried up (momentum exhausted)
    """
    if pos.signal_type == "vwap_reversion":
        if pos.entry_vwap > 0 and pos.current_vwap > 0:
            vwap_shift = abs(pos.current_vwap - pos.entry_vwap) / pos.entry_vwap
            if vwap_shift > 0.005:  # VWAP moved > 0.5%
                return (
                    f"VWAP invalidation: VWAP shifted {vwap_shift:.2%} "
                    f"from entry ({pos.entry_vwap:.2f} → {pos.current_vwap:.2f})"
                )

    # For all signals: if loss exceeds 2x the initial risk, thesis is broken
    initial_risk = abs(pos.entry_price - pos.initial_stop)
    current_loss = -pos.unrealized_pnl_pct * pos.entry_price
    if current_loss > initial_risk * 2:
        return f"Max loss exceeded: {pos.unrealized_pnl_pct:.2%} (2x initial risk)"

    return None


# ── Time-Based Exit ──────────────────────────────────────────────────────

def check_time_exit(
    pos: ManagedPosition,
    minutes_to_close: int,
    max_hold_minutes: int = 120,
) -> Optional[str]:
    """
    Check time-based exit conditions.

    - Mandatory close 15 min before market close
    - Reduce position by 50% when max_hold_minutes reached
    - Full close at 1.5x max_hold_minutes
    """
    if minutes_to_close <= 15:
        return "EOD mandatory close (15 min to market close)"

    if pos.entry_time:
        hold_minutes = (datetime.now() - pos.entry_time).total_seconds() / 60

        if hold_minutes >= max_hold_minutes * 1.5:
            return f"Max hold time exceeded ({hold_minutes:.0f} min)"

        if hold_minutes >= max_hold_minutes and pos.qty_remaining_pct > 0.5:
            return f"Hold time limit ({hold_minutes:.0f} min) — reducing position"

    return None


# ── Master Update Function ───────────────────────────────────────────────

@dataclass
class ManagementAction:
    action: str          # "hold", "partial_exit", "full_exit", "update_stop"
    reason: str
    exit_pct: float = 0  # fraction to exit (0 = hold, 1 = full exit)
    new_stop: float = 0


def update_position(
    pos: ManagedPosition,
    current_price: float,
    current_vwap: float = 0.0,
    minutes_to_close: int = 999,
    max_hold_minutes: int = 120,
) -> ManagementAction:
    """
    Master update: check all management rules and return the highest-priority action.

    Priority: invalidation > time exit > stop hit > partial profit > trailing stop update
    """
    pos.current_price = current_price
    pos.current_vwap = current_vwap

    # 1. Signal invalidation
    inv = check_invalidation(pos)
    if inv:
        return ManagementAction("full_exit", inv, exit_pct=1.0)

    # 2. Time-based exit
    time_exit = check_time_exit(pos, minutes_to_close, max_hold_minutes)
    if time_exit:
        if "mandatory" in time_exit or "exceeded" in time_exit:
            return ManagementAction("full_exit", time_exit, exit_pct=1.0)
        else:
            return ManagementAction("partial_exit", time_exit, exit_pct=0.5)

    # 3. Stop hit
    if pos.side == "buy" and current_price <= pos.trailing_stop:
        return ManagementAction("full_exit", f"Trailing stop hit at {pos.trailing_stop:.2f}", exit_pct=pos.qty_remaining_pct)
    if pos.side == "sell" and current_price >= pos.trailing_stop:
        return ManagementAction("full_exit", f"Trailing stop hit at {pos.trailing_stop:.2f}", exit_pct=pos.qty_remaining_pct)

    # 4. Partial profit-taking
    partial = check_partial_exit(pos)
    if partial:
        exit_frac, reason = partial
        return ManagementAction("partial_exit", reason, exit_pct=exit_frac)

    # 5. Update trailing stop
    old_stop = pos.trailing_stop
    pos = update_trailing_stop(pos)
    if pos.trailing_stop != old_stop:
        return ManagementAction("update_stop", f"Trailing stop → {pos.trailing_stop:.2f}", new_stop=pos.trailing_stop)

    return ManagementAction("hold", "No action needed")
