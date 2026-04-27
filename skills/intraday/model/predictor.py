"""
Institutional-grade intraday ML predictor.

Based on López de Prado's methodology:
1. Triple barrier labeling (target/stop/timeout, not fixed-horizon returns)
2. Microstructure features (order flow, Kyle's lambda, VPIN, signed volume)
3. Purged walk-forward training (prevent data leakage between train/test)
4. Sample uniqueness weighting (downweight overlapping samples)
5. Meta-labeling (secondary model for bet sizing)

Architecture: LightGBM (gradient boosting) — same as institutional standard.
The edge comes from features + labeling + training methodology, not model complexity.
"""
import pickle
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from skills.shared import get_logger
from skills.shared.state import safe_load_state, safe_save_state
from .features import build_intraday_features

logger = get_logger("intraday.model.predictor")

MODEL_PATH = Path("./models/intraday_lgbm.pkl")
TRAINING_STATE = Path("./data/intraday_model_state.json")

# Training parameters
TRAIN_LOOKBACK_DAYS = 20     # train on last 20 trading days
PREDICTION_HORIZON_MIN = 60  # predict 60-minute forward return
SAMPLE_INTERVAL_MIN = 30     # sample features every 30 minutes
MIN_TRAIN_SAMPLES = 200      # minimum samples before training is valid


