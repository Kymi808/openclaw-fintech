"""
Tests for the quant agent layer:
- Analyst scoring functions
- Personality-based conviction
- Preset interpolation
- PM resolution + CIO safety override
- Market session detection
"""
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# ── Scoring Functions ────────────────────────────────────────────────────

class TestScoringFunctions:
    def test_score_model_dispersion_high(self):
        from skills.analyst.scoring import score_model_dispersion
        # Wide spread = strong signal
        preds = {f"STOCK{i}": i * 0.01 for i in range(100)}
        score = score_model_dispersion(preds)
        assert score > 0.5

    def test_score_model_dispersion_flat(self):
        from skills.analyst.scoring import score_model_dispersion
        # All same score = no signal
        preds = {f"STOCK{i}": 0.5 for i in range(100)}
        score = score_model_dispersion(preds)
        assert score == 0.0

    def test_score_model_dispersion_empty(self):
        from skills.analyst.scoring import score_model_dispersion
        assert score_model_dispersion({}) == 0.5
        assert score_model_dispersion(None) == 0.5

    def test_score_sentiment_positive(self):
        from skills.analyst.scoring import score_sentiment
        assert score_sentiment(1.0) == 1.0
        assert score_sentiment(0.0) == 0.5
        assert score_sentiment(-1.0) == 0.0

    def test_score_breadth(self):
        from skills.analyst.scoring import score_breadth
        assert score_breadth(0.8) == 0.8
        assert score_breadth(0.0) == 0.0
        assert score_breadth(1.0) == 1.0

    def test_score_vol_regime_bull(self):
        from skills.analyst.scoring import score_vol_regime
        # Low VIX = high score for bull
        assert score_vol_regime(10.0, "bull") == 1.0
        assert score_vol_regime(40.0, "bull") == 0.0

    def test_score_vol_regime_bear(self):
        from skills.analyst.scoring import score_vol_regime
        # High VIX = high score for bear
        assert score_vol_regime(40.0, "bear") == 1.0
        assert score_vol_regime(10.0, "bear") == 0.0

    def test_score_credit_stress(self):
        from skills.analyst.scoring import score_credit_stress
        # Negative spread = tightening = stress
        assert score_credit_stress(-0.01) == 1.0
        assert score_credit_stress(0.0) == 0.5
        assert score_credit_stress(0.01) == 0.0

    def test_score_drawdown_proximity(self):
        from skills.analyst.scoring import score_drawdown_proximity
        assert score_drawdown_proximity(0.0) == 0.0  # no drawdown
        assert score_drawdown_proximity(-0.08, -0.08) == 1.0  # at limit
        assert score_drawdown_proximity(-0.04, -0.08) == 0.5  # halfway


# ── Personality Conviction ───────────────────────────────────────────────

class TestPersonalityConviction:
    def setup_method(self):
        self.predictions = {f"STOCK{i}": i * 0.01 for i in range(50)}
        self.regime = {"vix_level": 20.0, "credit_spread": 0.0}
        self.breadth = {"advance_pct": 0.5}
        self.sentiment = {"aggregate_score": 0.0}
        self.portfolio_state = {"current_drawdown": 0.0}

    def test_all_personalities_produce_conviction(self):
        from skills.analyst.scoring import personality_conviction
        from skills.analyst.personalities import ANALYST_PERSONALITIES

        for name, personality in ANALYST_PERSONALITIES.items():
            conv, reasoning = personality_conviction(
                personality, self.predictions, self.regime,
                self.breadth, self.sentiment, self.portfolio_state,
            )
            assert 0.0 <= conv <= 1.0, f"{name} conviction out of range: {conv}"
            assert len(reasoning) > 0, f"{name} has no reasoning"
            assert all("factor" in r and "score" in r for r in reasoning)

    def test_risk_analyst_high_vix(self):
        """Risk analyst should have higher conviction when VIX is elevated."""
        from skills.analyst.scoring import personality_conviction
        from skills.analyst.personalities import ANALYST_PERSONALITIES

        risk = ANALYST_PERSONALITIES["risk"]

        # Low VIX
        self.regime["vix_level"] = 12.0
        conv_low, _ = personality_conviction(
            risk, self.predictions, self.regime,
            self.breadth, self.sentiment, self.portfolio_state,
        )

        # High VIX
        self.regime["vix_level"] = 35.0
        conv_high, _ = personality_conviction(
            risk, self.predictions, self.regime,
            self.breadth, self.sentiment, self.portfolio_state,
        )

        assert conv_high > conv_low

    def test_momentum_analyst_strong_breadth(self):
        """Momentum analyst should score higher with strong breadth."""
        from skills.analyst.scoring import personality_conviction
        from skills.analyst.personalities import ANALYST_PERSONALITIES

        momentum = ANALYST_PERSONALITIES["momentum"]

        self.breadth["advance_pct"] = 0.2
        conv_weak, _ = personality_conviction(
            momentum, self.predictions, self.regime,
            self.breadth, self.sentiment, self.portfolio_state,
        )

        self.breadth["advance_pct"] = 0.8
        conv_strong, _ = personality_conviction(
            momentum, self.predictions, self.regime,
            self.breadth, self.sentiment, self.portfolio_state,
        )

        assert conv_strong > conv_weak


