"""
Deterministic regime detection from cross-asset data.
No LLM involved — pure quantitative signal extraction.
"""

from skills.shared import get_logger
from skills.market_data import AlpacaDataProvider
from .models import RegimeData, BreadthData, SentimentData

logger = get_logger("intel.regime")

# VIX regime thresholds (standard quant convention)
VIX_LOW = 15.0
VIX_NORMAL = 20.0
VIX_ELEVATED = 30.0
# Above 30 = crisis

# Tracked sector ETFs for breadth/rotation
SECTOR_ETFS = ["XLK", "XLF", "XLV", "XLE", "XLI", "XLP", "XLY", "XLU", "XLB", "XLRE", "XLC"]
SECTOR_NAMES = {
    "XLK": "Technology", "XLF": "Financials", "XLV": "Healthcare",
    "XLE": "Energy", "XLI": "Industrials", "XLP": "Consumer Staples",
    "XLY": "Consumer Discretionary", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication",
}

# Broad equity for breadth calculation
BREADTH_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "JPM", "V", "UNH", "JNJ", "XOM", "PG", "MA", "HD", "CVX", "MRK",
    "ABBV", "PEP", "KO", "COST", "AVGO", "LLY", "WMT", "MCD", "CSCO",
    "TMO", "ACN", "ABT", "DHR", "NEE", "TXN", "UPS", "RTX",
]


def classify_vix(level: float) -> str:
    """Classify VIX level into regime category."""
    if level < VIX_LOW:
        return "low_vol"
    if level < VIX_NORMAL:
        return "normal"
    if level < VIX_ELEVATED:
        return "elevated"
    return "crisis"


async def compute_regime(provider: AlpacaDataProvider) -> RegimeData:
    """
    Compute regime indicators from cross-asset data.

    Uses 5-day bars for trend calculation, current snapshots for levels.
    """
    # Get snapshots for current levels
    macro_symbols = ["VIXY", "TLT", "SHV", "HYG", "LQD", "UUP", "GLD", "SPY"]
    snapshots = await provider.get_snapshots(macro_symbols, feed="iex")

    vixy = snapshots.get("VIXY")
    tlt = snapshots.get("TLT")
    shv = snapshots.get("SHV")
    hyg = snapshots.get("HYG")
    lqd = snapshots.get("LQD")
    uup = snapshots.get("UUP")
    gld = snapshots.get("GLD")

    # VIX level (VIXY is a proxy; scale roughly to VIX-like range)
    vix_level = vixy.price if vixy else 20.0
    vix_change = vixy.change_pct if vixy else 0.0

    # Yield curve slope: TLT (long bonds) vs SHV (short bonds)
    # When TLT rises relative to SHV, curve is flattening/inverting (bearish)
    yield_slope = 0.0
    if tlt and shv:
        tlt_ret = tlt.change_pct / 100
        shv_ret = shv.change_pct / 100
        yield_slope = shv_ret - tlt_ret  # positive = steepening (bullish)

    # Credit spread: HYG (high yield) vs LQD (investment grade)
    # When HYG drops relative to LQD, credit conditions tightening (bearish)
    credit_spread = 0.0
    if hyg and lqd:
        credit_spread = (hyg.change_pct - lqd.change_pct) / 100

    # Dollar trend (positive = dollar strengthening = headwind for equities)
    dollar_trend = (uup.change_pct / 100) if uup else 0.0

    # Gold trend (positive = risk-off / inflation fear)
    gold_trend = (gld.change_pct / 100) if gld else 0.0

    return RegimeData(
        vix_level=round(vix_level, 2),
        vix_change_1d=round(vix_change, 2),
        vix_regime=classify_vix(vix_level),
        yield_curve_slope=round(yield_slope, 4),
        credit_spread=round(credit_spread, 4),
        dollar_trend=round(dollar_trend, 4),
        gold_trend=round(gold_trend, 4),
    )


async def compute_breadth(provider: AlpacaDataProvider) -> BreadthData:
    """
    Compute market breadth from sector ETFs and top stocks.
    """
    # Sector ETF snapshots for rotation
    sector_snaps = await provider.get_snapshots(SECTOR_ETFS, feed="iex")

    sector_returns = {}
    for sym, snap in sector_snaps.items():
        sector_returns[sym] = snap.change_pct

    # Sort sectors by daily return
    sorted_sectors = sorted(sector_returns.items(), key=lambda x: x[1], reverse=True)
    leaders = [SECTOR_NAMES.get(s[0], s[0]) for s in sorted_sectors[:3]]
    laggards = [SECTOR_NAMES.get(s[0], s[0]) for s in sorted_sectors[-3:]]

    # Breadth: % of broad stocks with positive return today
    breadth_snaps = await provider.get_snapshots(BREADTH_SYMBOLS, feed="iex")
    n_positive = sum(1 for s in breadth_snaps.values() if s.change_pct > 0)
    advance_pct = n_positive / max(len(breadth_snaps), 1)

    # SPY return
    spy_snap = sector_snaps.get("SPY") or breadth_snaps.get("SPY")
    # Get SPY from a separate call if not in sector ETFs
    if not spy_snap:
        spy_snaps = await provider.get_snapshots(["SPY"], feed="iex")
        spy_snap = spy_snaps.get("SPY")

    spy_1d = spy_snap.change_pct if spy_snap else 0.0

    return BreadthData(
        advance_pct=round(advance_pct, 3),
        sector_leaders=leaders,
        sector_laggards=laggards,
        sp500_return_1d=round(spy_1d, 2),
        sp500_return_5d=0.0,  # would need 5d bars; skip for now
    )


async def compute_sentiment(provider: AlpacaDataProvider) -> SentimentData:
    """
    Compute aggregate news sentiment from Alpaca news API.
    """
    POSITIVE = {
        "beat", "exceeds", "surpass", "upgrade", "bullish", "growth", "profit",
        "gain", "rally", "surge", "boost", "strong", "record", "outperform",
        "rise", "high", "positive", "optimistic", "buy", "soar",
    }
    NEGATIVE = {
        "miss", "disappoint", "downgrade", "bearish", "loss", "decline",
        "drop", "fall", "crash", "weak", "cut", "warning", "risk",
        "low", "negative", "pessimistic", "sell", "layoff", "lawsuit", "tariff",
    }

    articles = await provider.get_news(limit=50)

    scores = []
    top_headlines = []
    for article in articles[:50]:
        text = (article.headline + " " + article.summary).lower()
        words = set(text.split())
        pos = len(words & POSITIVE)
        neg = len(words & NEGATIVE)
        total = pos + neg
        score = (pos - neg) / max(total, 1)
        scores.append(score)

        if len(top_headlines) < 5:
            top_headlines.append({
                "headline": article.headline[:100],
                "symbols": article.symbols[:5],
                "score": round(score, 2),
                "source": article.source,
            })

    import numpy as np
    agg = float(np.mean(scores)) if scores else 0.0

    return SentimentData(
        aggregate_score=round(agg, 3),
        n_articles=len(scores),
        top_headlines=top_headlines,
    )
