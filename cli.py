#!/usr/bin/env python3
"""
OpenClaw — Production Trading CLI

Usage:
    python cli.py
"""
import asyncio
import logging
import os
import re
import sys

# Auto-set project root so imports work without PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv("gateway/.env", override=True)

# Production logging: JSON to file, human-readable to console
logging.basicConfig(level=logging.WARNING, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("fintech").setLevel(logging.WARNING)

BANNER = """\
╔══════════════════════════════════════════════════════════════╗
║              OpenClaw — Quant Trading System                ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Pipeline Commands:                                          ║
║    run cycle       — full daily: ML → debate → trade         ║
║    scan            — intraday signal scan                    ║
║    briefing        — market intelligence report              ║
║                                                              ║
║  Portfolio Commands:                                         ║
║    portfolio       — current positions + exposure            ║
║    pnl             — P&L report (daily/cumulative/Sharpe)    ║
║    reconcile       — verify positions vs Alpaca              ║
║    positions       — detailed position list from Alpaca      ║
║                                                              ║
║  Research & Intelligence:                                    ║
║    news            — aggregated news digest (macro/sector)   ║
║    report          — weekly senior analyst research report    ║
║    analysts        — all 5 analyst theses + convictions      ║
║    pm status       — PM parameters + last decision           ║
║    session         — market session + time to close           ║
║                                                              ║
║  Control Commands:                                           ║
║    approve APR-XX  — approve a pending decision              ║
║    deny APR-XX     — deny a pending decision                 ║
║    pending         — view pending approvals                  ║
║    health          — system health check                     ║
║    secrets         — verify API key configuration            ║
║                                                              ║
║  System:  help | quit                                        ║
╚══════════════════════════════════════════════════════════════╝
"""


# ─── Command Handlers ───────────────────────────────────────────────────

async def cmd_run_cycle() -> str:
    """Full daily pipeline: ML predictions → 5 analysts → 3 PMs → CIO → execute."""
    from skills.orchestrator.pipeline import run_daily_cycle
    result = await run_daily_cycle()

    if result.get("status") == "rate_limited":
        return result["message"]

    if result.get("status") == "awaiting_approval":
        return result.get("message", "Awaiting approval.")

    if result.get("status") == "failed":
        return f"Pipeline failed: {result.get('error', 'unknown')}"

    # Format successful result
    lines = [f"Daily Cycle Complete [{result.get('run_id', '')}]", ""]

    briefing = result.get("briefing", {})
    lines.append(f"  Market: {briefing.get('macro_summary', '')}")
    lines.append("")

    lines.append("  Analyst Convictions:")
    for name, thesis in sorted(
        result.get("analyst_theses", {}).items(),
        key=lambda x: -x[1].get("conviction", 0),
    ):
        params = thesis.get("recommended_params", {})
        lines.append(
            f"    {name:>10}: {thesis['conviction']:.3f}  "
            f"n_long={params.get('max_positions_long'):>2}, "
            f"n_short={params.get('max_positions_short'):>2}"
        )

    dec = result.get("decision", {})
    res = dec.get("resolution", {})
    params = dec.get("final_params", {})
    lines.append("")
    lines.append("  PM Proposals:")
    for pm_name, proposal in res.get("pm_proposals", {}).items():
        marker = " <-" if pm_name == res.get("selected_pm") else ""
        lines.append(
            f"    {pm_name:>12}: n_long={proposal.get('max_positions_long'):>2}, "
            f"leverage={proposal.get('max_gross_leverage', 0):.2f}{marker}"
        )

    lines.extend([
        "",
        f"  CIO Decision: {res.get('selected_pm')} PM",
        f"  {res.get('rationale', '')}",
        f"  Final: n_long={params.get('max_positions_long')}, "
        f"n_short={params.get('max_positions_short')}, "
        f"leverage={params.get('max_gross_leverage')}, "
        f"vol={params.get('target_annual_vol')}",
    ])

    execution = result.get("execution", {})
    if execution:
        lines.append(
            f"\n  Execution: {execution.get('orders_filled', 0)}/{execution.get('orders_placed', 0)} "
            f"orders, ${execution.get('total_notional', 0):,.2f} notional"
        )

    if result.get("elapsed_seconds"):
        lines.append(f"  Elapsed: {result['elapsed_seconds']}s")

    return "\n".join(lines)


async def cmd_scan() -> str:
    """Scan for intraday trading setups."""
    from skills.intraday.handlers import scan_for_setups
    result = await scan_for_setups()
    return result.get("message", "No setups found.")


async def cmd_briefing() -> str:
    """Market intelligence report."""
    from skills.intel.handlers import pre_market_briefing
    return await pre_market_briefing()


async def cmd_news() -> str:
    """Aggregated news digest from 3 news gathering agents."""
    from skills.news.aggregator import aggregate_all_news
    digest = await aggregate_all_news()

    lines = [
        f"News Digest — {digest.n_total} signals ({digest.n_breaking} breaking)",
        f"  Overall sentiment: {digest.overall_sentiment:+.3f}",
        f"  Key themes: {', '.join(digest.key_themes)}",
        "",
    ]

    if digest.macro_signals:
        lines.append("  MACRO:")
        for s in digest.macro_signals[:5]:
            lines.append(f"    [{s.urgency:>9}] {s.headline[:80]}")
            lines.append(f"              sentiment={s.sentiment:+.2f}  [{s.subcategory}]")
        lines.append("")

    if digest.sector_signals:
        lines.append("  SECTOR:")
        for s in digest.sector_signals[:5]:
            lines.append(f"    [{s.urgency:>9}] {s.headline[:80]}")
        lines.append("")

    if digest.company_signals:
        lines.append("  COMPANY:")
        for s in digest.company_signals[:5]:
            syms = ", ".join(s.symbols[:3])
            lines.append(f"    [{s.urgency:>9}] ({syms}) {s.headline[:70]}")
        lines.append("")

    if digest.sector_sentiment:
        lines.append("  Sector Sentiment:")
        for sec, sent in sorted(digest.sector_sentiment.items(), key=lambda x: -x[1]):
            lines.append(f"    {sec:<15} {sent:+.3f}")

    return "\n".join(lines)


async def cmd_feedback() -> str:
    """Show adaptive weight status — how the system is learning."""
    from skills.feedback.adapter import get_weight_adapter
    from skills.feedback.scorer import OutcomeScorer

    adapter = get_weight_adapter()
    scorer = OutcomeScorer()
    status = adapter.get_status()
    scores = scorer.get_all_agent_scores()

    lines = [
        "Adaptive Feedback Status",
        f"  Updates: {status.get('update_count', 0)}",
        f"  Last update: {status.get('last_update', 'never')}",
        "",
    ]

    analyst_adj = status.get("analyst_adjustments", {})
    if analyst_adj:
        lines.append("  Analyst Weight Multipliers (1.0 = baseline):")
        for name, mult in sorted(analyst_adj.items()):
            score = scores.get(f"{name}-analyst", {})
            trend = score.get("trend", 0)
            trend_arrow = "^" if trend > 0.02 else "v" if trend < -0.02 else "="
            lines.append(
                f"    {name:<12} {mult:.3f}x  "
                f"(score={score.get('avg_score', 0.5):.3f}, "
                f"n={score.get('n_scored', 0)}, {trend_arrow})"
            )
    else:
        lines.append("  No adaptive adjustments yet — need more scored predictions.")
        lines.append("  System will start learning after 5+ daily cycles.")

    return "\n".join(lines)


async def cmd_report() -> str:
    """Generate weekly senior analyst research report."""
    from skills.research.report import generate_weekly_report
    return await generate_weekly_report()


async def cmd_analysts() -> str:
    """Run all 5 analyst personalities and show their theses."""
    from skills.intel.handlers import gather_briefing
    from skills.analyst.handlers import form_all_theses
    from skills.analyst.personalities import ANALYST_PERSONALITIES
    from skills.orchestrator.pipeline import _dummy_predictions, _load_real_predictions, USE_REAL_MODELS

    briefing = await gather_briefing()
    try:
        predictions = _load_real_predictions() if USE_REAL_MODELS else _dummy_predictions()
    except Exception as e:
        return f"Could not load model predictions: {e}"
    portfolio_state = {"current_drawdown": 0.0}
    theses = await form_all_theses(briefing, predictions, portfolio_state)

    lines = ["Analyst Theses", f"  Market: {briefing.get('macro_summary', '')}", ""]
    for name, thesis in sorted(theses.items(), key=lambda x: -x[1].get("conviction", 0)):
        p = ANALYST_PERSONALITIES[name]
        params = thesis.get("recommended_params", {})
        flags = thesis.get("risk_flags", [])
        lines.append(
            f"  {p['name']:<22} conviction={thesis['conviction']:.3f}  "
            f"n_long={params.get('max_positions_long'):>2}, "
            f"n_short={params.get('max_positions_short'):>2}, "
            f"leverage={params.get('max_gross_leverage', 0):.2f}"
            + (f"  [{', '.join(flags)}]" if flags else "")
        )

    return "\n".join(lines)


async def cmd_portfolio() -> str:
    """Current portfolio: positions, exposure, PM parameters."""
    from skills.pm.handlers import get_current_params
    from skills.execution.session import get_session, minutes_to_close, is_market_open

    params = await get_current_params()
    session = get_session()

    lines = [
        f"Portfolio Status — {session.value}",
    ]

    if is_market_open():
        lines.append(f"  {minutes_to_close()} min to close")
    lines.append("")

    if params:
        lines.extend([
            "  Active Parameters (set by PM):",
            f"    Positions: {params.get('max_positions_long')} long / {params.get('max_positions_short')} short",
            f"    Leverage: {params.get('max_gross_leverage')}x gross",
            f"    Vol target: {params.get('target_annual_vol')}",
            f"    Weighting: {params.get('weighting')}",
            f"    Sector neutral: {params.get('sector_neutral')}",
        ])
    else:
        lines.append("  No active parameters. Run 'run cycle' to initialize.")

    # Show Alpaca positions
    lines.append("")
    try:
        from skills.execution.handlers import _get_current_positions, _get_account_equity
        equity = await _get_account_equity()
        positions = await _get_current_positions()
        lines.append(f"  Alpaca Account: ${equity:,.2f}")
        if positions:
            lines.append(f"  Open Positions ({len(positions)}):")
            for sym, val in sorted(positions.items(), key=lambda x: -abs(x[1])):
                pct = val / equity * 100 if equity > 0 else 0
                side = "LONG" if val > 0 else "SHORT"
                lines.append(f"    {sym:<6} ${abs(val):>10,.2f}  ({pct:>5.1f}%)  {side}")
        else:
            lines.append("  No open positions.")
    except Exception as e:
        lines.append(f"  Could not fetch Alpaca positions: {e}")

    return "\n".join(lines)


async def cmd_pnl() -> str:
    """P&L report."""
    from skills.pnl.tracker import get_pnl_tracker
    tracker = get_pnl_tracker()
    return tracker.format_report()


async def cmd_reconcile() -> str:
    """Reconcile system state vs Alpaca positions."""
    from skills.pnl.reconciliation import reconcile_positions, format_reconciliation_report
    report = await reconcile_positions()
    return format_reconciliation_report(report)


async def cmd_positions() -> str:
    """Detailed position list from Alpaca."""
    try:
        import httpx
        api_key = os.getenv("ALPACA_API_KEY", "")
        api_secret = os.getenv("ALPACA_API_SECRET", "")
        base_url = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret},
            timeout=10.0,
        ) as client:
            # Account
            acct = (await client.get("/v2/account")).json()
            equity = float(acct.get("equity", 0))
            cash = float(acct.get("cash", 0))
            buying_power = float(acct.get("buying_power", 0))

            # Positions
            resp = await client.get("/v2/positions")
            positions = resp.json()

        lines = [
            "Alpaca Positions",
            f"  Equity: ${equity:,.2f}  |  Cash: ${cash:,.2f}  |  Buying Power: ${buying_power:,.2f}",
            "",
        ]

        if positions:
            total_unrealized = 0
            lines.append(f"  {'Symbol':<7} {'Qty':>8} {'Mkt Value':>12} {'Unrealized':>12} {'Side':<6}")
            lines.append(f"  {'─'*7} {'─'*8} {'─'*12} {'─'*12} {'─'*6}")
            for p in sorted(positions, key=lambda x: -abs(float(x.get("market_value", 0)))):
                mv = float(p.get("market_value", 0))
                upl = float(p.get("unrealized_pl", 0))
                total_unrealized += upl
                side = "LONG" if float(p.get("qty", 0)) > 0 else "SHORT"
                lines.append(
                    f"  {p['symbol']:<7} {float(p.get('qty', 0)):>8.2f} "
                    f"${abs(mv):>10,.2f} {'+' if upl >= 0 else ''}{upl:>10,.2f}  {side}"
                )
            lines.append(f"\n  Total unrealized P&L: ${total_unrealized:+,.2f}")
        else:
            lines.append("  No open positions.")

        return "\n".join(lines)
    except Exception as e:
        return f"Failed to fetch positions: {e}"


