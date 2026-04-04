"""
Portfolio parameter presets and interpolation.

Each analyst personality has a risk_profile that determines the
range of parameters they recommend. Conviction interpolates within that range.
"""
from .personalities import RISK_PROFILES

# Full parameter presets (floor and ceiling for all profiles)
PARAM_FLOOR = {
    "max_positions_long": 3,
    "max_positions_short": 2,
    "max_gross_leverage": 0.6,
    "max_net_leverage": 0.05,
    "target_annual_vol": 0.05,
    "max_drawdown_threshold": -0.05,
    "drawdown_scale_factor": 0.3,
    "weighting": "risk_parity",
    "sector_neutral": True,
    "max_sector_net_pct": 0.03,
    "max_daily_turnover": 0.15,
}

PARAM_CEILING = {
    "max_positions_long": 20,
    "max_positions_short": 12,
    "max_gross_leverage": 1.8,
    "max_net_leverage": 0.30,
    "target_annual_vol": 0.16,
    "max_drawdown_threshold": -0.15,
    "drawdown_scale_factor": 0.7,
    "weighting": "score",
    "sector_neutral": False,
    "max_sector_net_pct": 0.15,
    "max_daily_turnover": 0.50,
}

INTERPOLATABLE = [
    "max_positions_long", "max_positions_short",
    "max_gross_leverage", "max_net_leverage",
    "target_annual_vol", "max_drawdown_threshold",
    "drawdown_scale_factor", "max_sector_net_pct",
    "max_daily_turnover",
]


def interpolate_params_from_profile(conviction: float, profile_name: str) -> dict:
    """
    Interpolate parameters based on conviction and risk profile.

    Each risk profile defines ranges for key params.
    Conviction 0 → low end of range, conviction 1 → high end.
    """
    profile = RISK_PROFILES.get(profile_name, RISK_PROFILES["moderate"])

    # Check conviction floor — below this, recommend minimal positioning
    if conviction < profile.get("conviction_floor", 0.15):
        return dict(PARAM_FLOOR)

    # Normalize conviction within the active range (floor to 1.0)
    floor = profile.get("conviction_floor", 0.15)
    t = (conviction - floor) / (1.0 - floor)  # 0 to 1

    # Profile-specific ranges
    lev_lo, lev_hi = profile["leverage_range"]
    long_lo, long_hi = profile["position_range_long"]
    short_lo, short_hi = profile["position_range_short"]
    vol_lo, vol_hi = profile["vol_target_range"]

    result = {
        "max_positions_long": round(long_lo + (long_hi - long_lo) * t),
        "max_positions_short": round(short_lo + (short_hi - short_lo) * t),
        "max_gross_leverage": round(lev_lo + (lev_hi - lev_lo) * t, 3),
        "max_net_leverage": round(0.05 + 0.25 * t, 3),
        "target_annual_vol": round(vol_lo + (vol_hi - vol_lo) * t, 4),
        "max_drawdown_threshold": round(-0.05 - 0.10 * t, 3),
        "drawdown_scale_factor": round(0.3 + 0.4 * t, 2),
        "max_sector_net_pct": round(0.03 + 0.12 * t, 3),
        "max_daily_turnover": round(0.15 + 0.35 * t, 3),
    }

    # Non-numeric params based on conviction threshold
    if t > 0.6:
        result["weighting"] = "score"
        result["sector_neutral"] = False
    elif t > 0.3:
        result["weighting"] = "risk_parity"
        result["sector_neutral"] = True
    else:
        result["weighting"] = "risk_parity"
        result["sector_neutral"] = True

    return result


# Legacy compatibility
def interpolate_params(conviction: float, bias: str) -> dict:
    """Legacy function — maps bull/bear to risk profiles."""
    if bias == "bull":
        return interpolate_params_from_profile(conviction, "aggressive")
    else:
        return interpolate_params_from_profile(conviction, "conservative")
