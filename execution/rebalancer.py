"""Portfolio rebalancing utilities.

Extracted from order_router.py - handles force close, auto-rebalance,
and position trimming.
"""

from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from core.config import settings
from core.logging_utils import get_logger
from core.mode_configs import TradingMode
from core.models import TradeResult

if TYPE_CHECKING:
    from core.position_registry import PositionRegistry
    from core.trading_interfaces import IPositionPersistence

logger = get_logger(__name__)


class Rebalancer:
    """Handles portfolio rebalancing operations."""
    
    def __init__(
        self,
        mode: TradingMode,
        positions: dict,
        position_registry: "PositionRegistry",
        persistence: "IPositionPersistence",
        get_price_func,
        close_full_func,
        execute_live_sell_func,
        get_portfolio_value_func,
        client=None,
    ):
        self.mode = mode
        self.positions = positions
        self.position_registry = position_registry
        self.persistence = persistence
        self.get_price = get_price_func
        self.close_full = close_full_func
        self.execute_live_sell = execute_live_sell_func
        self.get_portfolio_value = get_portfolio_value_func
        self._client = client
    
    async def force_close_all(self, reason: str = "manual") -> int:
        """
        Force close all positions.
        
        Args:
            reason: Exit reason to log
            
        Returns:
            Number of positions closed
        """
        closed = 0
        for symbol in list(self.positions.keys()):
            position = self.positions[symbol]
            price = self.get_price(symbol)
            try:
                await self.close_full(position, price, reason)
                closed += 1
            except Exception as e:
                logger.error("[REBAL] Failed to close %s: %s", symbol, e)
        
        logger.info("[REBAL] Force closed %d positions", closed)
        return closed
    
    async def auto_rebalance(self, target_available_pct: float = 0.3) -> float:
        """
        Auto-rebalance: sell enough of largest positions to free up budget.
        
        Args:
            target_available_pct: Target % of budget to have available (0.3 = 30%)
        
        Returns:
            Amount freed up in USD
        """
        if self.mode == TradingMode.PAPER:
            logger.info("[REBAL] Not available in paper mode")
            return 0.0
        
        # Calculate current state
        portfolio_value = self.get_portfolio_value()
        bot_budget = portfolio_value * settings.portfolio_max_exposure_pct
        current_exposure = sum(p.size_usd for p in self.positions.values())
        available = bot_budget - current_exposure
        target_available = bot_budget * target_available_pct
        
        # How much do we need to free?
        need_to_free = target_available - available
        
        if need_to_free <= 0:
            logger.info("[REBAL] Already have $%.0f available (target: $%.0f)",
                       available, target_available)
            return 0.0
        
        logger.info("[REBAL] Need to free $%.0f to reach $%.0f available",
                   need_to_free, target_available)
        
        # Sort positions by size (largest first)
        sorted_positions = sorted(
            self.positions.values(),
            key=lambda p: p.size_usd,
            reverse=True
        )
        
        freed = 0.0
        for position in sorted_positions:
            if freed >= need_to_free:
                break
            
            symbol = position.symbol
            price = self.get_price(symbol)
            
            logger.info("[REBAL] Closing %s ($%.0f) to free budget", symbol, position.size_usd)
            
            try:
                result = await self.close_full(position, price, "rebalance")
                if result:
                    freed += position.size_usd
                    logger.info("[REBAL] Freed $%.0f, total: $%.0f", position.size_usd, freed)
            except Exception as e:
                logger.error("[REBAL] Failed to close %s: %s", symbol, e)
        
        logger.info("[REBAL] Complete. Freed $%.0f. New available: $%.0f",
                   freed, available + freed)
        return freed
    
    async def trim_largest(self, amount_usd: float = 50.0) -> Optional[TradeResult]:
        """
        Trim the largest position by a specific dollar amount.
        
        Args:
            amount_usd: How much to trim (default $50)
        
        Returns:
            TradeResult if successful
        """
        if self.mode == TradingMode.PAPER or not self.positions:
            return None
        
        # Find largest position
        largest = max(self.positions.values(), key=lambda p: p.size_usd)
        
        if largest.size_usd < amount_usd * 1.5:
            # Position too small to trim, just close it
            price = self.get_price(largest.symbol)
            return await self.close_full(largest, price, "trim")
        
        # Partial close
        price = self.get_price(largest.symbol)
        qty_to_sell = amount_usd / price if price > 0 else 0
        
        if qty_to_sell <= 0:
            return None
        
        logger.info("[TRIM] Selling $%.0f of %s", amount_usd, largest.symbol)
        
        try:
            if self._client:
                await self.execute_live_sell(largest.symbol, qty_to_sell)
                
                # Update position
                largest.size_qty -= qty_to_sell
                largest.size_usd -= amount_usd
                
                if largest.size_usd < 5:
                    # Position is dust, remove it
                    self.position_registry.remove_position(largest.symbol)
                    self.positions.pop(largest.symbol, None)
                
                self.persistence.save_positions(self.positions)
                logger.info("[TRIM] Done. %s now $%.0f", largest.symbol, largest.size_usd)
                
                return TradeResult(
                    symbol=largest.symbol,
                    entry_price=largest.entry_price,
                    exit_price=price,
                    exit_time=datetime.now(timezone.utc),
                    pnl=0,
                    pnl_pct=0,
                    exit_reason="trim"
                )
        except Exception as e:
            logger.error("[TRIM] Error: %s", e, exc_info=True)
        
        return None
    
    def get_rebalance_info(self) -> dict:
        """
        Get current rebalance state info.
        
        Returns:
            Dict with portfolio state
        """
        portfolio_value = self.get_portfolio_value()
        bot_budget = portfolio_value * settings.portfolio_max_exposure_pct
        current_exposure = sum(p.size_usd for p in self.positions.values())
        available = bot_budget - current_exposure
        
        return {
            "portfolio_value": portfolio_value,
            "bot_budget": bot_budget,
            "current_exposure": current_exposure,
            "available": available,
            "exposure_pct": (current_exposure / bot_budget * 100) if bot_budget > 0 else 0,
            "position_count": len(self.positions),
        }
