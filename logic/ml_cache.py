"""ML and live indicator cache management.

Single source of truth for cached indicators and ML scores per symbol.
"""

from typing import Dict, Optional
from datetime import datetime, timezone

from core.logging_utils import get_logger

logger = get_logger(__name__)


class IndicatorCache:
    """Manages cached live indicators and ML scores."""
    
    def __init__(self):
        self.live_indicators: Dict[str, object] = {}
        self.live_ml: Dict[str, object] = {}
    
    def update_indicators(self, symbol: str, indicators):
        """Update cached live indicators for a symbol."""
        if indicators is None:
            return
        
        self.live_indicators[symbol] = indicators
        
        if indicators.is_ready:
            try:
                from logic.live_features import live_scorer
                self.live_ml[symbol] = live_scorer.score_from_indicators(indicators)
            except Exception as e:
                logger.warning("[ML] Error scoring %s: %s", symbol, e)
    
    def get_ml(self, symbol: str, max_stale_seconds: float = 180):
        """Get cached ML result for symbol. Returns None if stale."""
        ml = self.live_ml.get(symbol)
        if ml and ml.is_stale(max_stale_seconds):
            return None
        return ml
    
    def get_indicators(self, symbol: str, max_stale_seconds: float = 120):
        """Get cached indicators for symbol. Returns None if stale."""
        ind = self.live_indicators.get(symbol)
        if ind and ind.is_stale(max_stale_seconds):
            return None
        return ind
    
    def get_freshness_stats(self) -> dict:
        """Get cache freshness statistics."""
        now = datetime.now(timezone.utc)
        fresh_count = 0
        total_count = len(self.live_ml)
        
        for ml in self.live_ml.values():
            if not ml.is_stale(180):
                fresh_count += 1
        
        return {
            "fresh_count": fresh_count,
            "total_count": total_count,
            "fresh_pct": (fresh_count / total_count * 100) if total_count > 0 else 0,
        }
    
    def clear_symbol(self, symbol: str):
        """Clear cached data for a symbol."""
        self.live_indicators.pop(symbol, None)
        self.live_ml.pop(symbol, None)
    
    def clear_all(self):
        """Clear all cached data."""
        self.live_indicators.clear()
        self.live_ml.clear()


indicator_cache = IndicatorCache()
