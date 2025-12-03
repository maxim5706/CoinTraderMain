"""
Profile-based configuration overrides.

PROFILES control trading thresholds/geometry (what quality of setups to take).
MODE (paper/live) controls execution path (real orders vs simulated).

Usage:
    TRADING_MODE=paper PROFILE=paper-profile uv run python run_v2.py
    TRADING_MODE=live PROFILE=live-profile uv run python run_v2.py
    TRADING_MODE=paper PROFILE=test-profile uv run python run_v2.py  # For testing flow
"""

from typing import Dict, Any

# Only allow overriding these keys to avoid drifting gate order logic.
ALLOWED_PROFILE_KEYS = {
    "entry_score_min",
    "min_rr_ratio",
    "spread_max_bps",
    "fixed_stop_pct",
    "tp1_pct",
    "tp2_pct",
    "ml_min_confidence",
    "ml_boost_scale",
    "paper_start_balance_usd",  # Paper-only: starting cash balance
}

# Profile definitions
PROFILES: Dict[str, Dict[str, Any]] = {
    # === PRODUCTION PROFILES ===
    
    # paper-profile: Realistic paper trading with real market data
    # Use this for measurement before going live
    "paper-profile": {
        "entry_score_min": 60,       # Standard selectivity
        "min_rr_ratio": 2.0,         # Good R:R required
        "spread_max_bps": 25.0,      # Tight spreads only
        "ml_min_confidence": 0.55,   # ML must be confident
        "paper_start_balance_usd": 1000.0,  # Paper starts with $1000
    },
    
    # live-profile: Active live trading
    # Tuned based on trading data: 82.5% win rate but -12.20% avg loss
    "live-profile": {
        "entry_score_min": 60,       # Increase selectivity (was 50)
        "min_rr_ratio": 2.0,         # Require better R:R (was 1.5) 
        "spread_max_bps": 18.0,      # Tighter spreads only (was 20.0)
        "ml_min_confidence": 0.0,    # ML warming up, don't gate on it
        "fixed_stop_pct": 0.04,      # Wider stops for live (was 0.035)
    },
    
    # === TESTING PROFILES ===
    
    # test-profile: Bypasses most gates for testing flow
    # NEVER use with TRADING_MODE=live!
    "test-profile": {
        "entry_score_min": 10,       # Accept almost any signal
        "min_rr_ratio": 1.0,         # Accept any R:R
        "spread_max_bps": 100.0,     # Accept wide spreads
        "ml_min_confidence": 0.0,    # Ignore ML
    },
    
    # === LEGACY PROFILES (for backwards compatibility) ===
    "prod": {},  # No overrides - uses base config defaults
    "aggressive": {
        "entry_score_min": 55,
        "min_rr_ratio": 1.8,
        "ml_min_confidence": 0.5,
    },
    "conservative": {
        "entry_score_min": 65,
        "min_rr_ratio": 2.2,
        "spread_max_bps": 18.0,
    },
    "test": {  # Alias for test-profile
        "entry_score_min": 10,
        "min_rr_ratio": 1.0,
        "spread_max_bps": 100.0,
        "ml_min_confidence": 0.0,
    },
}

PROFILE_ALIASES = {
    "paper": "paper-profile",
    "live": "live-profile",
    "test": "test-profile",
}


def is_test_profile(profile: str) -> bool:
    """Check if profile is a test/debug profile (bypasses gates)."""
    return profile in ("test", "test-profile")


def default_profile_for_mode(mode: str) -> str:
    """Default profile selection by mode."""
    return "paper-profile" if mode == "paper" else "live-profile"


def apply_profile(profile: str, settings_obj):
    """Apply a named profile to the provided settings instance."""
    if not profile:
        profile = default_profile_for_mode(settings_obj.trading_mode)
    # Allow short aliases: paper/live/test
    profile = PROFILE_ALIASES.get(profile, profile)
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    
    # SAFETY: Prevent test profiles from being used in LIVE mode
    if is_test_profile(profile) and settings_obj.trading_mode == "live":
        raise ValueError(
            f"DANGER: Cannot use '{profile}' with TRADING_MODE=live! "
            f"Test profiles bypass safety gates. Use 'live-profile' instead."
        )
    
    overrides = PROFILES[profile]
    for key, value in overrides.items():
        if key not in ALLOWED_PROFILE_KEYS:
            raise ValueError(f"Profile key not allowed: {key}")
        if not hasattr(settings_obj, key):
            raise ValueError(f"Settings has no attribute '{key}'")
        setattr(settings_obj, key, value)
    # Track active profile on the settings object for observability
    setattr(settings_obj, "profile", profile)
