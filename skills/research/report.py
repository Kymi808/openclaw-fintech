"""
Institutional-grade weekly research report.

Generates a senior analyst-level report by:
1. Gathering all system data (P&L, positions, signals, news, regime)
2. Structuring it into sections
3. Using Claude to synthesize into professional prose

This is the ONE place where we use LLM for analysis output.
All trading decisions are still deterministic — this report is for
human consumption only (like a Goldman Sachs weekly research note).

Report sections:
- Executive Summary
- Market Regime & Macro Environment
- Portfolio Performance Attribution
- Top/Bottom Performers Analysis
- Risk Assessment & Exposure
- News & Event Impact
- Sector Rotation Analysis
- Forward Outlook & Key Events
"""
import os
from datetime import datetime, timezone

import httpx

from skills.shared import get_logger, audit_log

logger = get_logger("research.report")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"


async def generate_weekly_report() -> str:
    """
    Generate a full institutional weekly research report.

    Gathers all system data, structures it, and uses Claude
    to produce professional analyst-level prose.
    """
    logger.info("Generating weekly research report...")

    # 1. Gather all data
    data = await _gather_report_data()

    # 2. Build the prompt with structured data
    prompt = _build_report_prompt(data)

    # 3. Generate via Claude
    report = await _generate_with_claude(prompt)

    if not report:
        # Fallback: structured report without LLM
        report = _format_structured_report(data)

    audit_log("research", "weekly_report_generated", {
        "sections": report.count("##"),
        "length": len(report),
    })

    logger.info(f"Weekly report generated: {len(report)} chars")
    return report


async def _gather_report_data() -> dict:
    """Gather all system data needed for the report."""
    data = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "period": "Weekly",
    }

    # P&L data
    try:
        from skills.pnl.tracker import get_pnl_tracker
        tracker = get_pnl_tracker()
        data["pnl_stats"] = tracker.get_current_stats()
        data["equity_curve"] = tracker.get_equity_curve()[-5:]  # last 5 days
        data["daily_returns"] = tracker.get_daily_returns(5)
    except Exception as e:
        data["pnl_stats"] = {"status": "unavailable", "error": str(e)}

    # Current positions from Alpaca
    try:
        from skills.execution.handlers import _get_current_positions, _get_account_equity
        data["equity"] = await _get_account_equity()
        data["positions"] = await _get_current_positions()
    except Exception:
        data["positions"] = {}
        data["equity"] = 0

    # Market regime
    try:
        from skills.intel.handlers import gather_briefing
        data["briefing"] = await gather_briefing()
    except Exception as e:
        data["briefing"] = {"error": str(e)}

    # News digest
    try:
        from skills.news.aggregator import aggregate_all_news
        digest = await aggregate_all_news()
        data["news_digest"] = digest.to_dict()
    except Exception as e:
        data["news_digest"] = {"error": str(e)}

    # PM parameters
    try:
        from skills.pm.handlers import get_current_params
        data["pm_params"] = await get_current_params()
    except Exception:
        data["pm_params"] = {}

    # Last analyst theses
    try:
        from skills.shared.state import safe_load_state
        from pathlib import Path
        from skills.analyst.personalities import ANALYST_PERSONALITIES
        data["analyst_theses"] = {}
        for name in ANALYST_PERSONALITIES:
            state = safe_load_state(
                Path(f"./workspaces/{name}-analyst/state.json"),
                {"thesis_history": []},
            )
            if state.get("thesis_history"):
                last = state["thesis_history"][-1]
                data["analyst_theses"][name] = {
                    "conviction": last.get("conviction", 0),
                    "risk_flags": last.get("risk_flags", []),
                }
    except Exception:
        data["analyst_theses"] = {}

    return data


