"""Position limits and sector classification.

Handles sector mapping, correlation groups, and position limit enforcement.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from core.config import settings
from core.profiles import is_test_profile


SECTOR_MAP = {
    "BTC": "major", "ETH": "major",
    "SOL": "L1", "AVAX": "L1", "ADA": "L1", "DOT": "L1", "NEAR": "L1",
    "APT": "L1", "SUI": "L1", "SEI": "L1", "INJ": "L1", "TIA": "L1",
    "ATOM": "L1", "ALGO": "L1", "HBAR": "L1", "ICP": "L1",
    "ORCA": "sol_eco", "TNSR": "sol_eco", "JTO": "sol_eco", "JUP": "sol_eco",
    "PYTH": "sol_eco", "BONK": "sol_eco", "WIF": "sol_eco",
    "UNI": "defi", "AAVE": "defi", "COMP": "defi", "MKR": "defi",
    "LINK": "defi", "SNX": "defi", "SUSHI": "defi", "CRV": "defi",
    "LDO": "defi", "FXS": "defi", "LQTY": "defi", "ONDO": "defi",
    "ENA": "defi", "AERO": "defi", "SUPER": "defi",
    "FET": "ai", "RNDR": "ai", "TAO": "ai", "AGIX": "ai",
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "FARTCOIN": "meme",
    "FLOKI": "meme", "MEME": "meme", "PENGU": "meme",
    "AXS": "gaming", "SAND": "gaming", "MANA": "gaming", "IMX": "gaming",
    "GALA": "gaming", "ENJ": "gaming",
    "FIL": "infra", "AR": "infra", "STORJ": "infra", "GRT": "infra", "QNT": "infra",
    "BNB": "exchange", "OKB": "exchange",
    "ZEC": "privacy", "XMR": "privacy",
    "XLM": "payments", "XRP": "payments", "LTC": "payments", "BCH": "payments",
}

CORRELATION_GROUPS = {
    "sol_heavy": ["SOL", "ORCA", "TNSR", "JTO", "BONK", "WIF"],
    "eth_heavy": ["ETH", "LDO", "AAVE", "UNI", "LINK"],
    "l1_basket": ["SOL", "AVAX", "ADA", "SUI", "APT", "SEI"],
    "meme_basket": ["DOGE", "SHIB", "PEPE", "FARTCOIN", "BONK"],
}


@dataclass
class PositionLimits:
    """Position limit configuration - risk via sizing/stops, NOT position count."""
    max_per_symbol: float = 100.0   # Allow larger per-symbol if confident
    max_per_sector: int = 999       # No sector limit - diversify freely
    max_per_corr_group: int = 999   # No correlation limit
    max_total_positions: int = 999  # No total limit - budget controls exposure
    max_weak_plays: int = 999       # No weak play limit - stops protect us
    global_cooldown_sec: int = 10   # Faster trading allowed
    symbol_cooldown_sec: int = 60   # Can re-enter same symbol faster
    daily_loss_limit_pct: float = 10.0  # Wider daily limit
    daily_loss_limit_usd: float = 50.0  # Wider daily limit


class LimitChecker:
    """Checks and enforces position limits."""
    
    def __init__(self, limits: Optional[PositionLimits] = None):
        self.limits = limits or PositionLimits()
        self._sector_positions: Dict[str, int] = {}
        self._last_trade_time: Optional[datetime] = None
    
    @staticmethod
    def get_sector(symbol: str) -> str:
        """Get sector for a symbol."""
        base = symbol.split("-")[0] if "-" in symbol else symbol
        return SECTOR_MAP.get(base, "other")
    
    def update_sector_counts(self, positions: dict):
        """Update sector position counts from current positions."""
        self._sector_positions = {}
        for symbol in positions.keys():
            sector = self.get_sector(symbol)
            self._sector_positions[sector] = self._sector_positions.get(sector, 0) + 1
    
    def check_limits(
        self,
        symbol: str,
        size_usd: float,
        current_positions: dict,
    ) -> tuple[bool, str]:
        """Check if new position passes all limits."""
        if is_test_profile(settings.profile):
            return True, "OK (test profile)"
        
        if len(current_positions) >= self.limits.max_total_positions:
            return False, f"Max {self.limits.max_total_positions} positions reached"
        
        current_exposure = sum(
            p.size_usd for p in current_positions.values()
            if p.symbol == symbol
        )
        if current_exposure >= self.limits.max_per_symbol:
            return False, f"Max ${self.limits.max_per_symbol} per symbol"
        
        sector = self.get_sector(symbol)
        sector_count = self._sector_positions.get(sector, 0)
        if sector_count >= self.limits.max_per_sector:
            return False, f"Max {self.limits.max_per_sector} positions in {sector}"
        
        weak_plays = sum(
            1 for p in current_positions.values()
            if getattr(p, 'play_quality', 'neutral') == 'weak'
        )
        if weak_plays >= self.limits.max_weak_plays:
            return False, f"Too many weak plays ({weak_plays})"
        
        if self._last_trade_time:
            elapsed = (datetime.now(timezone.utc) - self._last_trade_time).total_seconds()
            if elapsed < self.limits.global_cooldown_sec:
                remaining = int(self.limits.global_cooldown_sec - elapsed)
                return False, f"Global cooldown: {remaining}s remaining"
        
        return True, "OK"
    
    def record_trade(self):
        """Record that a trade was made for cooldown tracking."""
        self._last_trade_time = datetime.now(timezone.utc)


limit_checker = LimitChecker()
