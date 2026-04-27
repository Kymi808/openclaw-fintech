"""
Intraday scanner — scans stocks for active setups, filtered by ML model conviction.

The scanner combines two signal sources:
1. Technical signals (VWAP, ORB, momentum burst, gaps) — timing
2. ML model predictions (CrossMamba/LightGBM rankings) — direction

A signal is only valid if the technical direction AGREES with the model:
- Model ranks NVDA top 10 (bullish) + VWAP buy signal → VALID
- Model ranks MRK bottom 10 (bearish) + VWAP buy signal → DISCARDED (conflict)
- Model neutral on AAPL + ORB breakout → VALID but lower confidence

This ensures intraday trades are aligned with the model's 10-day view,
using technicals only for entry timing.
"""
from datetime import datetime, timezone

from skills.shared import get_logger
from skills.market_data import get_data_provider
from .signals import (
    IntradaySignal,
    VWAPReversion,
    OpeningRangeBreakout,
    MomentumBurst,
    GapAnalysis,
)
from .calibration import filter_correlated_signals

logger = get_logger("intraday.scanner")

# Default scan universe — liquid large-caps suitable for intraday
DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "JPM", "V", "UNH", "XOM", "MA", "HD", "CVX",
    "AVGO", "LLY", "COST", "NFLX", "AMD", "QCOM",
    "CRM", "ORCL", "GS", "BA", "CAT",
    "SPY", "QQQ", "IWM",
]

# Asymmetric thresholds: intraday longs face overnight premium headwind
# Research: most equity returns happen overnight (close→open), not intraday (open→close)
# Intraday open→close returns are flat to negative on average
# Therefore: intraday SHORTS are naturally advantaged, longs need higher conviction
MIN_CONFIDENCE_LONG = 0.65   # higher bar for intraday longs (fighting intraday drag)
MIN_CONFIDENCE_SHORT = 0.50  # lower bar for intraday shorts (aligned with intraday weakness)
MIN_RISK_REWARD_LONG = 1.5   # longs need better R:R to compensate
MIN_RISK_REWARD_SHORT = 1.0  # shorts at standard R:R

# Model conviction thresholds for filtering
MODEL_STRONG_LONG = 0.03     # longs need stronger model conviction
MODEL_STRONG_SHORT = -0.02   # shorts at standard threshold
MODEL_CONFLICT_PENALTY = 0.4


