"""
Unified Position Registry - single source of truth for all positions.

Handles:
- Position storage and queries
- Dust threshold management  
- Multi-strategy position attribution
- Position lifecycle tracking
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Callable
from datetime import datetime, timezone

from core.logging_utils import get_logger
from core.models import Position, PositionState
from core.mode_configs import BaseTradingConfig

logger = get_logger(__name__)


@dataclass
class PositionLimits:
    """Configurable position limits (no more hardcoded values)."""
    min_position_usd: float = 1.0      # Minimum to open position
    dust_threshold_usd: float = 0.50   # Below this = dust  
    max_positions: int = 10            # Per strategy or total
    min_hold_seconds: int = 30         # Minimum hold time


@dataclass 
class PositionStats:
    """Position statistics and metrics."""
    total_positions: int = 0
    active_positions: int = 0
    dust_positions: int = 0
    total_exposure_usd: float = 0.0
    by_strategy: Dict[str, int] = field(default_factory=dict)
    

class PositionRegistry:
    """
    Unified position registry with configurable limits.
    
    This replaces scattered position tracking with a single,
    clean interface that handles dust, limits, and multi-strategy.
    """
    
    def __init__(self, config: BaseTradingConfig):
        self.config = config
        self.limits = self._create_limits()
        self._positions: Dict[str, Position] = {}
        self._dust_positions: Dict[str, Position] = {}
        self._exchange_holdings_func: Optional[Callable[[], Set[str]]] = None
    
    def set_exchange_holdings_func(self, func: Callable[[], Set[str]]):
        """Set function to get actual exchange holdings for reconciliation."""
        self._exchange_holdings_func = func
    
    def get_reconciled_active_count(self) -> int:
        """
        Get active position count, reconciled with exchange if possible.
        This prevents the gate from blocking when registry is out of sync.
        """
        registry_count = len(self._positions)
        
        if self._exchange_holdings_func:
            try:
                exchange_symbols = self._exchange_holdings_func()
                # Count positions that exist both in registry AND on exchange
                reconciled = sum(1 for s in self._positions if s in exchange_symbols)
                if reconciled != registry_count:
                    logger.debug("[REGISTRY] Position count: registry=%d, reconciled=%d", 
                                registry_count, reconciled)
                return reconciled
            except Exception:
                pass
        
        return registry_count
        
    def _create_limits(self) -> PositionLimits:
        """Create position limits from config."""
        return PositionLimits(
            min_position_usd=getattr(self.config, 'min_position_usd', 1.0),
            dust_threshold_usd=getattr(self.config, 'dust_threshold_usd', 0.50),
            max_positions=self.config.max_positions,
            min_hold_seconds=getattr(self.config, 'min_hold_seconds', 30),
        )

    def update_config(self, config: BaseTradingConfig) -> None:
        """Update config and recompute derived limits."""
        self.config = config
        self.limits = self._create_limits()
    
    def add_position(self, position: Position) -> bool:
        """
        Add position to registry.
        
        Returns:
            True if added to active positions
            False if added to dust (still tracked!)
        """
        current_value = position.size_qty * position.entry_price
        
        if current_value >= self.limits.dust_threshold_usd:
            self._positions[position.symbol] = position
            return True
        else:
            self._dust_positions[position.symbol] = position  
            return False
    
    def get_position(self, symbol: str) -> Optional[Position]:
        """Get position (checks both active and dust)."""
        return self._positions.get(symbol) or self._dust_positions.get(symbol)
    
    def has_position(self, symbol: str) -> bool:
        """Check if position exists (active or dust)."""
        return symbol in self._positions or symbol in self._dust_positions
    
    def has_active_position(self, symbol: str) -> bool:
        """Check if position is active (not dust)."""
        return symbol in self._positions
    
    def remove_position(self, symbol: str) -> Optional[Position]:
        """Remove position from registry."""
        position = self._positions.pop(symbol, None)
        if position is None:
            position = self._dust_positions.pop(symbol, None)
        return position
    
    def update_position_value(self, symbol: str, current_price: float):
        """Update position value and handle dust transitions."""
        position = self.get_position(symbol)
        if not position:
            return
            
        current_value = position.size_qty * current_price
        
        # Check for dust transition
        is_active = symbol in self._positions
        should_be_active = current_value >= self.limits.dust_threshold_usd
        
        if is_active and not should_be_active:
            # Move to dust
            pos = self._positions.pop(symbol)
            self._dust_positions[symbol] = pos
        elif not is_active and should_be_active:
            # Move to active
            pos = self._dust_positions.pop(symbol)  
            self._positions[symbol] = pos
    
    def get_active_positions(self) -> Dict[str, Position]:
        """Get all active (non-dust) positions."""
        return dict(self._positions)
    
    def get_dust_positions(self) -> Dict[str, Position]:
        """Get all dust positions."""
        return dict(self._dust_positions)
    
    def get_all_positions(self) -> Dict[str, Position]:
        """Get all positions (active + dust)."""
        all_pos = dict(self._positions)
        all_pos.update(self._dust_positions)
        return all_pos
    
    def can_open_position(
        self, 
        strategy_id: str, 
        position_value_usd: float
    ) -> tuple[bool, str]:
        """
        Check if new position can be opened.
        
        Returns:
            (can_open, reason)
        """
        # Check minimum value
        if position_value_usd < self.limits.min_position_usd:
            return False, f"Below minimum ${self.limits.min_position_usd}"
        
        # Check max positions using reconciled count
        active_count = self.get_reconciled_active_count()
        if active_count >= self.limits.max_positions:
            logger.info("[REGISTRY] Gate blocked: %d/%d positions (registry has %d)",
                       active_count, self.limits.max_positions, len(self._positions))
            return False, f"Max positions ({self.limits.max_positions}) reached (currently {active_count})"
        
        # Check strategy-specific limits (if configured)
        strategy_positions = self.get_positions_by_strategy(strategy_id)
        max_per_strategy = getattr(self.config, 'max_positions_per_strategy', None)
        if max_per_strategy and len(strategy_positions) >= max_per_strategy:
            return False, f"Max {strategy_id} positions ({max_per_strategy}) reached"
        
        return True, "OK"
    
    def can_close_position(self, symbol: str) -> tuple[bool, str]:
        """Check if position can be closed (min hold time, etc.)."""
        position = self.get_position(symbol)
        if not position:
            return False, "Position not found"
        
        # Check minimum hold time
        hold_seconds = (datetime.now(timezone.utc) - position.entry_time).total_seconds()
        if hold_seconds < self.limits.min_hold_seconds:
            remaining = self.limits.min_hold_seconds - hold_seconds
            return False, f"Min hold time: {remaining:.0f}s remaining"
        
        return True, "OK"
    
    def get_positions_by_strategy(self, strategy_id: str) -> List[Position]:
        """Get all positions for a specific strategy."""
        return [
            pos for pos in self.get_all_positions().values()
            if pos.strategy_id == strategy_id
        ]
    
    def get_exposure_by_strategy(self, price_func) -> Dict[str, float]:
        """Get USD exposure by strategy.""" 
        exposure = {}
        for pos in self.get_active_positions().values():
            if pos.strategy_id not in exposure:
                exposure[pos.strategy_id] = 0.0
            current_price = price_func(pos.symbol)
            if current_price > 0:
                exposure[pos.strategy_id] += pos.size_qty * current_price
        return exposure
    
    def get_stats(self, price_func) -> PositionStats:
        """Get position statistics."""
        active = self.get_active_positions()
        dust = self.get_dust_positions()
        
        total_exposure = 0.0
        by_strategy = {}
        
        for pos in active.values():
            current_price = price_func(pos.symbol)
            if current_price > 0:
                total_exposure += pos.size_qty * current_price
            
            if pos.strategy_id not in by_strategy:
                by_strategy[pos.strategy_id] = 0
            by_strategy[pos.strategy_id] += 1
        
        return PositionStats(
            total_positions=len(active) + len(dust),
            active_positions=len(active),
            dust_positions=len(dust),
            total_exposure_usd=total_exposure,
            by_strategy=by_strategy,
        )
