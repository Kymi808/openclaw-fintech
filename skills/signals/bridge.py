"""
Bridge between the CS_Multi_Model_Trading_System and OpenClaw.

Loads trained ML models (CrossMamba, TST, LightGBM) and generates
predictions that feed into the agent debate pipeline.
"""
import sys
import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

from skills.shared import get_logger

logger = get_logger("signals.bridge")

# Path to the CS trading system
_REPO_ROOT = Path(__file__).resolve().parents[2]
CS_SYSTEM_PATH = Path(os.getenv(
    "CS_SYSTEM_PATH",
    str(_REPO_ROOT.parent / "CS_Multi_Model_Trading_System"),
)).expanduser()

# Model file paths
MODEL_PATHS = {
    "crossmamba": CS_SYSTEM_PATH / "models" / "latest_crossmamba_model.pkl",
    "tst": CS_SYSTEM_PATH / "models" / "latest_tst_model.pkl",
    "lightgbm": CS_SYSTEM_PATH / "models" / "latest_lightgbm_model.pkl",
}

_generators: dict = {}


def _ensure_cs_path():
    """Add CS system to Python path if not already there."""
    cs_str = str(CS_SYSTEM_PATH)
    if cs_str not in sys.path:
        sys.path.insert(0, cs_str)


_alpaca_patched = False


