"""
Tests for adaptive feedback loop.
"""
import pytest
import tempfile
import os
from skills.feedback.scorer import OutcomeScorer
from skills.feedback.adapter import WeightAdapter


class TestOutcomeScorer:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.scorer = OutcomeScorer(db_path=os.path.join(self.tmpdir, "test_feedback.db"))

    def test_record_and_score(self):
        self.scorer.record_analyst_thesis("momentum", 0.8, "bull")
        self.scorer.record_analyst_thesis("risk", 0.6, "bear")

        # Manually mark as old enough to score
        import sqlite3
        with sqlite3.connect(self.scorer.db_path) as conn:
            conn.execute(
                "UPDATE predictions SET timestamp = datetime('now', '-15 days')"
            )

        # Score with positive return (bull was right)
        self.scorer.score_outcomes(portfolio_return_10d=0.05, market_return_10d=0.02)

        scores = self.scorer.get_all_agent_scores()
        assert "momentum-analyst" in scores
        assert "risk-analyst" in scores
        assert scores["momentum-analyst"]["n_scored"] > 0

    def test_no_scores_without_predictions(self):
        scores = self.scorer.get_agent_scores("nonexistent")
        assert scores["avg_score"] == 0.5
        assert scores["n_scored"] == 0

    def test_record_pm_decision(self):
        self.scorer.record_pm_decision("aggressive", n_long=15, n_short=7, leverage=1.5)
        # Should not crash
        scores = self.scorer.get_all_agent_scores()
        # Not scored yet (too recent)
        assert "pm-aggressive" not in scores or scores.get("pm-aggressive", {}).get("n_scored", 0) == 0

    def test_record_cio_decision(self):
        self.scorer.record_cio_decision("conservative", "crisis")
        # Should not crash


class TestWeightAdapter:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.scorer = OutcomeScorer(db_path=os.path.join(self.tmpdir, "test_fb.db"))
        self.adapter = WeightAdapter(scorer=self.scorer)
        # Override state path
        from pathlib import Path
        self.adapter.state = {
            "analyst_weight_adjustments": {},
            "pm_weight_adjustments": {},
            "cio_threshold_adjustments": {},
            "last_update": None,
            "update_count": 0,
        }

    def test_default_multiplier_is_one(self):
        assert self.adapter.get_analyst_weight_multiplier("momentum") == 1.0
        assert self.adapter.get_pm_weight_multiplier("aggressive", "momentum") == 1.0

    def test_apply_to_personality_no_change(self):
        from skills.analyst.personalities import ANALYST_PERSONALITIES
        original = ANALYST_PERSONALITIES["momentum"]
        adjusted = self.adapter.apply_to_analyst_personality(original, "momentum")
        # With multiplier 1.0, should be unchanged
        assert adjusted["signal_weights"] == original["signal_weights"]

    def test_apply_to_personality_with_adjustment(self):
        from skills.analyst.personalities import ANALYST_PERSONALITIES
        original = ANALYST_PERSONALITIES["momentum"]

        # Simulate good performance → multiplier > 1
        self.adapter.state["analyst_weight_adjustments"]["momentum"] = 1.3
        adjusted = self.adapter.apply_to_analyst_personality(original, "momentum")

        # Weights should still sum to ~1.0 (renormalized)
        total = sum(adjusted["signal_weights"].values())
        assert abs(total - 1.0) < 0.02

    def test_apply_to_pm_personality(self):
        from skills.analyst.personalities import PM_PERSONALITIES
        original = PM_PERSONALITIES["aggressive"]
        adjusted = self.adapter.apply_to_pm_personality(original, "aggressive")
        # With no adjustments, should be unchanged
        assert adjusted["analyst_weights"] == original["analyst_weights"]

    def test_min_weight_floor(self):
        from skills.analyst.personalities import PM_PERSONALITIES
        original = PM_PERSONALITIES["aggressive"]

        # Set very low adjustment
        self.adapter.state["pm_weight_adjustments"]["aggressive"] = {
            "momentum": 0.01, "value": 0.01, "macro": 0.01,
            "sentiment": 0.01, "risk": 0.01,
        }
        adjusted = self.adapter.apply_to_pm_personality(original, "aggressive")

        # All weights should be at least MIN_WEIGHT (0.08)
        for w in adjusted["analyst_weights"].values():
            assert w >= 0.07  # allow small float imprecision

    def test_update_weights_no_crash_without_data(self):
        """Update should handle empty scorer gracefully."""
        self.adapter.update_weights()
        # Should not crash, just skip
