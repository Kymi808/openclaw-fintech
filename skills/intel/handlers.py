"""
Market Intelligence Agent handlers.

Gathers macro regime data, market breadth, and news sentiment
into a structured MarketBriefing consumed by analyst agents.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from skills.shared import get_logger, audit_log
from skills.market_data import get_data_provider
from .models import MarketBriefing
from .regime import compute_regime, compute_breadth, compute_sentiment

# News integration
_news_digest_cache = None

logger = get_logger("intel.handlers")

STATE_FILE = Path("./workspaces/intel-agent/state.json")
ET = ZoneInfo("America/New_York")


def _load_state() -> dict:
    from skills.shared.state import safe_load_state
    return safe_load_state(STATE_FILE, {"briefing_history": [], "last_run": None})


def _save_state(state: dict) -> None:
    from skills.shared.state import safe_save_state
    safe_save_state(STATE_FILE, state)


def _get_session() -> str:
    """Determine current market session based on ET time."""
    now = datetime.now(ET)
    hour, minute = now.hour, now.minute
    t = hour * 60 + minute

    # Weekend
    if now.weekday() >= 5:
        return "closed"

    if t < 4 * 60:       # before 4:00 AM ET
        return "closed"
    if t < 9 * 60 + 30:  # 4:00 - 9:30 AM ET
        return "pre_market"
    if t < 15 * 60 + 45: # 9:30 AM - 3:45 PM ET
        return "open"
    if t < 16 * 60:      # 3:45 - 4:00 PM ET
        return "closing"
    if t < 20 * 60:      # 4:00 - 8:00 PM ET
        return "after_hours"
    return "closed"


async def gather_briefing() -> dict:
    """
    Produce a complete MarketBriefing.

    This is the primary entry point called by the orchestrator.
    Returns the briefing as a dict for easy serialization.
    """
    provider = get_data_provider()

    # Compute all three components
    regime = await compute_regime(provider)
    breadth = await compute_breadth(provider)
    sentiment = await compute_sentiment(provider)

    # Build macro summary (human-readable, no LLM needed for this)
    summary_parts = []
    summary_parts.append(f"VIX at {regime.vix_level:.1f} ({regime.vix_regime})")

    if regime.credit_spread < -0.005:
        summary_parts.append("credit conditions tightening")
    elif regime.credit_spread > 0.005:
        summary_parts.append("credit conditions easing")

    if breadth.advance_pct > 0.65:
        summary_parts.append(f"broad strength ({breadth.advance_pct:.0%} advancing)")
    elif breadth.advance_pct < 0.35:
        summary_parts.append(f"broad weakness ({breadth.advance_pct:.0%} advancing)")

    if sentiment.aggregate_score > 0.1:
        summary_parts.append("news sentiment positive")
    elif sentiment.aggregate_score < -0.1:
        summary_parts.append("news sentiment negative")

    # Integrate news gathering agents
    news_data = {}
    try:
        from skills.news.aggregator import aggregate_all_news
        digest = await aggregate_all_news()
        news_data = digest.to_dict()
        # Enhance sentiment with news digest
        if digest.overall_sentiment != 0:
            sentiment.aggregate_score = (sentiment.aggregate_score + digest.overall_sentiment) / 2
    except Exception as e:
        logger.debug(f"News gathering skipped: {e}")

    # LLM-powered sentiment analysis (Claude Haiku, ~$0.02/day)
    try:
        from skills.news.llm_sentiment import analyze_articles_batch

        llm_provider = get_data_provider()
        # Only analyze articles for stocks likely in portfolio (top 30)
        from skills.intel.regime import BREADTH_SYMBOLS
        articles = await llm_provider.get_news(symbols=BREADTH_SYMBOLS[:30], limit=30)

        article_dicts = [
            {
                "headline": a.headline,
                "summary": a.summary,
                "symbols": a.symbols,
                "source": a.source,
                "created_at": a.created_at.isoformat() if hasattr(a.created_at, 'isoformat') else str(a.created_at),
            }
            for a in articles
        ]

        market_context = f"VIX: {regime.vix_level} ({regime.vix_regime}), breadth: {breadth.advance_pct:.0%}"
        llm_analyses = await analyze_articles_batch(article_dicts, context=market_context)

        if llm_analyses:
            # Override basic sentiment with LLM-weighted sentiment
            llm_sentiments = [
                a["sentiment"] * a["decay_weight"] * a["magnitude"]
                for a in llm_analyses
            ]
            total_weight = sum(a["decay_weight"] * a["magnitude"] for a in llm_analyses)
            if total_weight > 0:
                llm_avg = sum(llm_sentiments) / total_weight
                # Blend: 70% LLM (context-aware), 30% keyword-based (backup)
                sentiment.aggregate_score = llm_avg * 0.7 + sentiment.aggregate_score * 0.3
                logger.info(f"LLM sentiment: {llm_avg:+.3f} ({len(llm_analyses)} articles analyzed)")

            news_data["llm_analyses"] = [a for a in llm_analyses[:10]]
    except Exception as e:
        logger.debug(f"LLM sentiment skipped: {e}")

    briefing = MarketBriefing(
        session=_get_session(),
        regime=regime,
        breadth=breadth,
        sentiment=sentiment,
        macro_summary=". ".join(summary_parts) + ".",
    )

    # Persist to state
    state = _load_state()
    state["last_briefing"] = briefing.to_dict()
    state["briefing_history"].append(briefing.to_dict())
    state["briefing_history"] = state["briefing_history"][-20:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    audit_log("intel-agent", "briefing_generated", {
        "session": briefing.session,
        "vix_regime": regime.vix_regime,
        "advance_pct": breadth.advance_pct,
        "sentiment": sentiment.aggregate_score,
    })

    logger.info(f"Briefing: {briefing.macro_summary}")
    return briefing.to_dict()


async def pre_market_briefing() -> str:
    """Generate a pre-market briefing for human consumption."""
    briefing = await gather_briefing()
    regime = briefing["regime"]
    breadth = briefing["breadth"]
    sentiment = briefing["sentiment"]

    lines = [
        f"Pre-Market Briefing — {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}",
        "",
        f"Regime: {regime['vix_regime'].upper()} (VIX proxy: {regime['vix_level']:.1f}, "
        f"{regime['vix_change_1d']:+.1f}%)",
        f"Yield curve: {'steepening' if regime['yield_curve_slope'] > 0 else 'flattening'} "
        f"({regime['yield_curve_slope']:+.4f})",
        f"Credit: {'easing' if regime['credit_spread'] > 0 else 'tightening'} "
        f"({regime['credit_spread']:+.4f})",
        f"Dollar: {regime['dollar_trend']:+.4f} | Gold: {regime['gold_trend']:+.4f}",
        "",
        f"Breadth: {breadth['advance_pct']:.0%} advancing",
        f"Leaders: {', '.join(breadth['sector_leaders'])}",
        f"Laggards: {', '.join(breadth['sector_laggards'])}",
        "",
        f"Sentiment: {sentiment['aggregate_score']:+.3f} ({sentiment['n_articles']} articles)",
    ]

    for h in sentiment.get("top_headlines", [])[:3]:
        lines.append(f"  [{h['source']}] {h['headline']}")

    lines.append("")
    lines.append(briefing["macro_summary"])

    return "\n".join(lines)


async def heartbeat() -> str:
    """Periodic intelligence refresh."""
    return await pre_market_briefing()
