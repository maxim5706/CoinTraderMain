"""
Tiered Symbol Scheduler

Manages 3-tier symbol rotation for full universe coverage:
- Tier 1 (WS): Top 30 symbols on WebSocket real-time
- Tier 2 (REST Fast): Next 50 symbols polled every 15s
- Tier 3 (REST Slow): Remaining symbols polled every 60s

Tier membership updates every 30 minutes with universe refresh.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, List
from enum import Enum

from core.logging_utils import get_logger

logger = get_logger(__name__)


class Tier(Enum):
    WS_REALTIME = 1      # Top 75 - WebSocket (real-time)
    REST_FAST = 2        # Next 15 - 15s polling
    REST_SLOW = 3        # Remaining - 60s polling
    UNASSIGNED = 0       # Not yet assigned


@dataclass
class TierConfig:
    """Configuration for tier sizes and intervals."""
    tier1_size: int = 75          # WS real-time (increased for more coverage)
    tier2_size: int = 15          # REST fast (reduced since more on WS)
    tier2_interval_s: float = 15  # 15 seconds
    tier3_interval_s: float = 60  # 60 seconds
    reassign_interval_s: float = 1800  # 30 minutes
    
    # Warmup requirements (lower = faster to trade, but less data)
    # Fast warmup: backfill + rehydration should handle most of this
    min_candles_1m: int = 5    # 5 minutes warmup (was 10)
    min_candles_5m: int = 2    # 10 minutes of 5m data (was 3)


@dataclass
class SymbolTierInfo:
    """Tier assignment for a symbol."""
    symbol: str
    tier: Tier = Tier.UNASSIGNED
    last_polled: Optional[datetime] = None
    candle_count_1m: int = 0
    candle_count_5m: int = 0
    is_warm: bool = False
    is_backfilling: bool = False
    backfill_started: Optional[datetime] = None
    
    def needs_poll(self, config: TierConfig) -> bool:
        """Check if symbol needs polling based on tier interval."""
        if self.tier == Tier.WS_REALTIME:
            return False  # WS handles this
        
        if self.last_polled is None:
            return True
        
        now = datetime.now(timezone.utc)
        elapsed = (now - self.last_polled).total_seconds()
        
        if self.tier == Tier.REST_FAST:
            return elapsed >= config.tier2_interval_s
        elif self.tier == Tier.REST_SLOW:
            return elapsed >= config.tier3_interval_s
        
        return False
    
    def check_warmth(self, config: TierConfig) -> bool:
        """Check if symbol has enough candle history."""
        self.is_warm = (
            self.candle_count_1m >= config.min_candles_1m and
            self.candle_count_5m >= config.min_candles_5m
        )
        return self.is_warm


class TierScheduler:
    """
    Manages tier assignments and polling schedules.
    
    Coordinates between:
    - WS collector (Tier 1)
    - REST poller (Tier 2 & 3)
    - Dynamic backfill (on tier changes)
    """
    
    def __init__(self, config: Optional[TierConfig] = None):
        self.config = config or TierConfig()
        self.symbols: dict[str, SymbolTierInfo] = {}
        self._last_reassign: Optional[datetime] = None
        self._running = False
        
        # Callbacks
        self.on_tier_change: Optional[Callable] = None  # (symbol, old_tier, new_tier)
        self.on_ws_add: Optional[Callable] = None       # (symbol) - trigger backfill
        self.on_ws_remove: Optional[Callable] = None    # (symbol)
        
        # Stats
        self.total_reassigns = 0
        self.total_promotions = 0
        self.total_demotions = 0
    
    def get_tier_symbols(self, tier: Tier) -> List[str]:
        """Get all symbols in a specific tier."""
        return [s.symbol for s in self.symbols.values() if s.tier == tier]
    
    def get_tier1_symbols(self) -> List[str]:
        """Get WS (Tier 1) symbols."""
        return self.get_tier_symbols(Tier.WS_REALTIME)
    
    def get_tier2_symbols(self) -> List[str]:
        """Get REST Fast (Tier 2) symbols."""
        return self.get_tier_symbols(Tier.REST_FAST)
    
    def get_tier3_symbols(self) -> List[str]:
        """Get REST Slow (Tier 3) symbols."""
        return self.get_tier_symbols(Tier.REST_SLOW)
    
    def get_symbols_needing_poll(self) -> tuple[List[str], List[str]]:
        """Get symbols due for REST polling."""
        tier2_due = []
        tier3_due = []
        
        for info in self.symbols.values():
            if info.needs_poll(self.config):
                if info.tier == Tier.REST_FAST:
                    tier2_due.append(info.symbol)
                elif info.tier == Tier.REST_SLOW:
                    tier3_due.append(info.symbol)
        
        return tier2_due, tier3_due
    
    def record_poll(self, symbol: str, candle_count_1m: int = 0, candle_count_5m: int = 0):
        """Record that a symbol was polled."""
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolTierInfo(symbol=symbol)
        
        info = self.symbols[symbol]
        info.last_polled = datetime.now(timezone.utc)
        info.candle_count_1m = candle_count_1m
        info.candle_count_5m = candle_count_5m
        info.check_warmth(self.config)
    
    def update_candle_counts(self, symbol: str, count_1m: int, count_5m: int):
        """Update candle counts for a symbol."""
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolTierInfo(symbol=symbol)
        
        info = self.symbols[symbol]
        info.candle_count_1m = count_1m
        info.candle_count_5m = count_5m
        info.check_warmth(self.config)
    
    def is_symbol_warm(self, symbol: str) -> bool:
        """Check if symbol has enough history for trading."""
        if symbol not in self.symbols:
            return False
        return self.symbols[symbol].is_warm
    
    def reassign_tiers(self, ranked_symbols: List[str]):
        """
        Reassign all symbols to tiers based on new ranking.
        
        Args:
            ranked_symbols: List of symbols sorted by priority (best first)
        """
        old_tier1 = set(self.get_tier1_symbols())
        new_tier1 = set()
        new_tier2 = set()
        new_tier3 = set()
        
        for i, symbol in enumerate(ranked_symbols):
            if symbol not in self.symbols:
                self.symbols[symbol] = SymbolTierInfo(symbol=symbol)
            
            info = self.symbols[symbol]
            old_tier = info.tier
            
            if i < self.config.tier1_size:
                info.tier = Tier.WS_REALTIME
                new_tier1.add(symbol)
            elif i < self.config.tier1_size + self.config.tier2_size:
                info.tier = Tier.REST_FAST
                new_tier2.add(symbol)
            else:
                info.tier = Tier.REST_SLOW
                new_tier3.add(symbol)
            
            # Track tier changes
            if old_tier != info.tier:
                if self.on_tier_change:
                    self.on_tier_change(symbol, old_tier, info.tier)
                
                if info.tier == Tier.WS_REALTIME:
                    self.total_promotions += 1
                elif old_tier == Tier.WS_REALTIME:
                    self.total_demotions += 1
        
        # Find symbols promoted to WS (need backfill)
        ws_additions = new_tier1 - old_tier1
        ws_removals = old_tier1 - new_tier1
        
        for symbol in ws_additions:
            if self.on_ws_add:
                self.on_ws_add(symbol)
        
        for symbol in ws_removals:
            if self.on_ws_remove:
                self.on_ws_remove(symbol)
        
        self._last_reassign = datetime.now(timezone.utc)
        self.total_reassigns += 1
        
        logger.info(
            "[TIER] Reassigned: T1=%s, T2=%s, T3=%s",
            len(new_tier1),
            len(new_tier2),
            len(new_tier3),
        )
        if ws_additions:
            logger.info("[TIER] WS adds: %s...", list(ws_additions)[:5])
        if ws_removals:
            logger.info("[TIER] WS removes: %s...", list(ws_removals)[:5])
    
    def needs_reassign(self) -> bool:
        """Check if tier reassignment is due."""
        if self._last_reassign is None:
            return True
        
        elapsed = (datetime.now(timezone.utc) - self._last_reassign).total_seconds()
        return elapsed >= self.config.reassign_interval_s
    
    def mark_backfilling(self, symbol: str, is_backfilling: bool):
        """Mark symbol as currently backfilling."""
        if symbol in self.symbols:
            self.symbols[symbol].is_backfilling = is_backfilling
            if is_backfilling:
                self.symbols[symbol].backfill_started = datetime.now(timezone.utc)
    
    def get_cold_tier1_symbols(self) -> List[str]:
        """Get Tier 1 symbols that aren't warm yet."""
        cold = []
        for symbol in self.get_tier1_symbols():
            info = self.symbols.get(symbol)
            if info and not info.is_warm and not info.is_backfilling:
                cold.append(symbol)
        return cold
    
    def get_stats(self) -> dict:
        """Get scheduler statistics."""
        tier_counts = {t: 0 for t in Tier}
        warm_count = 0
        cold_count = 0
        
        for info in self.symbols.values():
            tier_counts[info.tier] += 1
            if info.is_warm:
                warm_count += 1
            else:
                cold_count += 1
        
        return {
            "total_symbols": len(self.symbols),
            "tier1_ws": tier_counts[Tier.WS_REALTIME],
            "tier2_fast": tier_counts[Tier.REST_FAST],
            "tier3_slow": tier_counts[Tier.REST_SLOW],
            "warm": warm_count,
            "cold": cold_count,
            "total_reassigns": self.total_reassigns,
            "promotions": self.total_promotions,
            "demotions": self.total_demotions,
        }


# Singleton instance
tier_scheduler = TierScheduler()