async def cmd_pm_status() -> str:
    """PM agent status."""
    from skills.pm.handlers import heartbeat
    return await heartbeat()


async def cmd_session() -> str:
    """Market session status."""
    from skills.execution.handlers import heartbeat
    return await heartbeat()


async def cmd_health() -> str:
    """System health check."""
    from skills.shared.health import health_checker
    results = await health_checker.check_all()
    return health_checker.format_report(results)


def cmd_secrets() -> str:
    """Verify API key configuration."""
    from skills.shared.secrets import validate_secrets, mask_secret
    results = validate_secrets()
    lines = ["API Key Status:", ""]
    for name, status in results.items():
        val = os.getenv(name, "")
        masked = mask_secret(val) if val and "configured" in status else ""
        lines.append(f"  {name:<25} {status} {masked}")
    return "\n".join(lines)


def cmd_pending() -> str:
    """View pending approvals."""
    from skills.shared import approval_engine
    pending = approval_engine.get_pending()
    if not pending:
        return "No pending approvals."
    lines = [f"Pending Approvals ({len(pending)}):", ""]
    for rid, req in pending:
        lines.append(f"  {rid}: [{req.agent}] {req.description}")
        if req.amount:
            lines.append(f"    Amount: ${req.amount:,.2f}")
    return "\n".join(lines)