def _patch_data_loader_for_alpaca():
    """
    Replace the CS system's yfinance-based data fetchers with our Alpaca adapter.

    This monkey-patches data_loader and sentiment_features at import time
    so that generate_signals() uses Alpaca instead of yfinance for live data.
    The CS system's backtest code is NOT modified on disk.
    """
    global _alpaca_patched
    if _alpaca_patched:
        return

    _ensure_cs_path()

    try:
        from skills.market_data.adapter import (
            fetch_price_data as alpaca_fetch_prices,
            fetch_cross_asset_data as alpaca_fetch_cross_asset,
            fetch_news_sentiment as alpaca_fetch_sentiment,
        )

        import data_loader
        import sentiment_features

        # Save originals for reference
        data_loader._original_fetch_price_data = data_loader.fetch_price_data
        data_loader._original_fetch_cross_asset_data = data_loader.fetch_cross_asset_data
        sentiment_features._original_fetch_news_sentiment = sentiment_features.fetch_news_sentiment

        # Patch with Alpaca versions
        data_loader.fetch_price_data = alpaca_fetch_prices
        data_loader.fetch_cross_asset_data = alpaca_fetch_cross_asset
        sentiment_features.fetch_news_sentiment = alpaca_fetch_sentiment

        # Auto-integrate FMP for fundamentals if API key is available
        from skills.market_data.fmp import is_fmp_configured
        if is_fmp_configured():
            from skills.market_data.fmp import fetch_fundamentals_fmp, fetch_earnings_fmp
            import asyncio

            def _fmp_fundamentals_sync(tickers, cache_dir="data"):
                """Sync wrapper for FMP fundamentals."""
                try:
                    loop = asyncio.get_running_loop()
                    import nest_asyncio
                    nest_asyncio.apply()
                    result = loop.run_until_complete(fetch_fundamentals_fmp(tickers, cache_dir))
                except RuntimeError:
                    result = asyncio.run(fetch_fundamentals_fmp(tickers, cache_dir))
                if result:
                    return result
                # Fall back to original if FMP returned empty
                return data_loader._original_fetch_fundamental_data(tickers, cache_dir)

            def _fmp_earnings_sync(tickers, cache_dir="data"):
                """Sync wrapper for FMP earnings."""
                try:
                    loop = asyncio.get_running_loop()
                    import nest_asyncio
                    nest_asyncio.apply()
                    result = loop.run_until_complete(fetch_earnings_fmp(tickers, cache_dir))
                except RuntimeError:
                    result = asyncio.run(fetch_earnings_fmp(tickers, cache_dir))
                if result:
                    return result
                return data_loader._original_fetch_earnings_dates(tickers, cache_dir)

            data_loader._original_fetch_fundamental_data = data_loader.fetch_fundamental_data
            data_loader._original_fetch_earnings_dates = data_loader.fetch_earnings_dates
            data_loader.fetch_fundamental_data = _fmp_fundamentals_sync
            data_loader.fetch_earnings_dates = _fmp_earnings_sync
            logger.info("Patched fundamentals + earnings to use FMP")

        # Enhance sentiment with LLM analysis (adds features, doesn't replace)
        _original_fetch_sentiment = sentiment_features.fetch_news_sentiment

        def _enhanced_sentiment(tickers, max_per_ticker=10, cache_dir="data"):
            """Fetch keyword sentiment, then overlay LLM sentiment features."""
            # Get base keyword sentiment
            base = _original_fetch_sentiment(tickers, max_per_ticker, cache_dir)

            # Ensure every ticker has base keys (even if fetch failed)
            for ticker in tickers:
                if ticker not in base:
                    base[ticker] = {
                        "avg_sentiment": 0.0, "max_sentiment": 0.0,
                        "min_sentiment": 0.0, "sentiment_std": 0.0,
                        "n_articles": 0, "positive_ratio": 0.0, "negative_ratio": 0.0,
                    }

            # Try to add LLM sentiment features
            try:
                import asyncio
                from skills.news.llm_sentiment import (
                    analyze_articles_batch, compute_llm_sentiment_features,
                )
                from skills.market_data import get_data_provider

                async def _run_llm():
                    provider = get_data_provider()
                    articles = await provider.get_news(symbols=tickers[:30], limit=30)
                    article_dicts = [
                        {
                            "headline": a.headline,
                            "summary": a.summary,
                            "symbols": a.symbols,
                            "source": a.source,
                            "created_at": a.created_at.isoformat(),
                        }
                        for a in articles
                    ]
                    return await analyze_articles_batch(article_dicts)

                try:
                    loop = asyncio.get_running_loop()
                    import nest_asyncio
                    nest_asyncio.apply()
                    analyses = loop.run_until_complete(_run_llm())
                except RuntimeError:
                    analyses = asyncio.run(_run_llm())

                if analyses:
                    for ticker in tickers:
                        llm_feats = compute_llm_sentiment_features(analyses, ticker)
                        if ticker in base:
                            # Add LLM features alongside existing keyword features
                            base[ticker].update(llm_feats)
                        else:
                            # Ensure base keys exist even for LLM-only tickers
                            base[ticker] = {
                                "avg_sentiment": llm_feats.get("llm_sentiment_avg", 0.0),
                                "max_sentiment": 0.0,
                                "min_sentiment": 0.0,
                                "sentiment_std": 0.0,
                                "n_articles": 0,
                                "positive_ratio": 0.0,
                                "negative_ratio": 0.0,
                            }
                            base[ticker].update(llm_feats)
                    logger.info(f"Enhanced sentiment with LLM for {len(tickers)} tickers")
            except Exception as e:
                logger.debug(f"LLM sentiment enhancement skipped: {e}")

            return base

        sentiment_features.fetch_news_sentiment = _enhanced_sentiment

        _alpaca_patched = True
        logger.info("Patched data_loader + sentiment_features to use Alpaca + LLM sentiment")
    except Exception as e:
        logger.warning(f"Could not patch for Alpaca (will use yfinance): {e}")


