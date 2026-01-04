"""Sector Rotation Tracker - Find what's hot when BTC is down.

Tracks sector-level momentum to identify rotation opportunities:
- Which sectors are outperforming/underperforming
- Rare divergence setups (sector up while BTC down)
- Sector strength rankings for smarter position selection
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from core.logging_utils import get_logger
from logic.limits import SECTOR_MAP, CORRELATION_GROUPS

logger = get_logger(__name__)


@dataclass
class SectorStats:
    """Performance stats for a sector."""
    sector: str
    symbols: List[str] = field(default_factory=list)
    avg_trend_1h: float = 0.0
    avg_trend_5m: float = 0.0
    best_performer: str = ""
    best_trend: float = 0.0
    worst_performer: str = ""
    worst_trend: float = 0.0
    strength_score: float = 0.0  # -100 to +100
    diverging_from_btc: bool = False
    last_update: Optional[datetime] = None


class SectorTracker:
    """Track sector rotation and find opportunities."""
    
    def __init__(self):
        self._sector_stats: Dict[str, SectorStats] = {}
        self._symbol_trends: Dict[str, Dict] = {}  # symbol -> {trend_1h, trend_5m, price}
        self._btc_trend_1h: float = 0.0
        self._last_update: Optional[datetime] = None
        
        # Initialize all sectors
        sectors = set(SECTOR_MAP.values())
        sectors.add("other")
        for sector in sectors:
            self._sector_stats[sector] = SectorStats(sector=sector)
    
    def update_symbol(self, symbol: str, trend_1h: float, trend_5m: float = 0.0, price: float = 0.0):
        """Update trend data for a symbol."""
        self._symbol_trends[symbol] = {
            "trend_1h": trend_1h,
            "trend_5m": trend_5m,
            "price": price,
            "ts": datetime.now(timezone.utc),
        }
    
    def update_btc_trend(self, trend_1h: float):
        """Update BTC trend for divergence detection."""
        self._btc_trend_1h = trend_1h
    
    def refresh_sector_stats(self):
        """Recalculate all sector statistics."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=5)  # Only use recent data
        
        # Group symbols by sector
        sector_symbols: Dict[str, List[Tuple[str, float, float]]] = defaultdict(list)
        
        for symbol, data in self._symbol_trends.items():
            if data.get("ts", now) < cutoff:
                continue  # Skip stale data
            
            base = symbol.split("-")[0] if "-" in symbol else symbol
            sector = SECTOR_MAP.get(base, "other")
            sector_symbols[sector].append((
                symbol,
                data.get("trend_1h", 0.0),
                data.get("trend_5m", 0.0),
            ))
        
        # Calculate stats per sector
        for sector, symbols in sector_symbols.items():
            if not symbols:
                continue
            
            stats = self._sector_stats.get(sector, SectorStats(sector=sector))
            stats.symbols = [s[0] for s in symbols]
            
            trends_1h = [s[1] for s in symbols]
            trends_5m = [s[2] for s in symbols]
            
            stats.avg_trend_1h = sum(trends_1h) / len(trends_1h) if trends_1h else 0.0
            stats.avg_trend_5m = sum(trends_5m) / len(trends_5m) if trends_5m else 0.0
            
            # Find best/worst
            best_idx = max(range(len(symbols)), key=lambda i: symbols[i][1]) if symbols else 0
            worst_idx = min(range(len(symbols)), key=lambda i: symbols[i][1]) if symbols else 0
            
            stats.best_performer = symbols[best_idx][0] if symbols else ""
            stats.best_trend = symbols[best_idx][1] if symbols else 0.0
            stats.worst_performer = symbols[worst_idx][0] if symbols else ""
            stats.worst_trend = symbols[worst_idx][1] if symbols else 0.0
            
            # Strength score: -100 to +100 based on avg trend
            stats.strength_score = max(-100, min(100, stats.avg_trend_1h * 20))
            
            # Divergence: sector up while BTC down (or vice versa)
            stats.diverging_from_btc = (
                (stats.avg_trend_1h > 0.5 and self._btc_trend_1h < -0.5) or
                (stats.avg_trend_1h < -0.5 and self._btc_trend_1h > 0.5)
            )
            
            stats.last_update = now
            self._sector_stats[sector] = stats
        
        self._last_update = now
    
    def get_hot_sectors(self, min_strength: float = 20.0) -> List[SectorStats]:
        """Get sectors with positive momentum."""
        self.refresh_sector_stats()
        return sorted(
            [s for s in self._sector_stats.values() if s.strength_score >= min_strength],
            key=lambda s: s.strength_score,
            reverse=True
        )
    
    def get_cold_sectors(self, max_strength: float = -20.0) -> List[SectorStats]:
        """Get sectors with negative momentum."""
        self.refresh_sector_stats()
        return sorted(
            [s for s in self._sector_stats.values() if s.strength_score <= max_strength],
            key=lambda s: s.strength_score,
        )
    
    def get_diverging_sectors(self) -> List[SectorStats]:
        """Get sectors diverging from BTC - rare opportunity."""
        self.refresh_sector_stats()
        return [s for s in self._sector_stats.values() if s.diverging_from_btc]
    
    def get_sector_ranking(self) -> List[SectorStats]:
        """Get all sectors ranked by strength."""
        self.refresh_sector_stats()
        return sorted(
            self._sector_stats.values(),
            key=lambda s: s.strength_score,
            reverse=True
        )
    
    def is_symbol_in_hot_sector(self, symbol: str) -> bool:
        """Check if symbol is in a hot sector."""
        base = symbol.split("-")[0] if "-" in symbol else symbol
        sector = SECTOR_MAP.get(base, "other")
        stats = self._sector_stats.get(sector)
        return stats is not None and stats.strength_score >= 20.0
    
    def is_symbol_diverging(self, symbol: str) -> bool:
        """Check if symbol's sector is diverging from BTC."""
        base = symbol.split("-")[0] if "-" in symbol else symbol
        sector = SECTOR_MAP.get(base, "other")
        stats = self._sector_stats.get(sector)
        return stats is not None and stats.diverging_from_btc
    
    def get_best_in_sector(self, sector: str) -> Optional[str]:
        """Get best performing symbol in a sector."""
        stats = self._sector_stats.get(sector)
        return stats.best_performer if stats else None
    
    def get_rotation_opportunities(self) -> List[Dict]:
        """Find rotation opportunities: move from cold to hot sectors."""
        opportunities = []
        
        hot = self.get_hot_sectors(min_strength=30.0)
        diverging = self.get_diverging_sectors()
        
        # Hot sectors when BTC is down = strong rotation signal
        if self._btc_trend_1h < -0.5:
            for sector in hot:
                opportunities.append({
                    "type": "hot_in_btc_down",
                    "sector": sector.sector,
                    "strength": sector.strength_score,
                    "best_symbol": sector.best_performer,
                    "confidence": min(100, 50 + abs(sector.strength_score)),
                })
        
        # Diverging sectors = rare setup
        for sector in diverging:
            opportunities.append({
                "type": "divergence",
                "sector": sector.sector,
                "strength": sector.strength_score,
                "best_symbol": sector.best_performer,
                "confidence": min(100, 70 + abs(sector.strength_score - self._btc_trend_1h * 10)),
            })
        
        return sorted(opportunities, key=lambda x: x["confidence"], reverse=True)
    
    def get_status_summary(self) -> Dict:
        """Get summary for dashboard display."""
        self.refresh_sector_stats()
        
        ranking = self.get_sector_ranking()
        hot = [s for s in ranking if s.strength_score >= 20]
        cold = [s for s in ranking if s.strength_score <= -20]
        diverging = [s for s in ranking if s.diverging_from_btc]
        
        return {
            "btc_trend_1h": self._btc_trend_1h,
            "hot_sectors": [{"sector": s.sector, "strength": s.strength_score, "best": s.best_performer} for s in hot[:3]],
            "cold_sectors": [{"sector": s.sector, "strength": s.strength_score, "worst": s.worst_performer} for s in cold[:3]],
            "diverging": [{"sector": s.sector, "direction": "up" if s.avg_trend_1h > 0 else "down"} for s in diverging],
            "opportunities": len(self.get_rotation_opportunities()),
            "last_update": self._last_update.isoformat() if self._last_update else None,
        }


# Singleton instance
sector_tracker = SectorTracker()