def _build_report_prompt(data: dict) -> str:
    """Build the Claude prompt with all structured data."""
    positions = data.get("positions", {})
    equity = data.get("equity", 0)
    briefing = data.get("briefing", {})
    pnl = data.get("pnl_stats", {})
    news = data.get("news_digest", {})
    params = data.get("pm_params", {})
    theses = data.get("analyst_theses", {})

    # Format positions
    pos_lines = []
    if positions:
        sorted_pos = sorted(positions.items(), key=lambda x: -abs(x[1]))
        for sym, val in sorted_pos[:20]:
            pct = val / equity * 100 if equity > 0 else 0
            side = "LONG" if val > 0 else "SHORT"
            pos_lines.append(f"  {sym}: ${abs(val):,.0f} ({pct:.1f}%) {side}")

    # Format news
    news_lines = []
    for sig in news.get("top_macro", [])[:3]:
        news_lines.append(f"  [MACRO] {sig.get('headline', '')}")
    for sig in news.get("top_sector", [])[:3]:
        news_lines.append(f"  [SECTOR] {sig.get('headline', '')}")
    for sig in news.get("top_company", [])[:3]:
        news_lines.append(f"  [COMPANY] {sig.get('headline', '')}")

    # Format analyst convictions
    thesis_lines = []
    for name, t in sorted(theses.items(), key=lambda x: -x[1].get("conviction", 0)):
        flags = ", ".join(t.get("risk_flags", []))
        thesis_lines.append(f"  {name}: conviction={t['conviction']:.3f}" + (f" [{flags}]" if flags else ""))

    prompt = f"""You are a senior equity research analyst at a top-tier investment bank.
Generate a comprehensive weekly research report based on the following portfolio and market data.

Write in the style of a Goldman Sachs or Morgan Stanley weekly strategy note.
Be specific, data-driven, and actionable. No fluff.

PORTFOLIO DATA:
  Account equity: ${equity:,.2f}
  Positions: {len(positions)} ({sum(1 for v in positions.values() if v > 0)} long, {sum(1 for v in positions.values() if v < 0)} short)
  PM parameters: n_long={params.get('max_positions_long', 'N/A')}, n_short={params.get('max_positions_short', 'N/A')}, leverage={params.get('max_gross_leverage', 'N/A')}

Top positions:
{chr(10).join(pos_lines[:15]) if pos_lines else '  No positions'}

P&L:
  Daily return: {pnl.get('daily_return', 'N/A')}
  Cumulative: {pnl.get('cumulative_return', 'N/A')}
  Sharpe (30d): {pnl.get('sharpe_30d', 'N/A')}
  Max drawdown: {pnl.get('max_drawdown', 'N/A')}

MARKET REGIME:
  {briefing.get('macro_summary', 'N/A')}
  VIX regime: {briefing.get('regime', {}).get('vix_regime', 'N/A')}
  VIX level: {briefing.get('regime', {}).get('vix_level', 'N/A')}
  Breadth: {briefing.get('breadth', {}).get('advance_pct', 'N/A')}
  Sector leaders: {', '.join(briefing.get('breadth', {}).get('sector_leaders', []))}
  Sector laggards: {', '.join(briefing.get('breadth', {}).get('sector_laggards', []))}

ANALYST CONVICTIONS:
{chr(10).join(thesis_lines) if thesis_lines else '  No analyst data'}

KEY NEWS:
{chr(10).join(news_lines) if news_lines else '  No news data'}
  News sentiment: {news.get('overall_sentiment', 'N/A')}
  Key themes: {', '.join(news.get('key_themes', []))}

Generate the report with these sections:
1. Executive Summary (3-4 sentences: key takeaway, positioning, outlook)
2. Market Regime & Macro Environment (regime classification, rates, credit, volatility analysis)
3. Portfolio Performance Attribution (what drove returns, which positions contributed/detracted)
4. Position Analysis (top convictions, sector exposure, concentration risk)
5. Risk Assessment (drawdown risk, tail risk factors, correlation concerns)
6. News & Catalyst Monitor (upcoming events, earnings, macro releases that could impact)
7. Forward Outlook (1-week view: expected regime, recommended positioning adjustments)

Format as markdown. Be concise but thorough. Every claim should reference the data above."""

    return prompt


async def _generate_with_claude(prompt: str) -> str:
    """Generate report text using Claude API."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key in ("", "sk-ant-xxx"):
        logger.info("Anthropic API key not available, using structured fallback")
        return ""

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 4000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["content"][0]["text"]
    except Exception as e:
        logger.error(f"Claude report generation failed: {e}")
        return ""


def _format_structured_report(data: dict) -> str:
    """Fallback: structured report without LLM."""
    positions = data.get("positions", {})
    equity = data.get("equity", 0)
    briefing = data.get("briefing", {})
    pnl = data.get("pnl_stats", {})
    params = data.get("pm_params", {})

    regime = briefing.get("regime", {})
    breadth = briefing.get("breadth", {})

    n_long = sum(1 for v in positions.values() if v > 0)
    n_short = sum(1 for v in positions.values() if v < 0)
    long_val = sum(v for v in positions.values() if v > 0)
    short_val = sum(abs(v) for v in positions.values() if v < 0)
    gross = (long_val + short_val) / equity if equity > 0 else 0
    net = (long_val - short_val) / equity if equity > 0 else 0

    lines = [
        f"# Weekly Research Report — {data['date']}",
        "",
        "## Executive Summary",
        f"Portfolio equity at ${equity:,.2f} with {n_long} long and {n_short} short positions.",
        f"Gross exposure: {gross:.0%}, net exposure: {net:.0%}.",
        f"Market regime: {regime.get('vix_regime', 'N/A')} (VIX: {regime.get('vix_level', 'N/A')}).",
        f"PM targeting {params.get('max_positions_long', 'N/A')} longs / "
        f"{params.get('max_positions_short', 'N/A')} shorts at "
        f"{params.get('max_gross_leverage', 'N/A')}x leverage.",
        "",
        "## Market Regime",
        f"- VIX: {regime.get('vix_level', 'N/A')} ({regime.get('vix_regime', 'N/A')})",
        f"- Credit: {regime.get('credit_spread', 'N/A')}",
        f"- Dollar: {regime.get('dollar_trend', 'N/A')}",
        f"- Breadth: {breadth.get('advance_pct', 'N/A')}",
        f"- Leaders: {', '.join(breadth.get('sector_leaders', []))}",
        f"- Laggards: {', '.join(breadth.get('sector_laggards', []))}",
        "",
        "## P&L Summary",
        f"- Daily: {pnl.get('daily_return', 'N/A')}",
        f"- Cumulative: {pnl.get('cumulative_return', 'N/A')}",
        f"- Sharpe (30d): {pnl.get('sharpe_30d', 'N/A')}",
        f"- Max DD: {pnl.get('max_drawdown', 'N/A')}",
        "",
        "## Top Positions",
    ]

    for sym, val in sorted(positions.items(), key=lambda x: -abs(x[1]))[:10]:
        pct = val / equity * 100 if equity > 0 else 0
        side = "LONG" if val > 0 else "SHORT"
        lines.append(f"- {sym}: ${abs(val):,.0f} ({pct:.1f}%) {side}")

    lines.extend([
        "",
        "---",
        "*Report generated by OpenClaw Research Module. "
        "Configure ANTHROPIC_API_KEY for AI-synthesized analysis.*",
    ])

    return "\n".join(lines)
