"""
Adaptive weight updater — adjusts analyst and PM weights based on performance.

Uses exponential decay weighting:
- Agents that performed well recently get more weight
- Agents that performed poorly get less weight
- Minimum weight floors prevent any agent from being silenced
- Slow adaptation rate prevents overfitting to noise

This is the core learning mechanism. Standard at quant firms.
Called "online learning" or "adaptive allocation."
"""
from pathlib import Path
from typing import Optional

import numpy as np

from skills.shared import get_logger
from skills.shared.state import safe_load_state, safe_save_state
from .scorer import OutcomeScorer

logger = get_logger("feedback.adapter")

STATE_FILE = Path("./data/adaptive_weights.json")

# Adaptation parameters
DECAY_FACTOR = 0.95          # exponential decay — 0.95 = slow, stable adaptation
MIN_WEIGHT = 0.08            # no agent drops below 8% influence
MIN_SAMPLES = 5              # minimum scored predictions before adapting
ADAPTATION_RATE = 0.1        # how fast weights move toward performance (0.1 = slow)
SCORE_NEUTRAL = 0.5          # baseline score (neither good nor bad)


class WeightAdapter:
    """
    Adapts agent weights based on realized performance.

    Maintains two sets of adaptive weights:
    1. Analyst signal_weights — which signals each personality emphasizes
    2. PM analyst_weights — which analysts each PM trusts most

    Weights are updated daily via exponential moving average of performance scores.
    """

    def __init__(self, scorer: OutcomeScorer = None):
        self.scorer = scorer or OutcomeScorer()
        self.state = safe_load_state(STATE_FILE, {
            "analyst_weight_adjustments": {},
            "pm_weight_adjustments": {},
            "cio_threshold_adjustments": {},
            "last_update": None,
            "update_count": 0,
        })

    def save(self):
        safe_save_state(STATE_FILE, self.state)

    def get_analyst_weight_multiplier(self, analyst_name: str) -> float:
        """
        Get the adaptive weight multiplier for an analyst.

        Returns a multiplier (0.5 to 1.5) applied to the analyst's base weights.
        1.0 = no change from baseline. >1.0 = performing well, upweight.
        """
        adjustments = self.state.get("analyst_weight_adjustments", {})
        return adjustments.get(analyst_name, 1.0)

    def get_pm_weight_multiplier(self, pm_name: str, analyst_name: str) -> float:
        """
        Get the adaptive weight multiplier for a PM's trust in an analyst.
        """
        adjustments = self.state.get("pm_weight_adjustments", {})
        pm_adj = adjustments.get(pm_name, {})
        return pm_adj.get(analyst_name, 1.0)

    def update_weights(self):
        """
        Update all adaptive weights based on recent performance scores.

        Called daily (or every 10 days after prediction horizon).
        Uses exponential moving average to smooth updates.
        """
        scores = self.scorer.get_all_agent_scores(n_recent=20)

        if not scores:
            logger.debug("No scores available yet — skipping weight update")
            return

        # Update analyst multipliers
        analyst_adjustments = self.state.get("analyst_weight_adjustments", {})
        for agent_name, score_data in scores.items():
            if "-analyst" not in agent_name:
                continue
            if score_data["n_scored"] < MIN_SAMPLES:
                continue

            analyst = agent_name.replace("-analyst", "")
            avg_score = score_data["avg_score"]

            # Convert score (0-1) to multiplier (0.5-1.5)
            # Score 0.5 = neutral (1.0x), score 1.0 = excellent (1.5x), score 0.0 = poor (0.5x)
            target_multiplier = 0.5 + avg_score  # maps [0,1] → [0.5, 1.5]

            # Exponential moving average toward target
            current = analyst_adjustments.get(analyst, 1.0)
            new_multiplier = current * DECAY_FACTOR + target_multiplier * (1 - DECAY_FACTOR)

            # Clamp to prevent extreme weights
            new_multiplier = max(0.5, min(1.5, new_multiplier))
            analyst_adjustments[analyst] = round(new_multiplier, 4)

            if abs(new_multiplier - current) > 0.01:
                logger.info(
                    f"Weight update: {analyst} analyst {current:.3f} → {new_multiplier:.3f} "
                    f"(score={avg_score:.3f}, n={score_data['n_scored']})"
                )

        self.state["analyst_weight_adjustments"] = analyst_adjustments

        # Update PM trust multipliers
        pm_adjustments = self.state.get("pm_weight_adjustments", {})
        for agent_name, score_data in scores.items():
            if not agent_name.startswith("pm-"):
                continue
            if score_data["n_scored"] < MIN_SAMPLES:
                continue

            pm = agent_name.replace("pm-", "")
            avg_score = score_data["avg_score"]
            target = 0.5 + avg_score

            pm_adj = pm_adjustments.get(pm, {})
            # PM trust shifts toward analysts that contributed to good PM scores
            # For now, uniform adjustment — refine later with attribution
            for analyst in analyst_adjustments:
                current = pm_adj.get(analyst, 1.0)
                new_val = current * DECAY_FACTOR + target * (1 - DECAY_FACTOR)
                pm_adj[analyst] = round(max(0.5, min(1.5, new_val)), 4)

            pm_adjustments[pm] = pm_adj

        self.state["pm_weight_adjustments"] = pm_adjustments
        self.state["last_update"] = str(np.datetime64("now"))
        self.state["update_count"] = self.state.get("update_count", 0) + 1

        self.save()
        logger.info(f"Adaptive weights updated (iteration {self.state['update_count']})")

    def apply_to_analyst_personality(self, personality: dict, analyst_name: str) -> dict:
        """
        Apply adaptive weight multiplier to an analyst personality's signal_weights.

        Returns a modified copy of the personality dict with adjusted weights.
        The adjustment is subtle — multiplier of 1.1 means 10% more influence.
        """
        multiplier = self.get_analyst_weight_multiplier(analyst_name)
        if abs(multiplier - 1.0) < 0.01:
            return personality  # no change

        adjusted = dict(personality)
        adjusted["signal_weights"] = dict(personality["signal_weights"])

        # Scale all signal weights by the multiplier, then renormalize to sum to 1
        for key in adjusted["signal_weights"]:
            adjusted["signal_weights"][key] *= multiplier

        total = sum(adjusted["signal_weights"].values())
        if total > 0:
            for key in adjusted["signal_weights"]:
                adjusted["signal_weights"][key] = round(
                    adjusted["signal_weights"][key] / total, 4
                )

        return adjusted

    def apply_to_pm_personality(self, pm_personality: dict, pm_name: str) -> dict:
        """
        Apply adaptive multipliers to a PM's analyst_weights.
        """
        adjustments = self.state.get("pm_weight_adjustments", {}).get(pm_name, {})
        if not adjustments:
            return pm_personality

        adjusted = dict(pm_personality)
        adjusted["analyst_weights"] = dict(pm_personality["analyst_weights"])

        for analyst, base_weight in pm_personality["analyst_weights"].items():
            multiplier = adjustments.get(analyst, 1.0)
            adjusted["analyst_weights"][analyst] = base_weight * multiplier

        # Renormalize
        total = sum(adjusted["analyst_weights"].values())
        if total > 0:
            for key in adjusted["analyst_weights"]:
                adjusted["analyst_weights"][key] = round(
                    adjusted["analyst_weights"][key] / total, 4
                )

        # Enforce minimum weights
        for key in adjusted["analyst_weights"]:
            if adjusted["analyst_weights"][key] < MIN_WEIGHT:
                adjusted["analyst_weights"][key] = MIN_WEIGHT

        # Renormalize again after floor enforcement
        total = sum(adjusted["analyst_weights"].values())
        if total > 0:
            for key in adjusted["analyst_weights"]:
                adjusted["analyst_weights"][key] = round(
                    adjusted["analyst_weights"][key] / total, 4
                )

        return adjusted

    def get_status(self) -> dict:
        """Get current adaptive weight status."""
        return {
            "update_count": self.state.get("update_count", 0),
            "last_update": self.state.get("last_update"),
            "analyst_adjustments": self.state.get("analyst_weight_adjustments", {}),
            "pm_adjustments": self.state.get("pm_weight_adjustments", {}),
        }


# Singleton
_adapter: Optional[WeightAdapter] = None


def get_weight_adapter() -> WeightAdapter:
    global _adapter
    if _adapter is None:
        _adapter = WeightAdapter()
    return _adapter
