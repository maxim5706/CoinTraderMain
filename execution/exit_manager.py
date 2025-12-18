"""Exit management for open positions.

Extracted from order_router.py - handles stop loss, take profit,
trailing stops, thesis invalidation, and position closing.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from core.config import settings
from core.logging_utils import get_logger
from core.models import Position, TradeResult, Side
from core.mode_configs import TradingMode
from core.logger import log_exit_decision, log_trade, utc_iso_str
from core.alerts import alert_trade_exit
from logic.intelligence import intelligence

if TYPE_CHECKING:
    from core.pnl_engine import PnLEngine
    from core.position_registry import PositionRegistry
    from core.trading_interfaces import IPositionPersistence, IStopOrderManager
    from execution.order_manager import OrderManager
    from execution.risk import DailyStats

logger = get_logger(__name__)


@dataclass
class ExitDecision:
    """Result of exit check."""
    should_exit: bool
    reason: str = ""
    is_partial: bool = False
    
    
class ExitManager:
    """Manages position exits including stops, TPs, and thesis invalidation."""
    
    def __init__(
        self,
        mode: TradingMode,
        positions: dict,
        position_registry: "PositionRegistry",
        persistence: "IPositionPersistence",
        stop_manager: "IStopOrderManager",
        pnl_engine: "PnLEngine",
        daily_stats: "DailyStats",
        order_manager: "OrderManager",
        portfolio,
        get_price_func,
        execute_live_sell_func,
        config,
        event_bus=None,
    ):
        self.mode = mode
        self.positions = positions
        self.position_registry = position_registry
        self.persistence = persistence
        self.stop_manager = stop_manager
        self.pnl_engine = pnl_engine
        self.daily_stats = daily_stats
        self.order_manager = order_manager
        self.portfolio = portfolio
        self.get_price = get_price_func
        self.execute_live_sell = execute_live_sell_func
        self.config = config
        self.event_bus = event_bus
        
        # Track recently closed to prevent re-adding
        self.recently_closed: dict[str, datetime] = {}
        # Track last stop health check
        self._last_stop_check: dict[str, datetime] = {}
        # Trade history
        self.trade_history: list[TradeResult] = []
    
    async def check_exits(self, symbol: str) -> Optional[TradeResult]:
        """Check if position should exit with smart trailing stops."""
        
        # Skip recently closed positions
        if symbol in self.recently_closed:
            return None
        
        position = self.positions.get(symbol)
        if position is None:
            return None
        
        current_price = self.get_price(symbol)
        if current_price <= 0:
            return None
        
        # Stop-loss sanity: ensure active exchange stop exists
        self._ensure_stop_order(symbol, position)
        
        # Self-healing for invalid positions
        self._heal_invalid_position(position, current_price)
        
        pnl_pct = ((current_price / position.entry_price) - 1) * 100
        
        # Update trailing stop
        self._update_trailing_stop(position, pnl_pct, current_price)
        
        # Determine exit reason
        exit_decision = self._evaluate_exit(position, current_price, pnl_pct)
        
        if not exit_decision.should_exit:
            return None
        
        # Log exit decision
        log_exit_decision({
            "ts": utc_iso_str(),
            "symbol": symbol,
            "exit_reason": exit_decision.reason,
            "current_price": current_price,
            "entry_price": position.entry_price,
            "stop_price": position.stop_price,
            "tp1_price": position.tp1_price,
            "pnl_pct": pnl_pct,
            "hold_minutes": position.hold_duration_minutes(),
            "is_partial": exit_decision.is_partial and not position.partial_closed,
        })
        
        if exit_decision.is_partial and not position.partial_closed:
            return await self._close_partial(position, current_price, exit_decision.reason)
        else:
            return await self._close_full(position, current_price, exit_decision.reason)
    
    def _ensure_stop_order(self, symbol: str, position: Position):
        """Ensure stop order exists on exchange for live positions."""
        if self.mode != TradingMode.LIVE or not position.stop_price:
            return
        
        now = datetime.now(timezone.utc)
        last_check = self._last_stop_check.get(symbol)
        
        if last_check and (now - last_check).total_seconds() <= settings.stop_health_check_interval:
            return
        
        self._last_stop_check[symbol] = now
        
        if not self.order_manager.has_stop_order(symbol):
            placed_id = self.order_manager.place_stop_order(
                symbol=symbol,
                qty=position.size_qty,
                stop_price=position.stop_price,
            )
            if placed_id:
                logger.warning("[LIVE] Re-armed missing stop for %s @ $%.4f", symbol, position.stop_price)
            else:
                logger.error("[LIVE] Missing stop for %s and failed to re-arm", symbol)
    
    def _heal_invalid_position(self, position: Position, current_price: float):
        """Self-heal positions with invalid prices."""
        symbol = position.symbol
        
        # Fix invalid entry price
        if position.entry_price <= 0:
            logger.warning("[HEAL] %s has invalid entry_price=%.4f, healing with current price $%.4f",
                          symbol, position.entry_price, current_price)
            position.entry_price = current_price
            position.stop_price = current_price * (1 - settings.fixed_stop_pct)
            position.tp1_price = current_price * (1 + settings.tp1_pct)
            position.tp2_price = current_price * (1 + settings.tp2_pct)
            position.strategy_id = "healed"
            self.persistence.save_positions(self.positions)
            logger.info("[HEAL] %s healed: entry=$%.4f, stop=$%.4f, tp1=$%.4f",
                       symbol, position.entry_price, position.stop_price, position.tp1_price)
        
        # Fix invalid stop price
        if position.stop_price <= 0 or position.stop_price >= position.entry_price:
            old_stop = position.stop_price
            position.stop_price = current_price * (1 - settings.fixed_stop_pct)
            logger.warning("[HEAL] %s had invalid stop=$%.4f, reset to $%.4f",
                          symbol, old_stop, position.stop_price)
            self.persistence.save_positions(self.positions)
    
    def _update_trailing_stop(self, position: Position, pnl_pct: float, current_price: float):
        """Update trailing stop based on profit and regime."""
        symbol = position.symbol
        
        trail_start = settings.trail_start_pct * 100
        trail_lock = settings.trail_lock_pct
        be_trigger = settings.trail_be_trigger_pct * 100
        btc_regime = intelligence._market_regime
        
        # Tighten trail in risk-off regime
        if btc_regime == "risk_off" and pnl_pct > 0:
            trail_lock = 0.70
            trail_start = 0.5
            if position.stop_price < position.entry_price:
                position.stop_price = position.entry_price * 1.001
                logger.info("[REGIME] %s: BTC RISK_OFF - moving stop to BE", symbol)
                self.persistence.save_positions(self.positions)
        
        # Trail stop if up trail_start%+
        if pnl_pct >= trail_start:
            new_stop = position.entry_price * (1 + pnl_pct * trail_lock / 100)
            if new_stop > position.stop_price:
                old_stop = position.stop_price
                position.stop_price = new_stop
                self.stop_manager.update_stop_price(symbol, new_stop)
                logger.info("[TRAIL] %s: Stop raised $%.4f → $%.4f (lock %.1f%%)",
                           symbol, old_stop, new_stop, pnl_pct * trail_lock)
                self.persistence.save_positions(self.positions)
        
        # Move to breakeven if up be_trigger%+
        elif pnl_pct >= be_trigger and position.stop_price < position.entry_price:
            position.stop_price = position.entry_price * 1.001
            self.stop_manager.update_stop_price(symbol, position.stop_price)
            logger.info("[TRAIL] %s: Stop moved to breakeven @ $%.4f", symbol, position.stop_price)
            self.persistence.save_positions(self.positions)
    
    def _evaluate_exit(self, position: Position, current_price: float, pnl_pct: float) -> ExitDecision:
        """Evaluate all exit conditions."""
        symbol = position.symbol
        
        # Check stop loss
        if position.should_stop(current_price):
            return ExitDecision(True, "stop")
        
        # Check TP1 (partial)
        if position.should_tp1(current_price):
            return ExitDecision(True, "tp1", is_partial=True)
        
        # Check TP2 (full)
        if position.should_tp2(current_price):
            return ExitDecision(True, "tp2")
        
        # Check thesis invalidation (only if losing)
        if pnl_pct < 0:
            thesis_exit = self._check_thesis_invalidation(position, pnl_pct)
            if thesis_exit:
                return ExitDecision(True, thesis_exit)
        
        # Check weak confidence
        if position.current_confidence < 15 and pnl_pct < 3.0:
            return ExitDecision(True, f"weak_confidence: {position.current_confidence:.0f}%")
        
        # Check time stop (if enabled)
        if settings.time_stop_enabled and position.time_stop_min:
            hold_min = position.hold_duration_minutes()
            if hold_min >= position.time_stop_min:
                if pnl_pct > -0.5:
                    return ExitDecision(True, "time_stop")
                elif hold_min >= position.time_stop_min + 5:
                    return ExitDecision(True, "time_stop_extended")
        
        return ExitDecision(False)
    
    def _check_thesis_invalidation(self, position: Position, pnl_pct: float) -> Optional[str]:
        """Check if trade thesis is invalidated."""
        symbol = position.symbol
        
        try:
            ind = intelligence.get_live_indicators(symbol)
            ml = intelligence.get_live_ml(symbol)
            
            # Synced positions get more tolerance
            is_synced = position.strategy_id in ("sync", "sync_underwater", "healed", "recovered")
            thesis_mult = 2.0 if is_synced else 1.0
            min_loss = -2.0 if is_synced else 0.0
            
            if not (ind and ind.is_ready and pnl_pct < min_loss):
                return None
            
            trend_threshold = settings.thesis_trend_flip_5m * thesis_mult
            choppy_loss = -2.0 if is_synced else -1.0
            vwap_threshold = settings.thesis_vwap_distance * thesis_mult
            
            # 5m trend flipped bearish
            if ind.trend_5m < trend_threshold:
                logger.info("[THESIS] %s: 5m trend flipped to %.1f%% - exiting", symbol, ind.trend_5m)
                return f"thesis_invalid: 5m trend {ind.trend_5m:.1f}%"
            
            # Choppy price action
            if ind.is_choppy and pnl_pct < choppy_loss:
                daily_pos = getattr(ind, 'daily_range_position', 0.5)
                week_pos = getattr(ind, 'week_range_position', 0.5)
                if daily_pos >= 0.15 and week_pos >= 0.2:
                    logger.info("[THESIS] %s: Choppy + losing %.1f%% - exiting", symbol, pnl_pct)
                    return "thesis_invalid: choppy_losing"
            
            # ML bearish (skip for synced)
            if not is_synced and ml and ml.bearish and ml.confidence > 0.6 and pnl_pct < -0.5:
                logger.info("[THESIS] %s: ML bearish %.2f - exiting", symbol, ml.raw_score)
                return f"thesis_invalid: ml_bearish ({ml.raw_score:.2f})"
            
            # Below VWAP significantly
            if ind.vwap_distance < vwap_threshold:
                logger.info("[THESIS] %s: %.1f%% below VWAP - exiting", symbol, ind.vwap_distance)
                return f"thesis_invalid: below_vwap {ind.vwap_distance:.1f}%"
                
        except Exception:
            pass
        
        return None
    
    async def _close_partial(self, position: Position, price: float, reason: str) -> Optional[TradeResult]:
        """Close partial position (TP1)."""
        symbol = position.symbol
        
        close_qty = position.size_qty * settings.tp1_partial_pct
        close_usd = close_qty * price
        closed_cost = close_qty * position.entry_price if position.entry_price else 0.0
        pnl = (price - position.entry_price) * close_qty
        
        if self.mode == TradingMode.PAPER:
            logger.info("[PAPER] Partial close %s @ $%.4f (%s)", symbol, price, reason)
            if hasattr(self.portfolio, 'credit'):
                self.portfolio.credit(close_usd + pnl)
        else:
            await self.execute_live_sell(symbol, close_qty)
        
        position.realized_pnl += pnl
        position.partial_closed = True
        position.size_qty -= close_qty
        position.size_usd -= close_usd
        
        # Move stop to breakeven
        position.stop_price = position.entry_price * 1.001
        
        # Update stop order on exchange
        self.stop_manager.cancel_stop_order(symbol)
        self.stop_manager.place_stop_order(
            symbol=symbol,
            qty=position.size_qty,
            stop_price=position.stop_price
        )
        
        logger.info("Partial PnL: $%.2f, stop raised to breakeven $%.4f", pnl, position.stop_price)
        self.persistence.save_positions(self.positions)
        
        # Emit event
        pnl_pct = (pnl / closed_cost * 100) if closed_cost > 0 else 0.0
        self._emit_order_event("partial_close", position, price, reason, pnl, pnl_pct, close_usd, close_qty)
        
        return None  # Position still open
    
    async def _close_full(self, position: Position, price: float, reason: str) -> TradeResult:
        """Close full position."""
        symbol = position.symbol
        
        # Calculate final PnL
        pnl_breakdown = self.pnl_engine.calculate_trade_pnl(
            entry_price=position.entry_price,
            exit_price=price,
            qty=position.size_qty,
            side=position.side,
            realized_pnl=position.realized_pnl
        )
        
        if self.mode == TradingMode.PAPER:
            logger.info("[PAPER] Closed %s @ $%.4f (%s)", symbol, price, reason)
            if hasattr(self.portfolio, 'credit'):
                self.portfolio.credit(position.size_usd + pnl_breakdown.net_pnl)
        else:
            self.stop_manager.cancel_stop_order(symbol)
            await self.execute_live_sell(symbol, position.size_qty)
        
        pnl = pnl_breakdown.net_pnl
        pnl_pct = pnl_breakdown.pnl_pct
        
        logger.info("Gross: $%.2f | Fees: $%.2f | Net: $%.2f (%.1f%%)",
                   pnl_breakdown.gross_pnl, pnl_breakdown.total_fees, pnl, pnl_pct)
        
        result = TradeResult(
            symbol=symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=price,
            entry_time=position.entry_time,
            exit_time=datetime.now(),
            size_usd=position.size_usd,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason
        )
        
        self.trade_history.append(result)
        
        # Track strategy PnL
        if position.strategy_id:
            self.pnl_engine.track_strategy_pnl(position.strategy_id, pnl)
        
        # Record in daily stats (skip TEST trades)
        if not symbol.startswith("TEST"):
            self.daily_stats.record_trade(pnl)
        
        # Remove position
        self.position_registry.remove_position(symbol)
        self.positions.pop(symbol, None)
        
        # Track as recently closed
        self.recently_closed[symbol] = datetime.now(timezone.utc)
        
        # Log ML training data
        intelligence.log_trade_exit(
            symbol=symbol,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            hold_minutes=position.hold_duration_minutes()
        )
        
        # Send alert
        asyncio.create_task(alert_trade_exit(
            symbol=symbol,
            entry_price=position.entry_price,
            exit_price=price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            reason=reason
        ))
        
        # Update persistence
        self.persistence.save_positions(self.positions)
        self.persistence.clear_position(symbol)
        
        # Log trade close
        initial_risk = position.entry_price - position.stop_price
        r_multiple = 0.0
        if initial_risk > 0 and position.entry_price > 0 and position.size_usd > 0:
            r_multiple = (pnl / position.size_usd) / (initial_risk / position.entry_price)
        
        log_trade({
            "ts": utc_iso_str(result.exit_time),
            "type": "trade_close",
            "symbol": symbol,
            "strategy_id": position.strategy_id,
            "entry_price": position.entry_price,
            "exit_price": price,
            "stop_price": position.stop_price,
            "size_usd": position.size_usd,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "r_multiple": round(r_multiple, 2),
            "exit_reason": reason,
            "hold_minutes": position.hold_duration_minutes(),
            "tp1_hit": position.partial_closed,
        })
        
        log_trade({
            "ts": utc_iso_str(),
            "type": "fill",
            "symbol": symbol,
            "side": "sell",
            "price": price,
            "qty": position.size_qty
        })
        
        # Emit event
        self._emit_order_event("close", position, price, reason, pnl, pnl_pct, position.size_usd, position.size_qty)
        
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        logger.info("Final PnL: %s (%.2f%%)", pnl_str, pnl_pct)
        
        return result
    
    def _emit_order_event(
        self,
        event_type: str,
        position: Position,
        price: float,
        reason: str,
        pnl: float = 0.0,
        pnl_pct: float = 0.0,
        size_usd: float = 0.0,
        size_qty: float = 0.0,
    ):
        """Emit order event to event bus."""
        if not self.event_bus:
            return
        
        from core.events import OrderEvent
        
        event = OrderEvent(
            event_type=event_type,
            symbol=position.symbol,
            side=position.side,
            mode=self.mode.value,
            strategy_id=position.strategy_id or "",
            price=price,
            size_usd=size_usd or position.size_usd,
            size_qty=size_qty or position.size_qty,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )
        self.event_bus.emit_order(event)
    
    def update_position_confidence(self, symbol: str):
        """Update confidence tracking for an open position."""
        position = self.positions.get(symbol)
        if position is None:
            return
        
        ml_result = intelligence.get_live_ml(symbol)
        if not (ml_result and not ml_result.is_stale()):
            return
        
        position.ml_score_current = ml_result.raw_score
        
        current_price = self.get_price(symbol)
        if current_price <= 0 or position.entry_price <= 0:
            return
        
        pnl_pct = ((current_price / position.entry_price) - 1) * 100
        conf = position.entry_confidence
        
        # Adjust based on price action
        if pnl_pct > 2.0:
            conf += 10
        elif pnl_pct < -1.5:
            conf -= 15
        
        # Adjust based on ML
        is_synced = position.strategy_id in ("sync", "sync_underwater", "healed", "recovered")
        if position.ml_score_entry != 0 and not is_synced:
            ml_delta = position.ml_score_current - position.ml_score_entry
            conf += ml_delta * 20
        elif is_synced and position.ml_score_current > 0:
            conf += position.ml_score_current * 10
        
        # Floor for synced positions
        if is_synced:
            conf = max(40, conf)
        
        position.current_confidence = max(0, min(100, conf))
        
        if position.current_confidence > position.peak_confidence:
            position.peak_confidence = position.current_confidence
    
    def update_all_position_confidence(self):
        """Update confidence for all open positions."""
        for symbol in self.positions:
            self.update_position_confidence(symbol)