# ── Preset Interpolation ────────────────────────────────────────────────

class TestPresetInterpolation:
    def test_low_conviction_conservative(self):
        from skills.analyst.presets import interpolate_params_from_profile
        params = interpolate_params_from_profile(0.05, "conservative")
        # Below conviction floor → returns PARAM_FLOOR
        assert params["max_positions_long"] <= 5

    def test_high_conviction_aggressive(self):
        from skills.analyst.presets import interpolate_params_from_profile
        params = interpolate_params_from_profile(0.95, "aggressive")
        assert params["max_positions_long"] >= 15
        assert params["max_gross_leverage"] >= 1.5

    def test_moderate_profile_mid_conviction(self):
        from skills.analyst.presets import interpolate_params_from_profile
        params = interpolate_params_from_profile(0.5, "moderate")
        assert 5 <= params["max_positions_long"] <= 15
        assert 0.8 <= params["max_gross_leverage"] <= 1.4

    def test_all_profiles_return_required_keys(self):
        from skills.analyst.presets import interpolate_params_from_profile
        required = [
            "max_positions_long", "max_positions_short",
            "max_gross_leverage", "target_annual_vol",
            "weighting", "sector_neutral",
        ]
        for profile in ("conservative", "moderate", "aggressive"):
            params = interpolate_params_from_profile(0.5, profile)
            for key in required:
                assert key in params, f"{profile} missing {key}"


# ── PM Resolution ────────────────────────────────────────────────────────

class TestPMResolution:
    def _make_thesis(self, name, conviction, n_long, n_short, leverage):
        return {
            "agent": name,
            "conviction": conviction,
            "recommended_params": {
                "max_positions_long": n_long,
                "max_positions_short": n_short,
                "max_gross_leverage": leverage,
                "max_net_leverage": 0.15,
                "target_annual_vol": 0.10,
                "max_drawdown_threshold": -0.08,
                "drawdown_scale_factor": 0.5,
                "max_sector_net_pct": 0.05,
                "max_daily_turnover": 0.30,
                "weighting": "risk_parity",
                "sector_neutral": True,
            },
        }

    def test_cio_selects_conservative_in_crisis(self):
        from skills.pm.resolution import cio_decide

        proposals = {
            "aggressive": {"max_positions_long": 20, "max_positions_short": 10, "max_gross_leverage": 1.6},
            "balanced": {"max_positions_long": 12, "max_positions_short": 6, "max_gross_leverage": 1.2},
            "conservative": {"max_positions_long": 5, "max_positions_short": 3, "max_gross_leverage": 0.8},
        }
        briefing = {"regime": {"vix_level": 35.0, "vix_regime": "crisis"}, "breadth": {"advance_pct": 0.3}}

        selected, params, rationale = cio_decide(proposals, {}, briefing)
        assert selected == "conservative"

    def test_cio_selects_aggressive_in_low_vol(self):
        from skills.pm.resolution import cio_decide

        proposals = {
            "aggressive": {"max_positions_long": 20},
            "balanced": {"max_positions_long": 12},
            "conservative": {"max_positions_long": 5},
        }
        briefing = {"regime": {"vix_level": 14.0, "vix_regime": "low_vol"}, "breadth": {"advance_pct": 0.7}}

        selected, params, rationale = cio_decide(proposals, {}, briefing)
        assert selected == "aggressive"

    def test_cio_safety_override(self):
        from skills.pm.resolution import cio_decide, SAFETY_OVERRIDE_VIX

        proposals = {
            "aggressive": {"max_positions_long": 20},
            "balanced": {"max_positions_long": 12},
            "conservative": {"max_positions_long": 5},
        }
        briefing = {"regime": {"vix_level": 40.0, "vix_regime": "crisis"}, "breadth": {"advance_pct": 0.5}}

        selected, params, rationale = cio_decide(proposals, {}, briefing)
        assert selected == "conservative"
        assert "SAFETY OVERRIDE" in rationale

    def test_resolve_theses_multi_analyst(self):
        from skills.pm.resolution import resolve_theses

        theses = {
            "momentum": self._make_thesis("momentum", 0.7, 15, 7, 1.5),
            "value": self._make_thesis("value", 0.5, 10, 5, 1.2),
            "macro": self._make_thesis("macro", 0.4, 8, 4, 1.0),
            "sentiment": self._make_thesis("sentiment", 0.6, 12, 6, 1.3),
            "risk": self._make_thesis("risk", 0.3, 5, 3, 0.8),
        }
        briefing = {"regime": {"vix_level": 20.0, "vix_regime": "normal"}, "breadth": {"advance_pct": 0.55}}

        result = resolve_theses(theses, {}, briefing)
        assert "final_params" in result
        assert "resolution" in result
        assert result["resolution"]["selected_pm"] in ("aggressive", "balanced", "conservative")
        assert result["final_params"]["max_positions_long"] > 0

    def test_first_run_requires_approval(self):
        from skills.pm.resolution import resolve_theses

        theses = {
            "momentum": self._make_thesis("momentum", 0.5, 10, 5, 1.2),
        }
        result = resolve_theses(theses, current_params={}, mode="daily")
        assert result["requires_approval"] is True


