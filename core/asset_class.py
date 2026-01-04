"""Asset classification for dynamic risk management.

Classifies assets by market cap/liquidity tier to adjust:
- Stop loss percentage (tighter for small caps)
- Hold time expectations (shorter for risky assets)
- Position sizing confidence multiplier
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from core.logging_utils import get_logger

logger = get_logger(__name__)


class AssetTier(Enum):
    """Asset classification tiers."""
    LARGE_CAP = "large_cap"      # BTC, ETH - blue chips
    MID_CAP = "mid_cap"          # SOL, LINK, AVAX - established alts
    SMALL_CAP = "small_cap"      # Most alts - higher risk
    MICRO_CAP = "micro_cap"      # Low liquidity, very risky


@dataclass
class AssetRiskProfile:
    """Risk parameters for an asset tier."""
    tier: AssetTier
    stop_loss_pct: float         # Stop loss percentage
    take_profit_pct: float       # First take profit target
    max_hold_hours: int          # Max hold time before time stop
    confidence_multiplier: float # Adjust confidence scoring
    description: str


# Tier definitions with risk parameters
TIER_PROFILES = {
    AssetTier.LARGE_CAP: AssetRiskProfile(
        tier=AssetTier.LARGE_CAP,
        stop_loss_pct=0.08,       # 8% stop - less volatile
        take_profit_pct=0.12,     # 12% TP1 - R:R = 1.5 ✓
        max_hold_hours=168,       # Hold up to 1 week
        confidence_multiplier=1.2, # Boost confidence for blue chips
        description="Blue chip - swing trade friendly"
    ),
    AssetTier.MID_CAP: AssetRiskProfile(
        tier=AssetTier.MID_CAP,
        stop_loss_pct=0.06,       # 6% stop
        take_profit_pct=0.09,     # 9% TP1 - R:R = 1.5 ✓
        max_hold_hours=72,        # Hold up to 3 days
        confidence_multiplier=1.0, # Normal confidence
        description="Established alt - day trade"
    ),
    AssetTier.SMALL_CAP: AssetRiskProfile(
        tier=AssetTier.SMALL_CAP,
        stop_loss_pct=0.05,       # 5% stop - give room to breathe
        take_profit_pct=0.075,    # 7.5% TP1 - R:R = 1.5 ✓
        max_hold_hours=24,        # Hold up to 1 day
        confidence_multiplier=0.8, # Lower confidence
        description="Small cap - scalp/quick trade"
    ),
    AssetTier.MICRO_CAP: AssetRiskProfile(
        tier=AssetTier.MICRO_CAP,
        stop_loss_pct=0.04,       # 4% stop - still need room
        take_profit_pct=0.06,     # 6% TP1 - R:R = 1.5 ✓
        max_hold_hours=8,         # Hold max 8 hours
        confidence_multiplier=0.6, # Heavy confidence penalty
        description="Micro cap - high risk, quick exit"
    ),
}

# Known large cap symbols
LARGE_CAP_SYMBOLS = {
    "BTC", "ETH", "BNB", "XRP", "USDT", "USDC", "SOL", "ADA", "DOGE", "TRX",
    "AVAX", "SHIB", "DOT", "LINK", "TON", "MATIC", "BCH", "LTC", "UNI", "ATOM"
}

# Known mid cap symbols
MID_CAP_SYMBOLS = {
    "XLM", "ETC", "FIL", "HBAR", "APT", "IMX", "NEAR", "INJ", "OP", "ARB",
    "AAVE", "MKR", "ALGO", "VET", "RENDER", "GRT", "FTM", "SAND", "MANA",
    "AXS", "STX", "QNT", "SNX", "CRV", "LDO", "RUNE", "KAVA", "FLOW", "CFX",
    "THETA", "XTZ", "EOS", "NEO", "IOTA", "ZEC", "HNT", "ENS", "COMP", "YFI",
    "PAXG", "CBETH"
}

# Known micro cap symbols (low liquidity, high risk)
MICRO_CAP_SYMBOLS = {
    "SYRUP", "SQD", "SKY", "WELL", "SUPER", "ORCA", "GRT", "FLR", "CRO"
}


def classify_asset(symbol: str) -> AssetTier:
    """
    Classify an asset by its tier based on known lists.
    Unknown assets default to SMALL_CAP for safety.
    """
    # Normalize symbol (remove -USD suffix if present)
    base = symbol.split("-")[0].upper() if "-" in symbol else symbol.upper()
    
    if base in LARGE_CAP_SYMBOLS:
        return AssetTier.LARGE_CAP
    elif base in MID_CAP_SYMBOLS:
        return AssetTier.MID_CAP
    elif base in MICRO_CAP_SYMBOLS:
        return AssetTier.MICRO_CAP
    else:
        # Default to small cap for unknown assets
        return AssetTier.SMALL_CAP


def get_risk_profile(symbol: str) -> AssetRiskProfile:
    """Get the risk profile for a symbol."""
    tier = classify_asset(symbol)
    return TIER_PROFILES[tier]


def get_dynamic_stop_loss(symbol: str) -> float:
    """Get dynamic stop loss percentage for a symbol."""
    profile = get_risk_profile(symbol)
    return profile.stop_loss_pct


def get_dynamic_take_profit(symbol: str) -> float:
    """Get dynamic take profit percentage for a symbol."""
    profile = get_risk_profile(symbol)
    return profile.take_profit_pct


def get_max_hold_hours(symbol: str) -> int:
    """Get maximum hold time in hours for a symbol."""
    profile = get_risk_profile(symbol)
    return profile.max_hold_hours


def adjust_confidence(symbol: str, base_confidence: float) -> float:
    """Adjust confidence score based on asset tier."""
    profile = get_risk_profile(symbol)
    return base_confidence * profile.confidence_multiplier


# Convenience function for logging
def log_asset_classification(symbol: str):
    """Log asset classification for debugging."""
    profile = get_risk_profile(symbol)
    logger.info(
        "[ASSET] %s -> %s: stop=%.1f%%, tp=%.1f%%, max_hold=%dh",
        symbol, profile.tier.value,
        profile.stop_loss_pct * 100,
        profile.take_profit_pct * 100,
        profile.max_hold_hours
    )