async def cmd_approve(req_id: str) -> str:
    """Approve a pending decision."""
    from skills.shared import approval_engine
    from skills.pm.handlers import apply_approved_params

    ok = approval_engine.approve(req_id)
    if not ok:
        return f"Could not approve {req_id} (not found or already resolved)"

    # Apply PM params if this was a PM decision
    req = approval_engine._get_request(req_id)
    decision_id = ""
    if req and req.details:
        decision_id = req.details.get("decision_id", "")

    if decision_id:
        result = await apply_approved_params(decision_id)
        if "error" not in result:
            params = result.get("params", {})
            return (
                f"Approved {req_id}!\n"
                f"  Parameters applied: n_long={params.get('max_positions_long')}, "
                f"n_short={params.get('max_positions_short')}, "
                f"leverage={params.get('max_gross_leverage')}"
            )
        return f"Approved {req_id} but failed to apply: {result['error']}"

    return f"Approved {req_id}."


async def cmd_deny(req_id: str) -> str:
    """Deny a pending decision."""
    from skills.shared import approval_engine
    ok = approval_engine.deny(req_id)
    return f"Denied {req_id}." if ok else f"Could not deny {req_id}."


# ─── Main Loop ──────────────────────────────────────────────────────────

COMMANDS = {
    "run cycle": cmd_run_cycle,
    "scan": cmd_scan,
    "briefing": cmd_briefing,
    "news": cmd_news,
    "report": cmd_report,
    "feedback": cmd_feedback,
    "analysts": cmd_analysts,
    "portfolio": cmd_portfolio,
    "pnl": cmd_pnl,
    "reconcile": cmd_reconcile,
    "positions": cmd_positions,
    "pm status": cmd_pm_status,
    "session": cmd_session,
    "health": cmd_health,
}