# ── Market Session Detection ─────────────────────────────────────────────

class TestMarketSession:
    def test_market_open(self):
        from skills.execution.session import get_session, MarketSession
        # 10:00 AM ET on a Wednesday
        dt = datetime(2026, 4, 1, 10, 0, tzinfo=ET)
        assert get_session(dt) == MarketSession.OPEN

    def test_market_closed_weekend(self):
        from skills.execution.session import get_session, MarketSession
        # Saturday
        dt = datetime(2026, 4, 4, 12, 0, tzinfo=ET)
        assert get_session(dt) == MarketSession.CLOSED

    def test_pre_market(self):
        from skills.execution.session import get_session, MarketSession
        dt = datetime(2026, 4, 1, 7, 0, tzinfo=ET)
        assert get_session(dt) == MarketSession.PRE_MARKET

    def test_closing_window(self):
        from skills.execution.session import get_session, MarketSession
        dt = datetime(2026, 4, 1, 15, 50, tzinfo=ET)
        assert get_session(dt) == MarketSession.CLOSING

    def test_after_hours(self):
        from skills.execution.session import get_session, MarketSession
        dt = datetime(2026, 4, 1, 17, 0, tzinfo=ET)
        assert get_session(dt) == MarketSession.AFTER_HOURS

    def test_is_market_open(self):
        from skills.execution.session import is_market_open
        dt_open = datetime(2026, 4, 1, 11, 0, tzinfo=ET)
        dt_closed = datetime(2026, 4, 4, 11, 0, tzinfo=ET)  # Saturday
        assert is_market_open(dt_open) is True
        assert is_market_open(dt_closed) is False

    def test_minutes_to_close(self):
        from skills.execution.session import minutes_to_close
        dt = datetime(2026, 4, 1, 15, 0, tzinfo=ET)  # 3:00 PM
        assert minutes_to_close(dt) == 60

    def test_should_close_intraday(self):
        from skills.execution.session import should_close_intraday
        dt_early = datetime(2026, 4, 1, 14, 0, tzinfo=ET)
        dt_late = datetime(2026, 4, 1, 15, 50, tzinfo=ET)
        assert should_close_intraday(dt_early) is False
        assert should_close_intraday(dt_late) is True


# ── PDT Compliance ───────────────────────────────────────────────────────

