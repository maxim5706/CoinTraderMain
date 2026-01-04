"""
Position Controller - Manual position management from web dashboard.

Allows manual:
- Close individual positions
- Close all positions (panic button)
- Adjust stop loss
- Adjust take profit levels
- Lock profits (move stop to breakeven)
- Override trailing stops
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable

from core.logging_utils import get_logger
from core.config import settings
from core.models import Position

logger = get_logger(__name__)


class PositionController:
    """
    Manual position control interface for web dashboard.
    
    This controller is initialized with references to the order router
    and executor to perform actual position operations.
    """
    
    _instance: Optional["PositionController"] = None
    
    def __init__(self):
        self._order_router = None
        self._executor = None
        self._get_price_func = None
        self._state = None
        self._audit_log: list[dict] = []
    
    @classmethod
    def get_instance(cls) -> "PositionController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def initialize(self, order_router, executor, get_price_func, state):
        """Initialize with references to trading components."""
        self._order_router = order_router
        self._executor = executor
        self._get_price_func = get_price_func
        self._state = state
        logger.info("[POS_CTRL] Initialized with order router and executor")
    
    def _audit(self, action: str, symbol: str, details: dict):
        """Log action to audit trail."""
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "symbol": symbol,
            "details": details,
        }
        self._audit_log.append(entry)
        # Keep last 100 entries
        if len(self._audit_log) > 100:
            self._audit_log = self._audit_log[-100:]
        logger.info("[POS_CTRL] %s %s: %s", action, symbol, details)
    
    def get_positions(self) -> list[dict]:
        """Get all current positions as dicts."""
        if not self._order_router:
            return []
        
        positions = []
        for symbol, pos in self._order_router.positions.items():
            current_price = self._get_price_func(symbol) if self._get_price_func else pos.entry_price
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price * 100) if pos.entry_price else 0
            pnl_usd = pos.quantity * (current_price - pos.entry_price)
            age_min = (datetime.now(timezone.utc) - pos.entry_time).total_seconds() / 60 if pos.entry_time else 0
            
            positions.append({
                "symbol": symbol,
                "entry_price": pos.entry_price,
                "current_price": current_price,
                "quantity": pos.quantity,
                "size_usd": pos.quantity * current_price,
                "stop_price": pos.stop_price,
                "tp1_price": pos.tp1_price,
                "tp2_price": pos.tp2_price,
                "pnl_pct": pnl_pct,
                "pnl_usd": pnl_usd,
                "age_min": age_min,
                "strategy": getattr(pos, "strategy", "unknown"),
                "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
                "trailing_active": getattr(pos, "trailing_active", False),
                "breakeven_locked": getattr(pos, "breakeven_locked", False),
            })
        
        return positions
    
    def get_position(self, symbol: str) -> Optional[dict]:
        """Get a single position by symbol."""
        positions = self.get_positions()
        for pos in positions:
            if pos["symbol"] == symbol or pos["symbol"].replace("-USD", "") == symbol:
                return pos
        return None
    
    async def close_position(self, symbol: str, reason: str = "manual_close") -> dict:
        """
        Close a single position.
        
        Returns dict with success status and details.
        """
        if not self._order_router:
            return {"success": False, "error": "Order router not initialized"}
        
        # Normalize symbol
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        
        if symbol not in self._order_router.positions:
            return {"success": False, "error": f"No position found for {symbol}"}
        
        pos = self._order_router.positions[symbol]
        current_price = self._get_price_func(symbol) if self._get_price_func else pos.entry_price
        
        try:
            # Use exit manager if available
            if hasattr(self._order_router, '_exit_manager'):
                result = await self._order_router._exit_manager.exit_position(
                    symbol, 
                    "manual", 
                    reason
                )
            else:
                # Direct execution
                result = await self._executor.market_sell(symbol, pos.quantity)
            
            self._audit("CLOSE", symbol, {
                "reason": reason,
                "price": current_price,
                "quantity": pos.quantity,
            })
            
            return {
                "success": True,
                "symbol": symbol,
                "exit_price": current_price,
                "quantity": pos.quantity,
                "reason": reason,
            }
            
        except Exception as e:
            logger.error("[POS_CTRL] Failed to close %s: %s", symbol, e)
            return {"success": False, "error": str(e)}
    
    async def close_all_positions(self, reason: str = "manual_close_all") -> dict:
        """
        Close all positions (panic button).
        
        Returns dict with results for each position.
        """
        if not self._order_router:
            return {"success": False, "error": "Order router not initialized"}
        
        symbols = list(self._order_router.positions.keys())
        if not symbols:
            return {"success": True, "message": "No positions to close", "closed": []}
        
        results = []
        errors = []
        
        for symbol in symbols:
            result = await self.close_position(symbol, reason)
            if result.get("success"):
                results.append(result)
            else:
                errors.append({"symbol": symbol, "error": result.get("error")})
        
        self._audit("CLOSE_ALL", "*", {"count": len(results), "errors": len(errors)})
        
        return {
            "success": len(errors) == 0,
            "closed": results,
            "errors": errors,
            "total": len(symbols),
        }
    
    async def update_stop(self, symbol: str, new_stop: float) -> dict:
        """
        Update stop loss for a position.
        
        Returns dict with success status.
        """
        if not self._order_router:
            return {"success": False, "error": "Order router not initialized"}
        
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        
        if symbol not in self._order_router.positions:
            return {"success": False, "error": f"No position found for {symbol}"}
        
        pos = self._order_router.positions[symbol]
        old_stop = pos.stop_price
        
        # Validate: stop must be below entry for long positions
        if new_stop >= pos.entry_price:
            return {"success": False, "error": "Stop must be below entry price for long positions"}
        
        # Validate: stop can't be too far (>15%)
        stop_pct = (pos.entry_price - new_stop) / pos.entry_price * 100
        if stop_pct > 15:
            return {"success": False, "error": f"Stop too far ({stop_pct:.1f}%), max 15%"}
        
        # Update position
        pos.stop_price = new_stop
        
        # Update stop order on exchange if live
        if self._order_router.mode.value == "live":
            try:
                if hasattr(self._order_router, 'stop_manager') and self._order_router.stop_manager:
                    await self._order_router.stop_manager.update_stop(symbol, new_stop)
            except Exception as e:
                logger.warning("[POS_CTRL] Failed to update exchange stop: %s", e)
        
        self._audit("UPDATE_STOP", symbol, {"old": old_stop, "new": new_stop})
        
        return {
            "success": True,
            "symbol": symbol,
            "old_stop": old_stop,
            "new_stop": new_stop,
        }
    
    async def update_tp(self, symbol: str, tp1: Optional[float] = None, tp2: Optional[float] = None) -> dict:
        """
        Update take profit levels for a position.
        """
        if not self._order_router:
            return {"success": False, "error": "Order router not initialized"}
        
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        
        if symbol not in self._order_router.positions:
            return {"success": False, "error": f"No position found for {symbol}"}
        
        pos = self._order_router.positions[symbol]
        old_tp1 = pos.tp1_price
        old_tp2 = pos.tp2_price
        
        # Validate TPs must be above entry
        if tp1 is not None:
            if tp1 <= pos.entry_price:
                return {"success": False, "error": "TP1 must be above entry price"}
            pos.tp1_price = tp1
        
        if tp2 is not None:
            if tp2 <= pos.entry_price:
                return {"success": False, "error": "TP2 must be above entry price"}
            if tp1 and tp2 <= tp1:
                return {"success": False, "error": "TP2 must be above TP1"}
            pos.tp2_price = tp2
        
        self._audit("UPDATE_TP", symbol, {
            "old_tp1": old_tp1, "new_tp1": pos.tp1_price,
            "old_tp2": old_tp2, "new_tp2": pos.tp2_price,
        })
        
        return {
            "success": True,
            "symbol": symbol,
            "tp1": pos.tp1_price,
            "tp2": pos.tp2_price,
        }
    
    async def lock_profits(self, symbol: str) -> dict:
        """
        Move stop to breakeven (entry price).
        """
        if not self._order_router:
            return {"success": False, "error": "Order router not initialized"}
        
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        
        if symbol not in self._order_router.positions:
            return {"success": False, "error": f"No position found for {symbol}"}
        
        pos = self._order_router.positions[symbol]
        current_price = self._get_price_func(symbol) if self._get_price_func else pos.entry_price
        
        # Must be in profit to lock
        if current_price <= pos.entry_price:
            return {"success": False, "error": "Position must be in profit to lock breakeven"}
        
        old_stop = pos.stop_price
        # Set stop to entry + small buffer for fees
        new_stop = pos.entry_price * 1.002  # 0.2% above entry to cover fees
        pos.stop_price = new_stop
        pos.breakeven_locked = True
        
        # Update on exchange if live
        if self._order_router.mode.value == "live":
            try:
                if hasattr(self._order_router, 'stop_manager') and self._order_router.stop_manager:
                    await self._order_router.stop_manager.update_stop(symbol, new_stop)
            except Exception as e:
                logger.warning("[POS_CTRL] Failed to update exchange stop: %s", e)
        
        self._audit("LOCK_PROFITS", symbol, {"old_stop": old_stop, "new_stop": new_stop})
        
        return {
            "success": True,
            "symbol": symbol,
            "new_stop": new_stop,
            "message": "Breakeven locked",
        }
    
    async def activate_trailing(self, symbol: str, trail_pct: float = 2.0) -> dict:
        """
        Activate trailing stop for a position.
        """
        if not self._order_router:
            return {"success": False, "error": "Order router not initialized"}
        
        if not symbol.endswith("-USD"):
            symbol = f"{symbol}-USD"
        
        if symbol not in self._order_router.positions:
            return {"success": False, "error": f"No position found for {symbol}"}
        
        if not 0.5 <= trail_pct <= 10.0:
            return {"success": False, "error": "Trail percent must be between 0.5% and 10%"}
        
        pos = self._order_router.positions[symbol]
        current_price = self._get_price_func(symbol) if self._get_price_func else pos.entry_price
        
        # Set trailing parameters
        pos.trailing_active = True
        pos.trail_pct = trail_pct
        pos.trail_high = current_price
        pos.stop_price = current_price * (1 - trail_pct / 100)
        
        self._audit("ACTIVATE_TRAILING", symbol, {
            "trail_pct": trail_pct,
            "current_price": current_price,
            "new_stop": pos.stop_price,
        })
        
        return {
            "success": True,
            "symbol": symbol,
            "trailing_active": True,
            "trail_pct": trail_pct,
            "stop_price": pos.stop_price,
        }
    
    def get_audit_log(self, limit: int = 50) -> list[dict]:
        """Get recent audit log entries."""
        return self._audit_log[-limit:]


# Singleton accessor
def get_position_controller() -> PositionController:
    """Get the singleton PositionController instance."""
    return PositionController.get_instance()
