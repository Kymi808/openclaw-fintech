"""
Analyst and PM personality definitions.

Each personality is a weight vector that controls:
1. Which market signals matter most (signal_weights)
2. Which ML model predictions to trust most (model_weights)
3. Risk appetite for parameter recommendations (risk_profile)

This replaces the simple bull/bear split with diverse investment perspectives.
"""

# ─── Analyst Personalities ───────────────────────────────────────────────

ANALYST_PERSONALITIES = {
    "momentum": {
        "name": "Momentum Analyst",
        "description": "Trend-following. Favors strong price action and broad participation.",
        "signal_weights": {
            "model_dispersion": 0.35,
            "breadth": 0.30,
            "sentiment": 0.15,
            "vol_regime": 0.10,
            "credit_stress": 0.05,
            "drawdown_proximity": 0.05,
        },
        "model_weights": {
            "crossmamba": 0.60,  # best Sharpe + lowest drawdown, captures temporal patterns
            "tst": 0.25,
            "lightgbm": 0.15,
        },
        "risk_profile": "aggressive",
        "bias": "bull",
    },

    "value": {
        "name": "Value Analyst",
        "description": "Mean-reversion. Looks for overextension and cheapness.",
        "signal_weights": {
            "model_dispersion": 0.25,
            "breadth": 0.10,
            "sentiment": 0.10,
            "vol_regime": 0.15,
            "credit_stress": 0.20,
            "drawdown_proximity": 0.20,
        },
        "model_weights": {
            "crossmamba": 0.45,  # best risk-adjusted returns even in neutral mode
            "lightgbm": 0.35,   # good with tabular fundamental features
            "tst": 0.20,
        },
        "risk_profile": "moderate",
        "bias": "neutral",
    },

    "macro": {
        "name": "Macro Analyst",
        "description": "Top-down. Regime and cross-asset signals drive positioning.",
        "signal_weights": {
            "model_dispersion": 0.10,
            "breadth": 0.15,
            "sentiment": 0.10,
            "vol_regime": 0.30,
            "credit_stress": 0.25,
            "drawdown_proximity": 0.10,
        },
        "model_weights": {
            "crossmamba": 0.45,  # O(n) linear complexity handles long-range macro dependencies
            "tst": 0.35,        # attention good for cross-asset relationships
            "lightgbm": 0.20,
        },
        "risk_profile": "moderate",
        "bias": "neutral",
    },

    "sentiment": {
        "name": "Sentiment Analyst",
        "description": "News-driven. Tracks information flow and narrative shifts.",
        "signal_weights": {
            "model_dispersion": 0.15,
            "breadth": 0.20,
            "sentiment": 0.40,
            "vol_regime": 0.10,
            "credit_stress": 0.05,
            "drawdown_proximity": 0.10,
        },
        "model_weights": {
            "crossmamba": 0.50,  # primary model — best overall performance
            "tst": 0.30,
            "lightgbm": 0.20,
        },
        "risk_profile": "aggressive",
        "bias": "bull",
    },

    "risk": {
        "name": "Risk Analyst",
        "description": "Defensive. Capital preservation first. Looks for danger signs.",
        "signal_weights": {
            "model_dispersion": 0.05,
            "breadth": 0.10,
            "sentiment": 0.05,
            "vol_regime": 0.30,
            "credit_stress": 0.20,
            "drawdown_proximity": 0.30,
        },
        "model_weights": {
            "crossmamba": 0.55,  # -9.2% max drawdown vs LightGBM's -20.2% — better risk control
            "tst": 0.25,
            "lightgbm": 0.20,   # fallback if CrossMamba unavailable
        },
        "risk_profile": "conservative",
        "bias": "bear",
    },
}


# ─── PM Personalities ────────────────────────────────────────────────────

PM_PERSONALITIES = {
    "aggressive": {
        "name": "Aggressive PM",
        "description": "Growth-seeking. Weights momentum and sentiment analysts higher.",
        "analyst_weights": {
            "momentum": 0.30,
            "value": 0.10,
            "macro": 0.15,
            "sentiment": 0.30,
            "risk": 0.15,
        },
        "leverage_bias": 1.2,   # scales final leverage up by 20%
        "position_bias": 1.2,   # scales position count up by 20%
    },

    "conservative": {
        "name": "Conservative PM",
        "description": "Capital preservation. Weights risk and macro analysts higher.",
        "analyst_weights": {
            "momentum": 0.10,
            "value": 0.20,
            "macro": 0.25,
            "sentiment": 0.10,
            "risk": 0.35,
        },
        "leverage_bias": 0.8,   # scales final leverage down by 20%
        "position_bias": 0.8,
    },

    "balanced": {
        "name": "Balanced PM",
        "description": "Equal consideration. Moderate risk appetite.",
        "analyst_weights": {
            "momentum": 0.20,
            "value": 0.20,
            "macro": 0.20,
            "sentiment": 0.20,
            "risk": 0.20,
        },
        "leverage_bias": 1.0,
        "position_bias": 1.0,
    },
}


# ─── Risk profiles for parameter interpolation ──────────────────────────

RISK_PROFILES = {
    "conservative": {
        "conviction_floor": 0.2,   # minimum conviction to act
        "leverage_range": (0.6, 1.0),
        "position_range_long": (3, 8),
        "position_range_short": (2, 5),
        "vol_target_range": (0.05, 0.08),
    },
    "moderate": {
        "conviction_floor": 0.15,
        "leverage_range": (0.8, 1.4),
        "position_range_long": (5, 15),
        "position_range_short": (3, 8),
        "vol_target_range": (0.07, 0.12),
    },
    "aggressive": {
        "conviction_floor": 0.1,
        "leverage_range": (1.0, 1.8),
        "position_range_long": (10, 20),
        "position_range_short": (5, 12),
        "vol_target_range": (0.10, 0.16),
    },
}