class TestPDTCompliance:
    def test_above_threshold(self):
        from skills.execution.session import check_pdt_compliance
        ok, reason = check_pdt_compliance(30_000, 10)
        assert ok is True

    def test_below_threshold_within_limit(self):
        from skills.execution.session import check_pdt_compliance
        ok, reason = check_pdt_compliance(20_000, 2)
        assert ok is True
        assert "1 day trades remaining" in reason

    def test_below_threshold_at_limit(self):
        from skills.execution.session import check_pdt_compliance
        ok, reason = check_pdt_compliance(20_000, 3)
        assert ok is False
        assert "PDT limit" in reason


# ── VIX Regime Classification ────────────────────────────────────────────

class TestRegimeClassification:
    def test_classify_vix(self):
        from skills.intel.regime import classify_vix
        assert classify_vix(12.0) == "low_vol"
        assert classify_vix(18.0) == "normal"
        assert classify_vix(25.0) == "elevated"
        assert classify_vix(35.0) == "crisis"


# ── Model Blending ───────────────────────────────────────────────────────

class TestModelBlending:
    def test_blend_model_predictions(self):
        from skills.analyst.scoring import blend_model_predictions

        model_preds = {
            "crossmamba": {"AAPL": 0.10, "TSLA": -0.05},
            "tst": {"AAPL": 0.08, "TSLA": -0.03},
            "lightgbm": {"AAPL": 0.12, "TSLA": -0.08},
        }
        weights = {"crossmamba": 0.5, "tst": 0.3, "lightgbm": 0.2}

        blended = blend_model_predictions(model_preds, weights)
        assert "AAPL" in blended
        assert "TSLA" in blended
        # CrossMamba-weighted average
        expected_aapl = (0.10 * 0.5 + 0.08 * 0.3 + 0.12 * 0.2) / 1.0
        assert abs(blended["AAPL"] - expected_aapl) < 0.001

    def test_blend_missing_model(self):
        from skills.analyst.scoring import blend_model_predictions

        model_preds = {
            "crossmamba": {"AAPL": 0.10},
            # TST missing
        }
        weights = {"crossmamba": 0.5, "tst": 0.3, "lightgbm": 0.2}

        blended = blend_model_predictions(model_preds, weights)
        assert "AAPL" in blended


# ── Order Splitter ───────────────────────────────────────────────────────

class TestOrderSplitter:
    def test_small_order_no_split(self):
        from skills.execution.order_splitter import should_split, create_slices
        assert should_split(5000) is False
        slices = create_slices("AAPL", "buy", 5000)
        assert len(slices) == 1

    def test_large_order_splits(self):
        from skills.execution.order_splitter import should_split, create_slices
        assert should_split(15000) is True
        slices = create_slices("AAPL", "buy", 15000)
        assert len(slices) == 5
        assert sum(s.notional for s in slices) == 15000

    def test_slice_attributes(self):
        from skills.execution.order_splitter import create_slices
        slices = create_slices("TSLA", "sell", 20000)
        for i, s in enumerate(slices):
            assert s.symbol == "TSLA"
            assert s.side == "sell"
            assert s.slice_index == i
            assert s.total_slices == 5


# ── Personality Config Validation ────────────────────────────────────────

class TestPersonalityConfig:
    def test_all_analyst_signal_weights_sum_to_one(self):
        from skills.analyst.personalities import ANALYST_PERSONALITIES
        for name, p in ANALYST_PERSONALITIES.items():
            total = sum(p["signal_weights"].values())
            assert abs(total - 1.0) < 0.01, f"{name} signal weights sum to {total}"

    def test_all_analyst_model_weights_sum_to_one(self):
        from skills.analyst.personalities import ANALYST_PERSONALITIES
        for name, p in ANALYST_PERSONALITIES.items():
            total = sum(p["model_weights"].values())
            assert abs(total - 1.0) < 0.01, f"{name} model weights sum to {total}"

    def test_all_pm_analyst_weights_sum_to_one(self):
        from skills.analyst.personalities import PM_PERSONALITIES
        for name, p in PM_PERSONALITIES.items():
            total = sum(p["analyst_weights"].values())
            assert abs(total - 1.0) < 0.01, f"{name} analyst weights sum to {total}"

    def test_crossmamba_is_primary_for_all_analysts(self):
        from skills.analyst.personalities import ANALYST_PERSONALITIES
        for name, p in ANALYST_PERSONALITIES.items():
            mw = p["model_weights"]
            assert mw["crossmamba"] >= mw["lightgbm"], \
                f"{name}: CrossMamba ({mw['crossmamba']}) should be >= LightGBM ({mw['lightgbm']})"