class IntradayScanner:
    """
    Scans stocks for intraday setups using TWO model layers:

    1. Intraday ML model (1-hour horizon) — primary signal for direction + magnitude
    2. Daily ML model (10-day horizon) — secondary bias (prefer aligned direction)
    3. Technical signals (VWAP, ORB, etc.) — entry timing

    A signal is valid when:
    - Intraday model predicts positive 1h return + technical BUY signal → STRONG
    - Intraday model neutral + technical signal → MODERATE
    - Intraday model predicts negative + technical BUY → DISCARDED
    - Additionally boosted if daily model agrees with direction
    """

    def __init__(self, universe: list[str] = None, model_predictions: dict[str, float] = None):
        self.universe = universe or DEFAULT_UNIVERSE
        self.daily_predictions = model_predictions or {}

    async def scan(self) -> list[IntradaySignal]:
        """Scan all symbols using intraday model + technical signals."""
        provider = get_data_provider()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Load daily predictions for secondary bias
        if not self.daily_predictions:
            self.daily_predictions = _load_cached_predictions()

        # Generate intraday model predictions
        intraday_predictions = await self._run_intraday_model(provider, today)

        # Fetch today's 1-min bars
        logger.info(f"Scanning {len(self.universe)} symbols for intraday setups...")
        try:
            bars_data = await provider.get_bars(
                symbols=self.universe,
                start=today,
                timeframe="1Min",
                feed="iex",
            )
        except Exception as e:
            logger.error(f"Failed to fetch intraday bars: {e}")
            return []

        try:
            snapshots = await provider.get_snapshots(self.universe, feed="iex")
        except Exception as e:
            logger.warning(f"Failed to fetch snapshots: {e}")
            snapshots = {}

        # Run signal checks
        all_signals = []
        for symbol in self.universe:
            raw_bars = bars_data.get(symbol, [])
            if not raw_bars or len(raw_bars) < 5:
                continue

            bars = [
                {
                    "open": b.open, "high": b.high, "low": b.low,
                    "close": b.close, "volume": b.volume,
                    "timestamp": b.timestamp,
                }
                for b in raw_bars
            ]

            snap = snapshots.get(symbol)
            prev_close = snap.prev_close if snap else 0.0

            # Primary: intraday model (1-hour horizon)
            intraday_score = intraday_predictions.get(symbol, 0.0)
            # Secondary: daily model (10-day horizon, directional bias)
            daily_score = self.daily_predictions.get(symbol, 0.0)
            # Combined model score: intraday is primary (70%), daily is bias (30%)
            model_score = intraday_score * 0.7 + daily_score * 0.3

            # Run technical checks
            signals = self._check_all_signals(symbol, bars, prev_close)

            # Filter by model agreement
            for sig in signals:
                sig = self._apply_model_filter(sig, model_score)
                if sig is not None:
                    all_signals.append(sig)

        # Asymmetric quality filter: longs need higher conviction than shorts
        # Research: intraday open→close returns are flat/negative on average
        # Shorts are naturally advantaged intraday, longs fight the drag
        filtered = []
        for s in all_signals:
            if s.side == "buy":
                if s.confidence >= MIN_CONFIDENCE_LONG and s.risk_reward >= MIN_RISK_REWARD_LONG:
                    filtered.append(s)
            else:  # sell/short
                if s.confidence >= MIN_CONFIDENCE_SHORT and s.risk_reward >= MIN_RISK_REWARD_SHORT:
                    filtered.append(s)

        # Correlation filter
        filtered = filter_correlated_signals(filtered, max_per_sector=2)
        filtered.sort(key=lambda s: s.confidence, reverse=True)

        n_discarded = len(all_signals) - len(filtered)
        logger.info(
            f"Scan: {len(all_signals)} raw → {len(filtered)} after filters "
            f"({n_discarded} discarded, {len(self.model_predictions)} model predictions available)"
        )
        return filtered

    def _check_all_signals(
        self, symbol: str, bars: list[dict], prev_close: float,
    ) -> list[IntradaySignal]:
        """Run all technical signal checks on one symbol."""
        signals = []
        for check_fn in (VWAPReversion.check, OpeningRangeBreakout.check, MomentumBurst.check):
            sig = check_fn(symbol, bars)
            if sig:
                signals.append(sig)

        if prev_close > 0:
            sig = GapAnalysis.check(symbol, bars, prev_close)
            if sig:
                signals.append(sig)

        return signals

    def _apply_model_filter(
        self, signal: IntradaySignal, model_score: float,
    ) -> IntradaySignal | None:
        """
        Filter/adjust signal based on ML model conviction.

        Rules:
        - Signal BUY + model bullish (score > 0.02) → boost confidence
        - Signal BUY + model bearish (score < -0.02) → DISCARD (conflict)
        - Signal SELL + model bearish → boost confidence
        - Signal SELL + model bullish → DISCARD
        - Model neutral → keep signal with base confidence
        - ETFs (SPY, QQQ, IWM) → exempt from model filter
        """
        # ETFs are exempt (they're used for broad market signals)
        if signal.symbol in ("SPY", "QQQ", "IWM"):
            return signal

        # No model data → keep with base confidence
        if model_score == 0.0 and signal.symbol not in self.model_predictions:
            return signal

        is_buy = signal.side == "buy"
        model_bullish = model_score > MODEL_STRONG_LONG
        model_bearish = model_score < MODEL_STRONG_SHORT
        model_neutral = not model_bullish and not model_bearish

        # Check for conflict
        if is_buy and model_bearish:
            logger.debug(
                f"Discarded: {signal.symbol} BUY ({signal.signal_type}) "
                f"conflicts with model score {model_score:+.4f}"
            )
            return None

        if not is_buy and model_bullish:
            logger.debug(
                f"Discarded: {signal.symbol} SELL ({signal.signal_type}) "
                f"conflicts with model score {model_score:+.4f}"
            )
            return None

        # Asymmetric confidence adjustment based on overnight premium research:
        # - Intraday shorts are naturally advantaged (open→close drag)
        # - Intraday longs need stronger conviction to overcome the drag
        if (is_buy and model_bullish) or (not is_buy and model_bearish):
            alignment_boost = min(0.2, abs(model_score) * 2)
            signal.confidence = min(0.95, signal.confidence + alignment_boost)
            signal.reason += f" [ML aligned: {model_score:+.3f}]"

        elif model_neutral:
            if is_buy:
                # Neutral model + intraday long = penalty (no conviction + fighting drag)
                signal.confidence *= 0.75
            else:
                # Neutral model + intraday short = mild penalty (aligned with drag)
                signal.confidence *= 0.9

        # Additional penalty for longs in high-VIX: intraday drag is worse when vol is high
        # (larger intraday swings make intraday longs riskier)
        if is_buy:
            signal.reason += " [intraday long: higher conviction required]"

        return signal

    async def _run_intraday_model(self, provider, today: str) -> dict[str, float]:
        """Run the intraday ML model to predict 1-hour returns."""
        try:
            from .model.predictor import get_intraday_predictor
            from .model.features import build_features_batch

            predictor = get_intraday_predictor()
            if not predictor.is_trained:
                logger.info("Intraday model not trained yet — using technical signals only")
                return {}

            # Fetch bars and build features
            bars_data = await provider.get_bars(
                symbols=self.universe, start=today, timeframe="1Min", feed="iex",
            )
            snapshots = await provider.get_snapshots(self.universe, feed="iex")
            spy_bars_raw = bars_data.get("SPY", [])
            spy_bars = [
                {"open": b.open, "high": b.high, "low": b.low,
                 "close": b.close, "volume": b.volume}
                for b in spy_bars_raw
            ]

            all_bars = {}
            prev_closes = {}
            for sym in self.universe:
                raw = bars_data.get(sym, [])
                if raw:
                    all_bars[sym] = [
                        {"open": b.open, "high": b.high, "low": b.low,
                         "close": b.close, "volume": b.volume}
                        for b in raw
                    ]
                snap = snapshots.get(sym)
                if snap:
                    prev_closes[sym] = snap.prev_close

            features_df = build_features_batch(all_bars, prev_closes, spy_bars)
            if features_df.empty:
                return {}

            predictions = predictor.predict(features_df)
            logger.info(f"Intraday model: {len(predictions)} predictions")
            return predictions.to_dict()

        except Exception as e:
            logger.debug(f"Intraday model unavailable: {e}")
            return {}

    async def scan_symbol(self, symbol: str) -> list[IntradaySignal]:
        """Scan a single symbol."""
        provider = get_data_provider()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        bars_data = await provider.get_bars(
            symbols=[symbol], start=today, timeframe="1Min", feed="iex",
        )

        raw_bars = bars_data.get(symbol, [])
        if not raw_bars:
            return []

        bars = [
            {"open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume}
            for b in raw_bars
        ]

        snapshots = await provider.get_snapshots([symbol], feed="iex")
        snap = snapshots.get(symbol)
        prev_close = snap.prev_close if snap else 0.0

        signals = self._check_all_signals(symbol, bars, prev_close)
        model_score = self.model_predictions.get(symbol, 0.0)

        return [
            s for s in (self._apply_model_filter(sig, model_score) for sig in signals)
            if s is not None
        ]


def _load_cached_predictions() -> dict[str, float]:
    """
    Load the most recent ML predictions from the daily cycle.

    Priority:
    1. Cached predictions file (written by daily cycle)
    2. Fresh generation via LightGBM (fallback)
    """
    from skills.shared.state import safe_load_state
    from pathlib import Path
    from datetime import datetime, timezone

    # Try cached predictions from today's daily cycle
    cache = safe_load_state(Path("./data/cached_predictions.json"), {})
    if cache.get("predictions"):
        cache_date = cache.get("date", "")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today in cache_date:
            logger.info(f"Using cached daily predictions ({len(cache['predictions'])} stocks)")
            return cache["predictions"]

    # Fallback: generate fresh predictions
    try:
        from skills.signals.bridge import generate_predictions
        predictions, info = generate_predictions("lightgbm")
        logger.info(f"Generated fresh predictions: {len(predictions)} stocks")
        return predictions
    except Exception as e:
        logger.warning(f"Could not load predictions: {e}")
        return {}