def _load_pickle_compat(path: str) -> dict:
    """
    Load a pickled model file with pandas version compatibility.

    Models trained with older pandas use StringDtype for feature indexes.
    Newer pandas changed StringDtype/NDArrayBacked __setstate__, breaking unpickle.

    Strategy: use a custom Unpickler that intercepts pandas Index reconstruction
    and forces object dtype instead of StringDtype.
    """

    # First try standard load
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
        return data
    except (NotImplementedError, TypeError):
        pass

    logger.info(f"Using pandas compat mode for {Path(path).name}")

    class CompatUnpickler(pickle.Unpickler):
        """Custom unpickler that converts StringDtype arrays to object arrays."""

        def find_class(self, module, name):
            # Intercept pandas string array reconstruction
            if name == "NDArrayBacked" and "pandas" in module:
                return _compat_ndarray_backed
            return super().find_class(module, name)

    def _compat_ndarray_backed(*args, **kwargs):
        """Replacement constructor for NDArrayBacked during unpickling."""
        # Return a plain numpy array wrapper that won't fail on StringDtype
        obj = object.__new__(_PlainArray)
        return obj

    class _PlainArray:
        """Minimal stand-in for NDArrayBacked that converts to plain Index."""
        def __setstate__(self, state):
            if isinstance(state, tuple) and len(state) == 2:
                dtype, values = state
                # Convert to plain string array regardless of original dtype
                self._values = np.array(values, dtype=object)
            else:
                self._values = np.array([], dtype=object)

        def __reduce__(self):
            return (np.array, (self._values,))

    with open(path, "rb") as f:
        data = CompatUnpickler(f).load()

    # Post-process: fix any _PlainArray objects left in the data
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, pd.Series):
                # Fix Series with _PlainArray index
                try:
                    if hasattr(val.index, '_values'):
                        data[key] = pd.Series(
                            val.values,
                            index=pd.Index(val.index._values),
                            name=val.name,
                        )
                except Exception:
                    pass
            elif isinstance(val, _PlainArray):
                data[key] = list(val._values)

    return data


def get_signal_generator(
    model_name: str = "crossmamba",
    n_long: int = 10,
    n_short: int = 10,
    target_vol: float = 0.10,
):
    """
    Get or create a SignalGenerator loaded with a specific model.

    Args:
        model_name: "crossmamba", "tst", or "lightgbm"
        n_long: number of long positions
        n_short: number of short positions
        target_vol: annual volatility target

    Returns:
        SignalGenerator instance with model loaded and risk initialized
    """
    _ensure_cs_path()
    _patch_data_loader_for_alpaca()  # both training (GitHub Actions) and inference use Alpaca now

    cache_key = model_name
    if cache_key in _generators:
        gen = _generators[cache_key]
        gen.cfg.portfolio.max_positions_long = n_long
        gen.cfg.portfolio.max_positions_short = n_short
        gen.cfg.risk.target_annual_vol = target_vol
        return gen

    from config import Config
    from signal_generator import SignalGenerator

    cfg = Config()
    cfg.portfolio.max_positions_long = n_long
    cfg.portfolio.max_positions_short = n_short
    cfg.risk.target_annual_vol = target_vol

    gen = SignalGenerator(cfg)

    model_path = MODEL_PATHS.get(model_name)
    if not model_path or not model_path.exists():
        raise FileNotFoundError(
            f"Model '{model_name}' not found at {model_path}. "
            f"Available: {[k for k, v in MODEL_PATHS.items() if v.exists()]}"
        )

    # Load model with pandas compat
    logger.info(f"Loading {model_name} model from {model_path}")
    model_data = _load_pickle_compat(str(model_path))

    # Initialize the correct model class based on model type
    feature_names = [str(f) for f in model_data.get("feature_names", [])]

    if model_name == "crossmamba":
        import platform
        if platform.system() == "Darwin" and platform.machine() == "arm64":
            raise RuntimeError("CrossMamba segfaults on macOS ARM — use LightGBM locally")
        from models.crossmamba_model import CrossMambaRanker
        gen.model = CrossMambaRanker(cfg.crossmamba)
        # Load state dicts into the ranker
        model_states = model_data.get("model_states", [])
        if model_states:
            gen.model.feature_names = feature_names
            gen.model.models = []
            n_features = model_data.get("n_features", len(feature_names))
            import torch
            from models.crossmamba_model import CrossMambaNet
            for state in model_states:
                net = CrossMambaNet(
                    n_features=n_features,
                    d_model=cfg.crossmamba.d_model,
                    d_state=cfg.crossmamba.d_state,
                    d_conv=cfg.crossmamba.d_conv,
                    n_layers=cfg.crossmamba.n_layers,
                    dropout=cfg.crossmamba.dropout,
                ).to(torch.device("cpu"))
                net.load_state_dict(state)
                net.eval()
                gen.model.models.append(net)
        else:
            # Models saved as full objects (not state dicts)
            gen.model.models = model_data.get("models", [])
            gen.model.feature_names = feature_names
    elif model_name == "tst":
        from models.tst_model import TSTRanker
        gen.model = TSTRanker(cfg.tst)
        model_states = model_data.get("model_states", [])
        if model_states:
            gen.model.feature_names = feature_names
            gen.model.models = []
            n_features = model_data.get("n_features", len(feature_names))
            import torch
            from models.tst_model import TimeSeriesTransformer
            for state in model_states:
                net = TimeSeriesTransformer(
                    n_features=n_features,
                    d_model=cfg.tst.d_model,
                    n_heads=cfg.tst.n_heads,
                    n_encoder_layers=cfg.tst.n_encoder_layers,
                    d_ff=cfg.tst.d_ff,
                    dropout=cfg.tst.dropout,
                ).to(torch.device("cpu"))
                net.load_state_dict(state)
                net.eval()
                gen.model.models.append(net)
        else:
            gen.model.models = model_data.get("models", [])
            gen.model.feature_names = feature_names
    else:
        # LightGBM
        from model import EnsembleRanker
        gen.model = EnsembleRanker(cfg.model)
        gen.model.models = model_data.get("models", [])
        gen.model.feature_names = feature_names

    gen.selected_features = feature_names

    fi = model_data.get("feature_importance")
    if fi is not None and isinstance(fi, pd.Series):
        gen.model.feature_importance = fi
    else:
        gen.model.feature_importance = pd.Series(dtype=float)

    gen.initialize_risk()

    _generators[cache_key] = gen
    logger.info(
        f"{model_name} loaded: {len(gen.selected_features)} features, "
        f"n_long={n_long}, n_short={n_short}"
    )

    return gen


