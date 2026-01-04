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
from core.asset_class import get_risk_profile, get_dynamic_stop_loss, get_max_hold_hours
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
        exchange_sync=None,
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
        self.exchange_sync = exchange_sync
        
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
        
        # Update position's current price for state serialization
        position.current_price = current_price
        
        # Stop-loss sanity: ensure active exchange stop exists
        self._ensure_stop_order(symbol, position)
        
        # Self-healing for invalid positions
        self._heal_invalid_position(position, current_price)
        
        pnl_pct = ((current_price / position.entry_price) - 1) * 100
        position.pnl_pct = pnl_pct
        position.pnl_usd = position.unrealized_pnl(current_price)
        
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

    def _fee_aware_profit_pct(self) -> float:
        cfg = self.config
        use_limit = bool(getattr(cfg, "use_limit_orders", False))
        entry_fee_rate = float(getattr(cfg, "maker_fee_pct", 0.006) if use_limit else getattr(cfg, "taker_fee_pct", 0.012))
        exit_fee_rate = float(getattr(cfg, "taker_fee_pct", 0.012))
        fee_pct_total = (entry_fee_rate + exit_fee_rate) * 100.0
        fee_buffer_pct = float(getattr(settings, "fee_buffer_pct", 0.5))
        return fee_pct_total + fee_buffer_pct
    
    def _ensure_stop_order(self, symbol: str, position: Position):
        """Ensure stop order exists on exchange for live positions."""
        # Debug: log mode check
        if self.mode != TradingMode.LIVE:
            logger.debug("[STOPS] %s: skipping (mode=%s, not LIVE)", symbol, self.mode)
            return
        if not position.stop_price:
            logger.warning("[STOPS] %s: no stop_price set!", symbol)
            return
        
        # Skip staked positions - they cannot be sold
        if self.exchange_sync and self.exchange_sync.is_staked(symbol):
            logger.debug("[STOPS] Skipping staked position %s - cannot place stop", symbol)
            return
        
        now = datetime.now(timezone.utc)
        last_check = self._last_stop_check.get(symbol)
        
        if last_check and (now - last_check).total_seconds() <= settings.stop_health_check_interval:
            return
        
        self._last_stop_check[symbol] = now
        
        if not self.order_manager.has_stop_order(symbol):
            # Use available_qty from exchange_sync to avoid INSUFFICIENT_FUND errors
            qty = position.size_qty
            if self.exchange_sync:
                detail = self.exchange_sync.holdings_detail.get(symbol, {})
                available_qty = detail.get('available_qty', 0)
                if available_qty > 0 and available_qty < qty:
                    logger.info("[STOPS] %s: clamping qty %.8f -> %.8f (available)", symbol, qty, available_qty)
                    qty = available_qty * 0.999  # Small buffer for rounding
            
            if qty <= 0:
                logger.warning("[STOPS] %s: no available qty to place stop", symbol)
                return
            
            placed_id = self.order_manager.place_stop_order(
                symbol=symbol,
                qty=qty,
                stop_price=position.stop_price,
            )
            if placed_id:
                # Store stop_order_id on position for persistence
                position.stop_order_id = placed_id
                logger.warning("[LIVE] Re-armed missing stop for %s @ $%.4f (qty=%.8f, order=%s)", 
                             symbol, position.stop_price, qty, placed_id[:8])
            else:
                logger.error("[LIVE] Missing stop for %s and failed to re-arm", symbol)
    
    def _heal_invalid_position(self, position: Position, current_price: float):
        """Self-heal positions with invalid prices using dynamic asset-class stops."""
        symbol = position.symbol
        
        # Get dynamic risk profile for this asset
        risk_profile = get_risk_profile(symbol)
        stop_pct = risk_profile.stop_loss_pct
        tp_pct = risk_profile.take_profit_pct

        # Ensure time stop is present (some recovered/persisted positions may have it missing/0).
        if not getattr(position, "time_stop_min", 0):
            position.time_stop_min = risk_profile.max_hold_hours * 60
            self.persistence.save_positions(self.positions)
        
        # Fix invalid entry price
        if position.entry_price <= 0:
            logger.warning("[HEAL] %s has invalid entry_price=%.4f, healing with current price $%.4f",
                          symbol, position.entry_price, current_price)
            position.entry_price = current_price
            position.stop_price = current_price * (1 - stop_pct)
            position.tp1_price = current_price * (1 + tp_pct)
            position.tp2_price = current_price * (1 + tp_pct * 1.5)
            position.time_stop_min = risk_profile.max_hold_hours * 60
            position.strategy_id = "healed"
            self.persistence.save_positions(self.positions)
            logger.info("[HEAL] %s (%s): entry=$%.4f, stop=$%.4f (%.1f%%), tp=$%.4f",
                       symbol, risk_profile.tier.value, position.entry_price, 
                       position.stop_price, stop_pct * 100, position.tp1_price)
        
        # Fix invalid stop price
        if position.stop_price <= 0 or position.stop_price >= position.entry_price:
            old_stop = position.stop_price
            position.stop_price = current_price * (1 - stop_pct)
            logger.warning("[HEAL] %s (%s): stop $%.4f -> $%.4f (%.1f%%)",
                          symbol, risk_profile.tier.value, old_stop, position.stop_price, stop_pct * 100)
            self.persistence.save_positions(self.positions)
        
        # Fix missing TP1/TP2 prices (common in legacy or crashed positions)
        if position.tp1_price <= 0 or position.tp2_price <= 0:
            old_tp1 = position.tp1_price
            old_tp2 = position.tp2_price
            if position.tp1_price <= 0:
                position.tp1_price = position.entry_price * (1 + tp_pct)
            if position.tp2_price <= 0:
                position.tp2_price = position.entry_price * (1 + tp_pct * 1.5)
            logger.warning("[HEAL] %s (%s): tp1 $%.4f -> $%.4f, tp2 $%.4f -> $%.4f",
                          symbol, risk_profile.tier.value, old_tp1, position.tp1_price, 
                          old_tp2, position.tp2_price)
            self.persistence.save_positions(self.positions)
    
    def _update_trailing_stop(self, position: Position, pnl_pct: float, current_price: float):
        """Update trailing stop based on profit and regime."""
        symbol = position.symbol

        def _pct_to_percent(v: float) -> float:
            # Config values are typically expressed as fractions (e.g. 0.035 = 3.5%).
            # Normalize to "percent" units to match pnl_pct.
            try:
                fv = float(v)
            except Exception:
                return 0.0
            return fv * 100.0 if fv <= 1.0 else fv

        # Fee-aware breakeven: don't move stop up until the trade is meaningfully net-positive.
        cfg = self.config
        trail_start = _pct_to_percent(getattr(cfg, "trail_start_pct", getattr(settings, "trail_start_pct", 0.035)))
        trail_lock = float(getattr(cfg, "trail_lock_pct", getattr(settings, "trail_lock_pct", 0.50)))
        be_trigger = _pct_to_percent(getattr(cfg, "trail_be_trigger_pct", getattr(settings, "trail_be_trigger_pct", 0.025)))

        use_limit = bool(getattr(cfg, "use_limit_orders", False))
        entry_fee_rate = float(getattr(cfg, "maker_fee_pct", 0.006) if use_limit else getattr(cfg, "taker_fee_pct", 0.012))
        exit_fee_rate = float(getattr(cfg, "taker_fee_pct", 0.012))
        fee_pct_total = (entry_fee_rate + exit_fee_rate) * 100.0
        fee_buffer_pct = float(getattr(settings, "fee_buffer_pct", 0.5))
        fee_aware_profit_pct = fee_pct_total + fee_buffer_pct

        be_trigger = max(be_trigger, fee_aware_profit_pct)
        trail_start = max(trail_start, fee_aware_profit_pct)
        btc_regime = intelligence._market_regime
        
        # Tighten trail in risk-off regime
        if btc_regime == "risk_off" and pnl_pct > 0:
            trail_lock = max(trail_lock, 0.70)
            trail_start = max(fee_aware_profit_pct, trail_start)
            if position.stop_price < position.entry_price:
                position.stop_price = position.entry_price * (1 + fee_aware_profit_pct / 100.0)
                logger.info("[REGIME] %s: BTC RISK_OFF - moving stop to BE", symbol)
                self.persistence.save_positions(self.positions)
        
        # Trail stop if up trail_start%+
        if pnl_pct >= trail_start:
            new_stop = position.entry_price * (1 + pnl_pct * trail_lock / 100)
            if new_stop > position.stop_price:
                old_stop = position.stop_price
                position.stop_price = new_stop
                updated = self.stop_manager.update_stop_price(symbol, new_stop)
                if not updated and self.mode == TradingMode.LIVE:
                    logger.warning("[TRAIL] %s: Failed to update exchange stop to $%.4f", symbol, new_stop)
                logger.info("[TRAIL] %s: Stop raised $%.4f → $%.4f (lock %.1f%%)",
                           symbol, old_stop, new_stop, pnl_pct * trail_lock)
                self.persistence.save_positions(self.positions)
        
        # Move to breakeven if up be_trigger%+
        elif pnl_pct >= be_trigger and position.stop_price < position.entry_price:
            position.stop_price = position.entry_price * (1 + fee_aware_profit_pct / 100.0)
            updated = self.stop_manager.update_stop_price(symbol, position.stop_price)
            if not updated and self.mode == TradingMode.LIVE:
                logger.warning("[TRAIL] %s: Failed to move exchange stop to breakeven $%.4f", symbol, position.stop_price)
            logger.info("[TRAIL] %s: Stop moved to breakeven @ $%.4f", symbol, position.stop_price)
            self.persistence.save_positions(self.positions)
    
    def _evaluate_exit(self, position: Position, current_price: float, pnl_pct: float) -> ExitDecision:
        """Evaluate all exit conditions."""
        symbol = position.symbol
        fee_aware_profit_pct = self._fee_aware_profit_pct()
        
        # Check stop loss
        if position.should_stop(current_price):
            logger.warning("[STOP] %s triggered: price $%.4f <= stop $%.4f (%.1f%%)", 
                          symbol, current_price, position.stop_price, pnl_pct)
            return ExitDecision(True, "stop")
        
        # FORCED STOP (safety fallback only): if stop is missing/invalid and loss is extreme, force close.
        # When a valid stop exists, let the strategy stop handle it.
        if (not position.stop_price) and pnl_pct <= -5.0:
            logger.warning("[FORCED_STOP] %s at %.1f%% loss with missing stop - forcing exit", symbol, pnl_pct)
            return ExitDecision(True, f"forced_stop_{pnl_pct:.1f}%")
        
        # Check TP1 (partial)
        if position.should_tp1(current_price):
            return ExitDecision(True, "tp1", is_partial=True)
        
        # Check TP2 (full)
        if position.should_tp2(current_price):
            return ExitDecision(True, "tp2")
        
        # KNIFEFALL PREVENTION: only consider exhaustion exits once we're meaningfully net-positive.
        # This prevents "green" exits that become losers after fees.
        min_exhaustion_profit_pct = max(4.0, fee_aware_profit_pct + 0.5)
        if pnl_pct >= min_exhaustion_profit_pct:
            exhaustion = self._check_momentum_exhaustion(position, pnl_pct)
            if exhaustion:
                return ExitDecision(True, exhaustion)
        
        # DISABLED: Thesis invalidation was causing early exits
        # Let positions ride to their stop loss based on cap tier
        # if pnl_pct < 0:
        #     thesis_exit = self._check_thesis_invalidation(position, pnl_pct)
        #     if thesis_exit:
        #         return ExitDecision(True, thesis_exit)
        
        # DISABLED: Let positions ride to stop loss or profit
        # Weak confidence exit was causing early losses
        # if position.current_confidence < 15 and pnl_pct < 3.0:
        #     if position.strategy_id not in ("recovered", "healed", "manual"):
        #         return ExitDecision(True, f"weak_confidence: {position.current_confidence:.0f}%")
        
        # Check time stop (if enabled)
        if settings.time_stop_enabled and position.time_stop_min:
            hold_min = position.hold_duration_minutes()
            
            # AGGRESSIVE TIME STOP: If position is WAY over limit (2x+), force close
            # This is a safety net for positions that somehow evaded normal time stops
            if hold_min >= position.time_stop_min * 2:
                logger.warning(
                    "[TIME_STOP] %s FORCE CLOSE: held %d min (limit %d, 2x over)",
                    symbol, hold_min, position.time_stop_min
                )
                return ExitDecision(True, f"time_stop_forced_{hold_min}m")
            
            if hold_min >= position.time_stop_min:
                if pnl_pct >= fee_aware_profit_pct:
                    logger.info("[TIME_STOP] %s profitable exit at %d min", symbol, hold_min)
                    return ExitDecision(True, "time_stop")
                elif hold_min >= position.time_stop_min + 5:
                    logger.info("[TIME_STOP] %s extended exit at %d min (pnl %.1f%%)", 
                               symbol, hold_min, pnl_pct)
                    return ExitDecision(True, "time_stop_extended")
        
        return ExitDecision(False)
    
    def _check_momentum_exhaustion(self, position: Position, pnl_pct: float) -> Optional[str]:
        """Detect momentum exhaustion before knifefall. Exit to lock gains."""
        symbol = position.symbol
        
        # BIG WINNER TRAIL: For +15%+, aggressively lock gains ALWAYS (even without indicators)
        # This runs first to ensure big winners are protected regardless of indicator state
        if pnl_pct >= 15.0:
            lock_pct = 0.85 if pnl_pct >= 25.0 else 0.80
            new_stop = position.entry_price * (1 + pnl_pct * lock_pct / 100)
            if new_stop > position.stop_price:
                old_stop = position.stop_price
                position.stop_price = new_stop
                self.stop_manager.update_stop_price(symbol, new_stop)
                logger.info("[KNIFEFALL] %s: Big winner +%.1f%% - locking %.0f%% gains, stop $%.4f → $%.4f",
                           symbol, pnl_pct, lock_pct * 100, old_stop, new_stop)
                self.persistence.save_positions(self.positions)
        
        try:
            ind = intelligence.get_live_indicators(symbol)
            if not (ind and ind.is_ready):
                return None  # No indicators, but big winner trail already ran above
            
            signals = []
            
            # 1. Volume dying while price still up (distribution)
            buy_pressure = getattr(ind, 'buy_pressure', 0.5)
            if buy_pressure < 0.35:
                signals.append(f"volume_dying:{buy_pressure:.0%}")
            
            # 2. RSI divergence (price higher, RSI lower)
            rsi_div = getattr(ind, 'rsi_divergence', 0)
            if rsi_div == -1:
                signals.append("bearish_divergence")
            
            # 3. MACD histogram shrinking (momentum fading)
            macd_hist = getattr(ind, 'macd_histogram', 0)
            if macd_hist < 0:
                signals.append(f"macd_negative:{macd_hist:.4f}")
            
            # 4. At Bollinger top (extended)
            bb_pos = getattr(ind, 'bb_position', 0.5)
            if bb_pos > 0.92:
                signals.append(f"bb_top:{bb_pos:.0%}")
            
            # 5. RSI overbought
            rsi = getattr(ind, 'rsi_14', 50)
            if rsi > 75:
                signals.append(f"rsi_hot:{rsi:.0f}")
            
            # 6. Order flow turning (bid/ask imbalance negative)
            imbalance = getattr(ind, 'bid_ask_imbalance', 0)
            if imbalance < -0.25:
                signals.append(f"sellers:{imbalance:.0%}")
            
            # BIG WINNER PROTECTION: For +20%+ profits, be more aggressive
            # At +50%, we don't want to give it all back waiting for 2 signals
            signals_needed = 2  # Default: need 2+ signals
            if pnl_pct >= 30.0:
                signals_needed = 1  # +30%: exit on ANY warning sign
            elif pnl_pct >= 20.0:
                signals_needed = 1  # +20%: also 1 signal (big winner protection)
            
            if len(signals) >= signals_needed:
                reason = f"exhaustion:{'+'.join(signals[:3])}"
                logger.info("[KNIFEFALL] %s: Momentum exhaustion detected @ +%.1f%% - %s",
                           symbol, pnl_pct, reason)
                return reason
            
            # Single signal with moderate profit (3-15%) - tighten stop
            # Note: +15%+ already handled at top of function before indicator check
            if len(signals) == 1 and 3.0 <= pnl_pct < 15.0:
                # Aggressive trail - lock 80% of gains
                new_stop = position.entry_price * (1 + pnl_pct * 0.80 / 100)
                if new_stop > position.stop_price:
                    old_stop = position.stop_price
                    position.stop_price = new_stop
                    self.stop_manager.update_stop_price(symbol, new_stop)
                    logger.info("[KNIFEFALL] %s: %s - tightening stop $%.4f → $%.4f",
                               symbol, signals[0], old_stop, new_stop)
                    self.persistence.save_positions(self.positions)
            
        except Exception as e:
            logger.debug("[KNIFEFALL] Error checking %s: %s", symbol, e)
        
        return None
    
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
        pnl_breakdown = self.pnl_engine.calculate_trade_pnl(
            entry_price=position.entry_price,
            exit_price=price,
            qty=close_qty,
            side=position.side,
            realized_pnl=0.0,
        )
        pnl = pnl_breakdown.net_pnl
        
        if self.mode == TradingMode.PAPER:
            logger.info("[PAPER] Partial close %s @ $%.4f (%s)", symbol, price, reason)
            if hasattr(self.portfolio, 'credit'):
                self.portfolio.credit(closed_cost + pnl)
        else:
            await self.execute_live_sell(symbol, close_qty)
            # Wait for order to settle before re-arming stop
            await asyncio.sleep(1.0)
        
        position.realized_pnl += pnl
        position.partial_closed = True
        position.size_qty -= close_qty
        position.size_usd = max(0.0, position.size_usd - closed_cost)
        if getattr(position, "entry_cost_usd", 0.0) > 0:
            position.entry_cost_usd = max(0.0, position.entry_cost_usd - closed_cost)
        
        # Move stop up to a fee-aware "breakeven" on the remaining size
        use_limit = bool(getattr(self.config, "use_limit_orders", False))
        entry_fee_rate = float(getattr(self.config, "maker_fee_pct", 0.006) if use_limit else getattr(self.config, "taker_fee_pct", 0.012))
        exit_fee_rate = float(getattr(self.config, "taker_fee_pct", 0.012))
        fee_pct_total = (entry_fee_rate + exit_fee_rate) * 100.0
        fee_buffer_pct = float(getattr(settings, "fee_buffer_pct", 0.5))
        fee_aware_profit_pct = fee_pct_total + fee_buffer_pct
        position.stop_price = max(position.stop_price, position.entry_price * (1 + fee_aware_profit_pct / 100.0))
        
        # Cancel existing stop first
        cancelled = self.stop_manager.cancel_stop_order(symbol)
        if self.mode == TradingMode.LIVE and not cancelled:
            logger.warning("[STOPS] %s: Failed to cancel existing stop prior to partial-close re-arm", symbol)
        
        # Get fresh remaining qty from exchange for accurate stop placement
        remaining_qty = position.size_qty
        if self.mode == TradingMode.LIVE and self.exchange_sync:
            try:
                # Force refresh holdings to get accurate remaining balance
                self.exchange_sync.refresh_full_portfolio()
                detail = self.exchange_sync.holdings_detail.get(symbol, {})
                available_qty = detail.get('available_qty', 0)
                if available_qty > 0:
                    remaining_qty = min(remaining_qty, available_qty * 0.999)
                    logger.info("[STOPS] %s: Using fresh available qty %.8f for stop", symbol, remaining_qty)
            except Exception as e:
                logger.warning("[STOPS] %s: Failed to get fresh balance: %s", symbol, e)
        
        placed_id = self.stop_manager.place_stop_order(
            symbol=symbol,
            qty=remaining_qty,
            stop_price=position.stop_price
        )

        if self.mode == TradingMode.LIVE and not placed_id:
            logger.warning("[STOPS] %s: Failed to re-arm stop after partial close", symbol)
        
        logger.info("Partial PnL: $%.2f, stop raised to breakeven $%.4f", pnl, position.stop_price)
        self.persistence.save_positions(self.positions)
        
        # Emit event
        pnl_pct = pnl_breakdown.pnl_pct
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
            exit_reason=reason,
            strategy_id=position.strategy_id or ""
        )
        
        self.trade_history.append(result)
        
        # Track strategy PnL
        if position.strategy_id:
            self.pnl_engine.track_strategy_pnl(position.strategy_id, pnl)
        
        # Record in daily stats (skip TEST trades)
        if not symbol.startswith("TEST"):
            self.daily_stats.record_trade(pnl)
            # Also record in session stats
            try:
                from core.session_stats import record_session_trade
                record_session_trade(pnl, pnl > 0)
            except Exception:
                pass
            # Record to strategy registry for per-strategy tracking
            if position.strategy_id:
                try:
                    from core.strategy_registry import get_strategy_registry
                    registry = get_strategy_registry()
                    registry.record_trade(position.strategy_id, pnl, position.hold_duration_minutes())
                except Exception as e:
                    logger.debug("[EXIT] Failed to record to strategy registry: %s", e)
        
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