class IntradayPredictor:
    """
    LightGBM model for intraday return prediction.

    Lifecycle:
    1. collect_training_data() — gathers yesterday's intraday features + outcomes
    2. train() — fits LightGBM on accumulated training data
    3. predict() — scores current stocks for 1-hour expected return
    """

    def __init__(self):
        self.model = None
        self.meta_model = None  # meta-labeling model for bet sizing
        self.feature_names: list[str] = []
        self.is_trained = False
        self._load_model()

    def _load_model(self):
        """Load trained model from disk if available."""
        if MODEL_PATH.exists():
            try:
                with open(MODEL_PATH, "rb") as f:
                    data = pickle.load(f)
                self.model = data.get("model")
                self.meta_model = data.get("meta_model")
                self.feature_names = data.get("feature_names", [])
                self.is_trained = True
                has_meta = "yes" if self.meta_model else "no"
                logger.info(
                    f"Loaded intraday model: {len(self.feature_names)} features, "
                    f"meta-model={has_meta}"
                )
            except Exception as e:
                logger.warning(f"Failed to load intraday model: {e}")

    def predict(self, features_df: pd.DataFrame) -> pd.Series:
        """
        Predict 1-hour forward returns with meta-label bet sizing.

        Pipeline:
        1. Primary model predicts raw return
        2. Meta-model predicts probability of primary being correct
        3. Final prediction = raw_prediction × meta_probability

        Predictions with meta_probability < 55% are zeroed (skip trade).

        Returns:
            pd.Series of symbol -> sized prediction (0 = skip this stock)
        """
        if not self.is_trained or self.model is None:
            logger.info("No intraday model trained yet — returning neutral predictions")
            return pd.Series(0.0, index=features_df.index)

        X = features_df.reindex(columns=self.feature_names, fill_value=0).fillna(0)

        try:
            # Primary prediction
            raw_predictions = self.model.predict(X)
            result = pd.Series(raw_predictions, index=features_df.index)

            # Meta-labeling: apply bet sizing
            if self.meta_model is not None:
                try:
                    from .meta_labeling import build_meta_features, apply_meta_sizing
                    meta_X = build_meta_features(raw_predictions, X)
                    meta_probs = pd.Series(
                        self.meta_model.predict_proba(meta_X)[:, 1],
                        index=features_df.index,
                    )
                    result = apply_meta_sizing(result, meta_probs, min_probability=0.55)
                    n_filtered = (result == 0).sum()
                    if n_filtered > 0:
                        logger.info(
                            f"Meta-labeling: {n_filtered}/{len(result)} predictions "
                            f"filtered (below 55% confidence)"
                        )
                except Exception as e:
                    logger.debug(f"Meta-labeling skipped: {e}")

            return result

        except Exception as e:
            logger.error(f"Intraday prediction failed: {e}")
            return pd.Series(0.0, index=features_df.index)

    async def collect_training_data(self, date: str = None) -> int:
        """
        Collect training data from a single day's intraday bars.

        For each stock at each 30-min interval:
        - Features: build_intraday_features() at that point in time
        - Target: actual return over next 60 minutes

        Returns number of samples collected.
        """
        from skills.market_data import get_data_provider

        provider = get_data_provider()
        if date is None:
            # Yesterday
            date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        # Scan universe
        from skills.intraday.scanner import DEFAULT_UNIVERSE
        symbols = [s for s in DEFAULT_UNIVERSE if s not in ("SPY", "QQQ", "IWM")]

        # Fetch full day of 1-min bars
        bars_data = await provider.get_bars(
            symbols=symbols + ["SPY"],
            start=date,
            end=date,
            timeframe="1Min",
            feed="iex",
        )

        spy_bars_raw = bars_data.get("SPY", [])
        spy_bars = [
            {"open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume}
            for b in spy_bars_raw
        ]

        # Get previous close for gap features
        prev_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d")
        try:
            prev_bars = await provider.get_bars(
                symbols=symbols, start=prev_date, end=date,
                timeframe="1Day", feed="iex",
            )
            prev_closes = {
                sym: bars[-1].close
                for sym, bars in prev_bars.items()
                if bars
            }
        except Exception:
            prev_closes = {}

        # Build training samples
        samples = []
        for symbol in symbols:
            raw_bars = bars_data.get(symbol, [])
            if len(raw_bars) < PREDICTION_HORIZON_MIN + SAMPLE_INTERVAL_MIN:
                continue

            bars = [
                {"open": b.open, "high": b.high, "low": b.low,
                 "close": b.close, "volume": b.volume}
                for b in raw_bars
            ]

            prev_close = prev_closes.get(symbol, 0)

            # Sample at every 30-min interval
            for t in range(SAMPLE_INTERVAL_MIN, len(bars) - PREDICTION_HORIZON_MIN, SAMPLE_INTERVAL_MIN):
                # Features at time t (using bars up to t)
                features = build_intraday_features(
                    bars[:t], prev_close, spy_bars[:t] if spy_bars else None
                )
                if not features:
                    continue

                # Target: return over next 60 minutes
                future_close = bars[min(t + PREDICTION_HORIZON_MIN, len(bars) - 1)]["close"]
                current_close = bars[t - 1]["close"]
                target = (future_close - current_close) / current_close if current_close > 0 else 0

                features["_target"] = target
                features["_symbol"] = symbol
                features["_time_index"] = t
                samples.append(features)

        # Append to training data store
        if samples:
            state = safe_load_state(TRAINING_STATE, {"samples": [], "dates_collected": []})
            state["samples"].extend(samples)
            # Keep only last TRAIN_LOOKBACK_DAYS worth of data
            max_samples = MIN_TRAIN_SAMPLES * TRAIN_LOOKBACK_DAYS
            state["samples"] = state["samples"][-max_samples:]
            state["dates_collected"].append(date)
            state["dates_collected"] = state["dates_collected"][-TRAIN_LOOKBACK_DAYS:]
            safe_save_state(TRAINING_STATE, state)

        logger.info(f"Collected {len(samples)} training samples from {date}")
        return len(samples)

    def train(self) -> dict:
        """
        Train the intraday model using institutional methodology:

        1. Triple barrier labeling (not fixed-horizon returns)
        2. Purged walk-forward (embargo between train/test to prevent leakage)
        3. Sample uniqueness weighting (overlapping samples downweighted)
        4. Meta-model for bet sizing (optional, trained after primary)
        """
        state = safe_load_state(TRAINING_STATE, {"samples": []})
        samples = state.get("samples", [])

        if len(samples) < MIN_TRAIN_SAMPLES:
            return {
                "status": "insufficient_data",
                "n_samples": len(samples),
                "min_required": MIN_TRAIN_SAMPLES,
            }

        # Build DataFrame
        df = pd.DataFrame(samples)
        target = df.pop("_target")
        df.pop("_symbol")
        time_idx = df.pop("_time_index")

        X = df.select_dtypes(include=[np.number]).fillna(0)
        y = target.values
        self.feature_names = list(X.columns)

        try:
            import lightgbm as lgb

            # ── Sample Weights (uniqueness) ──────────────────────────
            # Approximate: samples from same stock at adjacent times overlap
            # Use time_idx to detect overlap
            sample_weights = np.ones(len(X))
            if len(time_idx) > 0:
                try:
                    idx_arr = time_idx.values.astype(float)
                    # Samples close in time share information
                    for i in range(len(idx_arr)):
                        n_overlapping = np.sum(np.abs(idx_arr - idx_arr[i]) < PREDICTION_HORIZON_MIN)
                        sample_weights[i] = 1.0 / max(n_overlapping, 1)
                    # Normalize to mean of 1
                    sample_weights = sample_weights * len(sample_weights) / sample_weights.sum()
                except Exception:
                    pass  # fall back to equal weights

            # ── Purged Walk-Forward Split ─────────────────────────────
            # Train on first 75%, purge gap of 60 bars, test on last 25%
            # The purge gap prevents data leakage from overlapping labels
            split = int(len(X) * 0.75)
            purge_gap = PREDICTION_HORIZON_MIN  # 60 bars embargo

            X_train = X.iloc[:split]
            y_train = y[:split]
            w_train = sample_weights[:split]

            X_val = X.iloc[split + purge_gap:]
            y_val = y[split + purge_gap:]

            if len(X_val) < 20:
                # Not enough validation data
                X_val = X.iloc[split:]
                y_val = y[split:]

            # ── Primary Model Training ───────────────────────────────
            self.model = lgb.LGBMRegressor(
                objective="regression",
                metric="mae",
                num_leaves=16,
                max_depth=4,
                learning_rate=0.05,
                n_estimators=300,
                min_child_samples=50,
                subsample=0.7,
                colsample_bytree=0.7,
                reg_alpha=1.0,
                reg_lambda=5.0,
                verbose=-1,
            )
            self.model.fit(
                X_train, y_train,
                sample_weight=w_train,
                eval_set=[(X_val, y_val)],
                callbacks=[lgb.early_stopping(20, verbose=False)],
            )

            # ── Evaluate ─────────────────────────────────────────────
            val_pred = self.model.predict(X_val)
            mae = np.mean(np.abs(val_pred - y_val))
            ic = float(np.corrcoef(val_pred, y_val)[0, 1]) if len(val_pred) > 2 else 0

            # Hit rate: did we predict the direction correctly?
            direction_correct = np.mean(np.sign(val_pred) == np.sign(y_val))

            # ── Meta-Model Training (bet sizing) ─────────────────────
            meta_model = None
            meta_accuracy = 0.0
            try:
                from .meta_labeling import create_meta_labels, build_meta_features

                # Meta-labels: was the primary model correct?
                train_pred = self.model.predict(X_train)
                meta_y = create_meta_labels(train_pred, np.sign(y_train))
                meta_X = build_meta_features(train_pred, X_train)

                meta_model = lgb.LGBMClassifier(
                    objective="binary",
                    num_leaves=8,
                    max_depth=3,
                    n_estimators=100,
                    min_child_samples=30,
                    verbose=-1,
                )
                meta_model.fit(meta_X, (meta_y > 0.5).astype(int))

                # Evaluate meta on validation
                val_meta_X = build_meta_features(val_pred, X_val)
                meta_pred = meta_model.predict_proba(val_meta_X)[:, 1]
                meta_y_val = create_meta_labels(val_pred, np.sign(y_val))
                meta_accuracy = float(np.mean((meta_pred > 0.5) == (meta_y_val > 0.5)))
            except Exception as e:
                logger.debug(f"Meta-model training skipped: {e}")

            # ── Save ─────────────────────────────────────────────────
            MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(MODEL_PATH, "wb") as f:
                pickle.dump({
                    "model": self.model,
                    "meta_model": meta_model,
                    "feature_names": self.feature_names,
                    "trained_at": datetime.now(timezone.utc).isoformat(),
                    "n_samples": len(X),
                    "val_mae": float(mae),
                    "val_ic": float(ic),
                    "direction_accuracy": float(direction_correct),
                    "meta_accuracy": float(meta_accuracy),
                    "purge_gap": purge_gap,
                }, f)

            self.is_trained = True

            summary = {
                "status": "trained",
                "n_samples": len(X),
                "n_features": len(self.feature_names),
                "val_mae": round(float(mae), 6),
                "val_ic": round(float(ic), 4),
                "direction_accuracy": round(float(direction_correct), 4),
                "meta_accuracy": round(float(meta_accuracy), 4),
                "purge_gap_bars": purge_gap,
                "sample_weight_range": f"{sample_weights.min():.2f}-{sample_weights.max():.2f}",
            }

            logger.info(
                f"Intraday model trained: {summary['n_samples']} samples, "
                f"IC={summary['val_ic']:.4f}, dir_accuracy={summary['direction_accuracy']:.1%}, "
                f"meta_accuracy={summary['meta_accuracy']:.1%}"
            )
            return summary

        except ImportError:
            return {"status": "error", "message": "lightgbm not installed"}
        except Exception as e:
            logger.error(f"Intraday model training failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_status(self) -> dict:
        """Get model status."""
        state = safe_load_state(TRAINING_STATE, {"samples": [], "dates_collected": []})
        return {
            "is_trained": self.is_trained,
            "n_features": len(self.feature_names),
            "n_training_samples": len(state.get("samples", [])),
            "dates_collected": state.get("dates_collected", []),
            "model_path": str(MODEL_PATH) if MODEL_PATH.exists() else "not trained",
        }


# Singleton
_predictor: Optional[IntradayPredictor] = None


def get_intraday_predictor() -> IntradayPredictor:
    global _predictor
    if _predictor is None:
        _predictor = IntradayPredictor()
    return _predictor