SYNC_COMMANDS = {
    "secrets": cmd_secrets,
    "pending": cmd_pending,
}


async def main():
    print(BANNER)

    # Quick startup check
    api_key = os.getenv("ALPACA_API_KEY", "")
    if api_key and api_key not in ("", "xxxxx"):
        print("  Alpaca: configured (paper trading)")
    else:
        print("  Alpaca: NOT CONFIGURED — set ALPACA_API_KEY in gateway/.env")

    fmp_key = os.getenv("FMP_API_KEY", "")
    if fmp_key and fmp_key not in ("", "xxxxx"):
        print("  FMP fundamentals: configured")
    else:
        print("  FMP fundamentals: not set (using cached data)")

    print()

    while True:
        try:
            text = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Goodbye!")
            break

        if not text:
            continue

        cmd = text.lower()

        if cmd in ("quit", "exit", "q"):
            print("  Goodbye!")
            break

        if cmd == "help":
            print(BANNER)
            continue

        # Approve/deny
        approve_match = re.match(r"^approve\s+(APR-\d+)", cmd, re.IGNORECASE)
        deny_match = re.match(r"^deny\s+(APR-\d+)", cmd, re.IGNORECASE)

        try:
            if approve_match:
                response = await cmd_approve(approve_match.group(1).upper())
            elif deny_match:
                response = await cmd_deny(deny_match.group(1).upper())
            elif cmd in SYNC_COMMANDS:
                response = SYNC_COMMANDS[cmd]()
            elif cmd in COMMANDS:
                response = await COMMANDS[cmd]()
            else:
                # Fuzzy match
                matched = None
                for key in COMMANDS:
                    if cmd in key or key in cmd:
                        matched = key
                        break
                if matched:
                    response = await COMMANDS[matched]()
                else:
                    print(f"\n  Unknown command: '{text}'. Type 'help' for commands.\n")
                    continue

            print(f"\n{response}\n")

        except Exception as e:
            print(f"\n  Error: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