def generate_predictions(
    model_name: str = "crossmamba",
    pm_params: dict = None,
) -> tuple[dict[str, float], dict]:
    """
    Generate stock predictions using a trained ML model.

    Runs the full CS system pipeline: data fetch → features → model → risk.

    Returns:
        (predictions, info)
        predictions: {ticker: score} for ranking
        info: diagnostic dict
    """
    pm_params = pm_params or {}

    gen = get_signal_generator(
        model_name=model_name,
        n_long=pm_params.get("max_positions_long", 10),
        n_short=pm_params.get("max_positions_short", 10),
        target_vol=pm_params.get("target_annual_vol", 0.10),
    )

    logger.info(f"Generating signals with {model_name}...")
    target_weights, info = gen.generate_signals()

    predictions = target_weights.to_dict()

    logger.info(
        f"Predictions: {info.get('n_tickers', 0)} tickers, "
        f"n_long={info.get('n_long', 0)}, n_short={info.get('n_short', 0)}, "
        f"regime={info.get('regime_score', 0):.3f}"
    )

    return predictions, info


def generate_all_predictions(pm_params: dict = None) -> dict[str, dict[str, float]]:
    """
    Generate predictions from ALL three models.

    Returns:
        {"crossmamba": {ticker: score}, "tst": {...}, "lightgbm": {...}}
    """
    all_predictions = {}
    errors = {}

    for model_name in ("crossmamba", "tst", "lightgbm"):
        model_path = MODEL_PATHS.get(model_name)
        if not model_path or not model_path.exists():
            continue

        try:
            preds, info = generate_predictions(model_name, pm_params)
            all_predictions[model_name] = preds
            logger.info(f"  {model_name}: {len(preds)} predictions")
        except Exception as e:
            logger.error(f"  {model_name} failed: {e}")
            errors[model_name] = str(e)

    if not all_predictions:
        raise RuntimeError(f"All models failed: {errors}")

    return all_predictions
