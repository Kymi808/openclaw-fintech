"""
Intraday agent handlers.

Provides scanning for intraday setups and manages active signals.
Integrates with the execution agent for trade placement.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from skills.shared import get_logger, audit_log, approval_engine
from skills.execution.session import is_market_open, get_session, minutes_to_close
from .scanner import IntradayScanner

logger = get_logger("intraday.handlers")

STATE_FILE = Path("./workspaces/execution-agent/intraday_signals.json")

# Risk limits for intraday
MAX_CONCURRENT_INTRADAY = 5     # max simultaneous intraday positions
MAX_INTRADAY_NOTIONAL = 20_000  # max total $ in intraday positions
MAX_SINGLE_POSITION = 5_000     # max $ per intraday trade
MIN_MINUTES_TO_CLOSE = 30       # don't open new positions within 30 min of close


def _load_state() -> dict:
    from skills.shared.state import safe_load_state
    return safe_load_state(STATE_FILE, {"active_signals": [], "trade_history": [], "last_scan": None})


def _save_state(state: dict) -> None:
    from skills.shared.state import safe_save_state
    safe_save_state(STATE_FILE, state)


async def scan_for_setups(
    universe: list[str] = None,
    model_predictions: dict[str, float] = None,
) -> dict:
    """
    Scan the market for intraday trading setups.

    Signals are filtered by ML model conviction:
    - Only BUY stocks the model likes
    - Only SHORT stocks the model dislikes
    - Conflicting signals are discarded

    Returns dict with active signals and summary.
    """
    session = get_session()
    if not is_market_open():
        return {
            "status": "market_closed",
            "session": session.value,
            "message": f"Market is {session.value} — no intraday setups available.",
        }

    mtc = minutes_to_close()
    if mtc < MIN_MINUTES_TO_CLOSE:
        return {
            "status": "too_close_to_eod",
            "minutes_to_close": mtc,
            "message": f"Only {mtc} min to close — not opening new intraday positions.",
        }

    scanner = IntradayScanner(universe, model_predictions)
    signals = await scanner.scan()

    # Save to state
    state = _load_state()
    state["active_signals"] = [s.to_dict() for s in signals]
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    audit_log("intraday-agent", "scan_complete", {
        "n_signals": len(signals),
        "session": session.value,
        "minutes_to_close": mtc,
    })

    if not signals:
        return {
            "status": "no_setups",
            "message": "No intraday setups found. Market conditions may not be favorable.",
        }

    # Format for display
    lines = [
        f"Intraday Scan — {len(signals)} setups found",
        f"Session: {session.value} | {mtc} min to close",
        "",
    ]

    for i, sig in enumerate(signals[:10], 1):
        side_icon = "LONG" if sig.side == "buy" else "SHORT"
        lines.append(
            f"  {i}. {sig.symbol} {side_icon} @ ${sig.entry_price:,.2f}"
            f"  stop=${sig.stop_loss:,.2f}  target=${sig.target_price:,.2f}"
            f"  R:R={sig.risk_reward:.1f}  conf={sig.confidence:.0%}"
        )
        lines.append(f"     [{sig.signal_type}] {sig.reason}")
        lines.append("")

    return {
        "status": "signals_found",
        "n_signals": len(signals),
        "signals": [s.to_dict() for s in signals],
        "message": "\n".join(lines),
    }


async def execute_intraday_signal(signal_index: int = 0) -> dict:
    """
    Execute an intraday signal from the last scan.

    Requires approval for positions > $2,000.
    """
    state = _load_state()
    signals = state.get("active_signals", [])

    if not signals:
        return {"error": "No active signals. Run a scan first."}

    if signal_index >= len(signals):
        return {"error": f"Signal index {signal_index} out of range (have {len(signals)})"}

    sig = signals[signal_index]

    # Risk checks
    active_count = len([s for s in state.get("trade_history", []) if s.get("status") == "open"])
    if active_count >= MAX_CONCURRENT_INTRADAY:
        return {"error": f"Max concurrent intraday positions reached ({MAX_CONCURRENT_INTRADAY})"}

    # Size the position (risk-based: risk 1% of $100k = $1k per trade)
    risk_per_share = abs(sig["entry_price"] - sig["stop_loss"])
    if risk_per_share == 0:
        return {"error": "Invalid signal: zero risk"}

    position_size = min(MAX_SINGLE_POSITION, 1000 / risk_per_share * sig["entry_price"])
    position_size = round(position_size, 2)

    if position_size < 100:
        return {"error": f"Position too small (${position_size:.2f})"}

    # Approval for larger positions
    if position_size > 2000:
        req_id = approval_engine.create_request(
            agent="intraday-agent",
            action="intraday_trade",
            description=(
                f"{sig['side'].upper()} ${position_size:,.2f} of {sig['symbol']} "
                f"({sig['signal_type']}) — {sig['reason']}"
            ),
            amount=position_size,
            details=sig,
        )
        return {
            "status": "awaiting_approval",
            "request_id": req_id,
            "message": approval_engine.format_request_message(req_id),
        }

    return {
        "status": "ready",
        "symbol": sig["symbol"],
        "side": sig["side"],
        "notional": position_size,
        "entry": sig["entry_price"],
        "stop": sig["stop_loss"],
        "target": sig["target_price"],
        "signal_type": sig["signal_type"],
    }


async def get_active_signals() -> dict:
    """Return currently active intraday signals."""
    state = _load_state()
    signals = state.get("active_signals", [])
    last_scan = state.get("last_scan", "never")

    return {
        "n_signals": len(signals),
        "last_scan": last_scan,
        "signals": signals[:10],
    }


async def heartbeat() -> str:
    """Intraday agent status check."""
    session = get_session()
    state = _load_state()
    signals = state.get("active_signals", [])

    lines = [
        f"Intraday Agent — {session.value}",
        f"  Last scan: {state.get('last_scan', 'never')}",
        f"  Active signals: {len(signals)}",
    ]

    if is_market_open():
        lines.append(f"  Minutes to close: {minutes_to_close()}")
        if signals:
            lines.append("  Top setups:")
            for sig in signals[:3]:
                side = "LONG" if sig["side"] == "buy" else "SHORT"
                lines.append(
                    f"    {sig['symbol']} {side} ({sig['signal_type']}) "
                    f"conf={sig['confidence']:.0%}"
                )
    else:
        lines.append("  Market closed — no active scanning")

    return "\n".join(lines)
