"""
Meta-labeling — secondary model that decides HOW MUCH to bet.

The primary model says: "this stock will go up" (direction)
The meta-model says: "how confident should we be in that call?" (sizing)

This produces bet sizes proportional to the probability of the
primary model being correct. High probability → full position.
Low probability → small position or skip.

Benefits:
1. Filters out false positives (primary model says buy, but meta says low confidence)
2. Sizes positions by conviction (not equal-weight everything)
3. Can incorporate features the primary model doesn't see
   (regime, time-of-day, volume regime, recent primary model accuracy)

Reference: López de Prado, "Advances in Financial Machine Learning", Ch. 3.6
"""
import numpy as np
import pandas as pd

from skills.shared import get_logger

logger = get_logger("intraday.model.meta_labeling")


def create_meta_labels(
    primary_predictions: np.ndarray,
    actual_labels: np.ndarray,
) -> np.ndarray:
    """
    Create meta-labels: was the primary model's prediction correct?

    Args:
        primary_predictions: primary model's predicted direction (+1/-1)
        actual_labels: triple barrier labels (+1/-1/0)

    Returns:
        meta_labels: 1 if primary was correct, 0 if incorrect
    """
    # Primary correct if: predicted direction matches actual outcome
    # Timeout (0) counts as incorrect for meta-labeling
    primary_direction = np.sign(primary_predictions)
    meta_labels = (primary_direction == actual_labels).astype(float)
    # Timeouts → mark as 0.5 (uncertain, let the model learn)
    meta_labels[actual_labels == 0] = 0.5
    return meta_labels


def build_meta_features(
    primary_predictions: np.ndarray,
    base_features: pd.DataFrame,
    session_progress: np.ndarray = None,
) -> pd.DataFrame:
    """
    Build features for the meta-model.

    Meta-features include:
    1. Primary model's confidence (absolute prediction magnitude)
    2. Session progress (time of day — some periods are noisier)
    3. Recent primary model accuracy (rolling hit rate)
    4. Volatility regime (high vol → more uncertainty)
    5. Volume regime (low volume → more noise)
    """
    meta = pd.DataFrame(index=base_features.index)

    # Primary confidence
    meta["primary_confidence"] = np.abs(primary_predictions)

    # Primary direction
    meta["primary_direction"] = np.sign(primary_predictions)

    # Session progress (if available)
    if session_progress is not None:
        meta["session_progress"] = session_progress
    elif "session_progress" in base_features.columns:
        meta["session_progress"] = base_features["session_progress"]

    # Volatility features from base
    for col in ["realized_vol_5m", "realized_vol_15m", "vol_ratio", "vol_1m", "vol_5m"]:
        if col in base_features.columns:
            meta[f"meta_{col}"] = base_features[col]

    # Volume features from base
    for col in ["relative_volume_5m", "relative_volume_15m", "volume_acceleration"]:
        if col in base_features.columns:
            meta[f"meta_{col}"] = base_features[col]

    # Microstructure from base
    for col in ["vpin_proxy", "roll_spread", "kyle_lambda"]:
        if col in base_features.columns:
            meta[f"meta_{col}"] = base_features[col]

    return meta


def apply_meta_sizing(
    primary_predictions: pd.Series,
    meta_probabilities: pd.Series,
    min_probability: float = 0.55,
) -> pd.Series:
    """
    Apply meta-model probabilities to size positions.

    Args:
        primary_predictions: primary model's return predictions
        meta_probabilities: meta-model's probability of primary being correct (0-1)
        min_probability: minimum probability to take the trade (default: 55%)

    Returns:
        sized_predictions: primary prediction × meta probability
        Predictions below min_probability are zeroed out (skip trade)
    """
    # Zero out low-confidence predictions
    mask = meta_probabilities >= min_probability
    sized = primary_predictions * meta_probabilities * mask.astype(float)

    n_filtered = (~mask).sum()
    if n_filtered > 0:
        logger.debug(f"Meta-labeling filtered {n_filtered} low-confidence predictions")

    return sized
