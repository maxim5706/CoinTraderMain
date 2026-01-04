"""Order routing coordinator.

Slim coordinator that orchestrates:
- Entry gates (entry_gates.py)
- Exit management (exit_manager.py)  
- Exchange sync (exchange_sync.py)
- Signal batching (signal_batch.py)
- Rebalancing (rebalancer.py)
- Rejection tracking (rejection_tracker.py)

Refactored from 2,230 lines to ~400 lines.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from core.alerts import alert_trade_entry
from core.config import settings
from core.logging_utils import get_logger
from core.logger import log_trade, utc_iso_str
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.models import Intent, OrderRequest, Position, Signal, TradePlan, TradeResult
from core.pnl_engine import PnLEngine
from core.position_registry import PositionRegistry
from core.persistence import sync_with_exchange
from core.trading_container import TradingContainer
from core.trading_interfaces import (
    IExecutor,
    IPortfolioManager,
    IPositionPersistence,
    IStopOrderManager,
)
from core.events import OrderEvent
from core.helpers import GateReason, make_signal_event

from execution.trade_planner import TradePlanner
from execution.exit_manager import ExitManager
from execution.exchange_sync import ExchangeSyncer
from execution.signal_batch import SignalBatcher, process_signal_batch
from execution.rebalancer import Rebalancer
from execution.rejection_tracker import RejectionTracker
from execution.risk import DailyStats, CircuitBreaker, CooldownPersistence
from execution.order_manager import order_manager

from logic.intelligence import intelligence

logger = get_logger(__name__)


class OrderRouter:
    """Coordinates order execution in paper or live mode."""
    
    def __init__(
        self,
        get_price_func,
        state=None,
        *,
        mode: TradingMode | None = None,
        executor: IExecutor | None = None,
        portfolio: IPortfolioManager | None = None,
        persistence: IPositionPersistence | None = None,
        stop_manager: IStopOrderManager | None = None,
        config=None,
        event_bus=None,
    ):
        self.get_price = get_price_func
        self.state = state
        self.mode = mode or ConfigurationManager.get_trading_mode()
        self.config = config or ConfigurationManager.get_config_for_mode(self.mode)
        
        # DI container
        self.container = TradingContainer(self.mode, self.config)
        self.executor = executor or self.container.get_executor()
        self.portfolio = portfolio or self.container.get_portfolio_manager()
        self.persistence = persistence or self.container.get_persistence()
        self.stop_manager = stop_manager or self.container.get_stop_manager()
        self.event_bus = event_bus
        
        # Core components
        self.pnl_engine = PnLEngine(self.config)
        self.position_registry = PositionRegistry(self.config)
        self.positions: dict[str, Position] = {}
        self.daily_stats = DailyStats.load(self.mode)
        
        # Extracted modules
        self._circuit_breaker = CircuitBreaker()
        self._cooldown_persistence = CooldownPersistence(self.mode)
        self._order_cooldown = self._cooldown_persistence.load()
        self._cooldown_seconds = settings.order_cooldown_seconds
        
        # Exchange sync
        self._exchange_sync = ExchangeSyncer(
            mode=self.mode,
            positions=self.positions,
            position_registry=self.position_registry,
            persistence=self.persistence,
            portfolio=self.portfolio,
            config=self.config,
        )
        
        # Rejection tracking
        self._rejection_tracker = RejectionTracker(state)
        
        # Signal batching
        self._signal_batcher = SignalBatcher(batch_window_seconds=30)
        
        # Exit management (initialized after positions loaded)
        self._exit_manager: Optional[ExitManager] = None
        
        # Rebalancer (initialized after positions loaded)
        self._rebalancer: Optional[Rebalancer] = None
        
        # Race condition prevention
        self._in_flight: set[str] = set()
        self._recently_closed: dict[str, datetime] = {}
        
        # Candle collector reference
        self._candle_collector = None
        
        # Initialize
        self._load_positions()
        self._init_submodules()
        
        if self.mode == TradingMode.LIVE:
            self._exchange_sync.init_live_client()
            self._exchange_sync.refresh_full_portfolio()  # Get full Coinbase data
            # Wire up exchange holdings for position registry reconciliation
            self.position_registry.set_exchange_holdings_func(
                lambda: set(self._exchange_sync.holdings_detail.keys())
            )
            # Sync positions from exchange to ensure we track all holdings
            self._sync_positions_from_exchange()
            logger.info("[ORDER] Synced %d positions from exchange", len(self.positions))
    
    def _load_positions(self):
        """Load positions from persistence."""
        loaded = self.persistence.load_positions()
        # Update in place to preserve dict reference for exchange_sync
        self.positions.clear()
        self.positions.update(loaded)
        self._exchange_sync.prune_dust_positions("load")
        self._exchange_sync.sync_position_stores()
        self._exchange_sync.update_cached_balances()
        
        for pos in self.positions.values():
            self.position_registry.add_position(pos)
        
        logger.info("[ORDER] Loaded %d positions", len(self.positions))
    
    def _sync_positions_from_exchange(self):
        """Sync positions from exchange holdings to ensure all are tracked."""
        if not self._exchange_sync._client:
            logger.warning("[SYNC] No client available, skipping exchange sync")
            return
        
        try:
            from core.persistence import sync_with_exchange
            from core.mode_configs import TradingMode
            old_count = len(self.positions)
            # sync_with_exchange modifies positions dict in place
            sync_with_exchange(
                self._exchange_sync._client,
                self.positions,
                quiet=False,
                mode=TradingMode.LIVE
            )
            
            # Sync to registry
            for symbol, pos in self.positions.items():
                if not self.position_registry.has_position(symbol):
                    self.position_registry.add_position(pos)
            
            new_count = len(self.positions)
            if new_count != old_count:
                logger.info("[SYNC] Positions synced from exchange: %d → %d", old_count, new_count)
            self.persistence.save_positions(self.positions)
            
            # Sync existing stop orders from exchange BEFORE health check
            synced_orders = order_manager.sync_with_exchange()
            logger.info("[ORDER] Synced %d orders from exchange", synced_orders)
            
            # Startup health check: verify stops for all positions
            self._startup_stop_health_check()
        except Exception as e:
            logger.error("[SYNC] Failed to sync positions from exchange: %s", e)
    
    def _startup_stop_health_check(self):
        """Verify all live positions have valid stop orders and place missing ones."""
        if self.mode != TradingMode.LIVE:
            return
        
        positions_without_stops = []
        positions_checked = 0
        stops_placed = 0
        stops_failed = []
        
        for symbol, pos in self.positions.items():
            if not pos.stop_price or pos.stop_price <= 0:
                continue
            positions_checked += 1
            
            # Check if stop order exists
            if not order_manager.has_stop_order(symbol):
                positions_without_stops.append(symbol)
        
        if positions_without_stops:
            logger.warning("[STARTUP] %d/%d positions missing stop orders: %s", 
                          len(positions_without_stops), positions_checked,
                          ', '.join(positions_without_stops[:5]))
            
            # IMMEDIATELY place stops for all missing positions
            for symbol in positions_without_stops:
                pos = self.positions.get(symbol)
                if not pos:
                    continue
                
                # Get available qty from exchange
                qty = pos.size_qty
                if self._exchange_sync:
                    detail = self._exchange_sync.holdings_detail.get(symbol, {})
                    available_qty = detail.get('available_qty', 0)
                    if available_qty > 0:
                        qty = min(qty, available_qty * 0.999)
                
                if qty <= 0:
                    logger.warning("[STARTUP] %s: no available qty for stop", symbol)
                    stops_failed.append(symbol)
                    continue
                
                # Place stop order
                placed_id = order_manager.place_stop_order(
                    symbol=symbol,
                    qty=qty,
                    stop_price=pos.stop_price,
                )
                
                if placed_id:
                    pos.stop_order_id = placed_id
                    stops_placed += 1
                    logger.info("[STARTUP] Placed stop for %s @ $%.4f", symbol, pos.stop_price)
                else:
                    stops_failed.append(symbol)
                    logger.warning("[STARTUP] Failed to place stop for %s", symbol)
                
                # Brief pause to avoid rate limits
                import time
                time.sleep(0.3)
            
            # Save positions with new stop_order_ids
            self.persistence.save_positions(self.positions)
            
            if stops_placed > 0:
                logger.info("[STARTUP] Placed %d/%d missing stops", stops_placed, len(positions_without_stops))
            if stops_failed:
                logger.warning("[STARTUP] %d stops failed: %s", len(stops_failed), ', '.join(stops_failed[:5]))
        else:
            logger.info("[STARTUP] Stop health check OK: %d positions verified", positions_checked)
    
    def _init_submodules(self):
        """Initialize extracted submodules with dependencies."""
        # Exit manager
        self._exit_manager = ExitManager(
            mode=self.mode,
            positions=self.positions,
            position_registry=self.position_registry,
            persistence=self.persistence,
            stop_manager=self.stop_manager,
            pnl_engine=self.pnl_engine,
            daily_stats=self.daily_stats,
            order_manager=order_manager,
            portfolio=self.portfolio,
            get_price_func=self.get_price,
            execute_live_sell_func=self._execute_live_sell,
            config=self.config,
            event_bus=self.event_bus,
            exchange_sync=self._exchange_sync,
        )
        self._exit_manager.recently_closed = self._recently_closed
        self._exit_manager.trade_history = []
        
        # Rebalancer
        self._rebalancer = Rebalancer(
            mode=self.mode,
            positions=self.positions,
            position_registry=self.position_registry,
            persistence=self.persistence,
            get_price_func=self.get_price,
            close_full_func=self._exit_manager._close_full,
            execute_live_sell_func=self._execute_live_sell,
            get_portfolio_value_func=lambda: self._exchange_sync.portfolio_value,
            client=self._exchange_sync._client,
        )

    def _record_gate_trace(self, symbol: str, plan_result, signal: Signal | None = None) -> None:
        """Persist the latest gate trace for a symbol into BotState."""
        if not self.state or plan_result is None:
            return
        gate_result = getattr(plan_result, "gate_result", None)
        if gate_result is None:
            return

        trace_entries = []
        for entry in getattr(gate_result, "trace", []) or []:
            if isinstance(entry, dict):
                trace_entries.append(entry)
            else:
                trace_entries.append({
                    "name": getattr(entry, "name", ""),
                    "passed": getattr(entry, "passed", False),
                    "reason": getattr(entry, "reason", ""),
                    "details": getattr(entry, "details", {}),
                })

        blocking = next((g for g in trace_entries if not g.get("passed", False)), None)
        blocking_gate = blocking.get("name") if blocking else ""
        blocking_reason = blocking.get("reason") if blocking else ""
        blocking_category = getattr(gate_result.gate, "value", gate_result.gate) if not gate_result.passed else ""

        entry_score = None
        if getattr(plan_result, "entry_score", None) is not None:
            entry_score = getattr(plan_result.entry_score, "total_score", None)

        payload = {
            "ts": utc_iso_str(),
            "symbol": symbol,
            "passed": gate_result.passed,
            "blocking_gate": blocking_gate,
            "blocking_reason": blocking_reason,
            "blocking_category": blocking_category,
            "entry_score": entry_score,
            "gates": trace_entries,
        }

        if signal is not None:
            payload["spread_bps"] = getattr(signal, "spread_bps", None)

        sizing = getattr(plan_result, "sizing", None)
        if sizing is not None:
            payload["size_usd"] = getattr(sizing, "size_usd", None)
            payload["available_budget"] = getattr(sizing, "available_budget", None)

        self.state.last_gate_trace_by_symbol[symbol] = payload

    def _append_gate_trace(self, symbol: str, name: str, passed: bool, reason: str = "", details: dict | None = None):
        """Append a gate evaluation to the last trace if present."""
        if not self.state:
            return
        trace = self.state.last_gate_trace_by_symbol.get(symbol)
        if not trace:
            return
        gates = trace.get("gates", [])
        gates.append({
            "name": name,
            "passed": passed,
            "reason": reason,
            "details": details or {},
        })
        trace["gates"] = gates
        trace["ts"] = utc_iso_str()
        if not passed:
            trace["passed"] = False
            trace["blocking_gate"] = name
            trace["blocking_reason"] = reason
        self.state.last_gate_trace_by_symbol[symbol] = trace
    
    # === Public API ===
    
    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions
    
    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)
    
    def set_candle_collector(self, collector):
        """Set candle collector for warmth checks."""
        self._candle_collector = collector

    def update_config(self, config) -> None:
        """Update running config for router and dependent components."""
        self.config = config
        self.container.update_config(config)
        if hasattr(self.executor, "update_config"):
            self.executor.update_config(config)
        elif hasattr(self.executor, "config"):
            self.executor.config = config
        if hasattr(self.portfolio, "update_config"):
            self.portfolio.update_config(config)
        elif hasattr(self.portfolio, "config"):
            self.portfolio.config = config
        if hasattr(self.stop_manager, "config"):
            self.stop_manager.config = config
        if hasattr(self.pnl_engine, "config"):
            self.pnl_engine.config = config
        if hasattr(self.position_registry, "update_config"):
            self.position_registry.update_config(config)
        if hasattr(self._exchange_sync, "config"):
            self._exchange_sync.config = config
        if self._exit_manager and hasattr(self._exit_manager, "config"):
            self._exit_manager.config = config
        self._cooldown_seconds = settings.order_cooldown_seconds
    
    def _get_candle_buffer(self, symbol: str):
        """Get candle buffer for a symbol."""
        if self._candle_collector:
            return self._candle_collector.get_buffer(symbol)
        return None
    
    # === Signal Batching ===
    
    def add_signal_to_batch(self, signal: Signal, features: dict = None):
        """Add signal to batch buffer for ranked execution."""
        self._signal_batcher.add_signal(signal, features)
    
    async def process_signal_batch(self) -> int:
        """Process buffered signals in ranked order."""
        return await process_signal_batch(
            self._signal_batcher,
            self.open_position,
            self.positions,
            max_new_positions=3
        )
    
    # === Entry ===
    
    async def open_position(self, signal: Signal | Intent) -> Optional[Position]:
        """Open a new position based on a signal or intent."""
        symbol = signal.symbol if hasattr(signal, "symbol") else ""
        
        # Prevent race conditions
        if symbol in self._in_flight:
            logger.info("[ORDER] %s already in flight, skipping", symbol)
            return None
        
        self._in_flight.add(symbol)
        try:
            return await self._do_open_position(signal)
        finally:
            self._in_flight.discard(symbol)
    
    async def _do_open_position(self, signal: Signal | Intent) -> Optional[Position]:
        """Internal: Execute position open with all gate checks."""
        if isinstance(signal, Intent):
            intent = signal
            symbol = intent.symbol
        else:
            intent = Intent.from_signal(signal)
            symbol = signal.symbol
        from core.profiles import is_test_profile
        is_test = is_test_profile(settings.profile)

        # CRITICAL: Refresh exchange holdings AND sync positions before entry
        # This ensures stacking check has accurate data about existing positions
        if self.mode == TradingMode.LIVE and self._exchange_sync._client:
            try:
                self._exchange_sync.refresh_full_portfolio()
                # Also sync positions dict with exchange to catch any orphaned positions
                # This ensures stacking can find positions that exist on exchange
                sync_with_exchange(self._exchange_sync._client, self.positions, quiet=True)
            except Exception as e:
                logger.warning("[ORDER] Pre-entry sync failed: %s", e)

        planner = TradePlanner(
            positions=self.positions,
            position_registry=self.position_registry,
            daily_stats=self.daily_stats,
            circuit_breaker=self._circuit_breaker,
            order_cooldown=self._order_cooldown,
            exchange_holdings=self._exchange_sync.exchange_holdings,
            cooldown_seconds=self._cooldown_seconds,
            get_candle_buffer_func=self._get_candle_buffer,
            exchange_sync=self._exchange_sync,
            config=self.config,
            is_test=is_test,
        )

        pv = self._exchange_sync.portfolio_value or 500.0
        plan_result = planner.plan_trade(intent, pv, self.get_price)
        self._record_gate_trace(symbol, plan_result, signal)
        if not plan_result.plan:
            gate_result = plan_result.gate_result
            if gate_result:
                self._rejection_tracker.record(gate_result.gate, symbol, gate_result.details)
            return None

        plan: TradePlan = plan_result.plan
        entry_score = plan_result.entry_score
        sizing = plan_result.sizing

        if entry_score:
            logger.info(
                "[INTEL] %s score %.0f/100 ✓ (%s)",
                symbol,
                entry_score.total_score,
                ", ".join(entry_score.reasons[:2]),
            )

        if sizing:
            logger.info(
                "[TIER] %s: %s bet $%.0f (score:%d, confluence:%d)",
                symbol,
                sizing.tier,
                sizing.size_usd,
                sizing.score,
                sizing.confluence,
            )

        order_request = OrderRequest.from_plan(plan)

        # Executor check
        can_execute, reason = self.executor.can_execute_order(order_request.size_usd, symbol)
        if not can_execute:
            self._append_gate_trace(
                symbol,
                "executor_can_execute",
                False,
                reason,
                {"size_usd": order_request.size_usd},
            )
            logger.info("[ORDER] Cannot execute %s: %s", symbol, reason)
            return None
        
        # Set cooldown before order
        self._order_cooldown[symbol] = datetime.now(timezone.utc)
        self._cooldown_persistence.save(self._order_cooldown)
        
        # Check if this is a stack (add to existing position)
        existing_position = self.positions.get(symbol)
        is_stack = existing_position is not None
        
        # Execute
        position = await self.executor.open_position(
            symbol=symbol,
            size_usd=order_request.size_usd,
            price=order_request.price,
            stop_price=order_request.stop_price,
            tp1_price=order_request.tp1_price,
            tp2_price=order_request.tp2_price,
        )
        
        if position is None:
            return None
        
        # Handle stacking - merge into existing position
        if is_stack and existing_position:
            # Calculate new average entry price
            old_cost = existing_position.entry_cost_usd or (existing_position.entry_price * existing_position.size_qty)
            new_cost = position.entry_price * position.size_qty
            total_qty = existing_position.size_qty + position.size_qty
            new_avg_price = (old_cost + new_cost) / total_qty if total_qty > 0 else position.entry_price
            
            # Update existing position
            existing_position.size_qty = total_qty
            existing_position.size_usd = total_qty * position.entry_price
            existing_position.entry_cost_usd = old_cost + new_cost
            existing_position.entry_price = new_avg_price
            existing_position.stack_count += 1
            
            # Move stop up to protect profit (breakeven on original)
            # New stop = original entry price (locks in original cost)
            old_entry = old_cost / (total_qty - position.size_qty) if (total_qty - position.size_qty) > 0 else existing_position.entry_price
            existing_position.stop_price = max(existing_position.stop_price, old_entry * 0.995)  # Just below original entry
            
            logger.info("[STACK] %s: Added %.2f USD, total qty=%.6f, avg_entry=$%.4f, stop moved to $%.4f",
                       symbol, order_request.size_usd, total_qty, new_avg_price, existing_position.stop_price)
            
            # Update stop on exchange
            if self.mode == TradingMode.LIVE and hasattr(self, '_exit_manager') and self._exit_manager:
                self._exit_manager._ensure_stop_order(symbol, existing_position)
            
            position = existing_position
        else:
            position.time_stop_min = plan.time_stop_min
            # Set tier and source_strategy from plan for proper tagging
            position.tier = getattr(plan, "tier_code", "normal")
            position.entry_score = plan.entry_score
            position.source_strategy = getattr(signal, "strategy_id", "") or position.strategy_id
            # Register new position
            self.position_registry.add_position(position)
            self.positions[symbol] = position
        self.pnl_engine.track_strategy_pnl(position.strategy_id, 0.0)
        self.persistence.save_positions(self.positions)

        # Track in recent signals stream as a TAKEN event (only once the position is actually opened).
        # This keeps the dashboard feed consistent with real execution.
        if self.state is not None:
            try:
                evt = make_signal_event(
                    datetime.now(timezone.utc),
                    symbol,
                    getattr(signal, "strategy_id", "entry") or "entry",
                    int(getattr(entry_score, "total_score", 0) or 0),
                    float(plan_result.gate_result.details.get("spread_bps", 0.0) if plan_result.gate_result.details else 0.0),
                    True,
                    plan.intent.reason or "opened",
                )
                self.state.recent_signals.appendleft(evt)
            except Exception:
                pass
        
        # Verify in live mode
        if self.mode == TradingMode.LIVE:
            asyncio.create_task(
                self._exchange_sync.verify_position_created(symbol, plan.size_usd / plan.intent.price)
            )
        
        # Log and alert
        self._log_entry(plan, position, entry_score, sizing)
        asyncio.create_task(alert_trade_entry(
            symbol=symbol,
            price=position.entry_price,
            size_usd=position.size_usd,
            stop_price=plan.stop_price,
            score=getattr(entry_score, "total_score", 0) if entry_score else 0,
        ))
        
        # Emit event
        self._emit_order_event("open", position, position.entry_price, plan.intent.reason)
        
        logger.info("[%s] Opened LONG %s @ $%.4f, size $%.2f",
                   self.mode.value.upper(), symbol, plan.intent.price, plan.size_usd)
        
        return position
    
    # === Exit ===
    
    async def check_exits(self, symbol: str) -> Optional[TradeResult]:
        """Check if position should exit."""
        return await self._exit_manager.check_exits(symbol)
    
    def update_position_confidence(self, symbol: str):
        """Update confidence tracking for position."""
        self._exit_manager.update_position_confidence(symbol)
    
    def update_all_position_confidence(self):
        """Update confidence for all positions."""
        self._exit_manager.update_all_position_confidence()
    
    # === Scaling Into Winners ===
    
    async def scale_into_winners(self) -> list:
        """
        Add to winning positions that show strong 1m/1h momentum.
        Only scales if:
        - Position is profitable (>1% gain)
        - Strong 1m/1h momentum (>2% trend)
        - Have available budget
        - Position hasn't been scaled recently (5min cooldown)
        """
        scaled = []
        
        # Check available budget
        pv = self._exchange_sync.portfolio_value or 500.0
        current_exposure = sum(p.size_usd for p in self.positions.values())
        max_exposure = pv * settings.portfolio_max_exposure_pct
        available = max_exposure - current_exposure
        
        if available < settings.scout_trade_usd:
            return scaled  # Not enough budget to scale
        
        for symbol, position in list(self.positions.items()):
            try:
                # Check if profitable (>1%)
                current_price = self.get_price(symbol)
                if not current_price:
                    continue
                    
                pnl_pct = ((current_price / position.entry_price) - 1) * 100
                if pnl_pct < 1.0:
                    continue  # Only scale winners
                
                # Check 1m/1h momentum from features
                buffer = self._get_candle_buffer(symbol) if self._get_candle_buffer else None
                if not buffer:
                    continue
                
                # Get momentum indicators (handle None values)
                trend_1m = buffer.roc(1) if hasattr(buffer, 'roc') else 0
                trend_1h = buffer.roc(60) if hasattr(buffer, 'roc') else 0
                trend_1m = trend_1m if trend_1m is not None else 0
                trend_1h = trend_1h if trend_1h is not None else 0
                
                # Require strong momentum (>2% on either timeframe)
                if trend_1m < 2.0 and trend_1h < 2.0:
                    continue
                
                # Check scale cooldown (5 min)
                scale_cooldown = getattr(position, '_last_scale_time', None)
                if scale_cooldown:
                    from datetime import datetime, timezone
                    elapsed = (datetime.now(timezone.utc) - scale_cooldown).total_seconds()
                    if elapsed < 300:  # 5 min cooldown
                        continue
                
                # Calculate scale size (50% of original, capped by available)
                scale_size = min(position.size_usd * 0.5, available, settings.normal_trade_usd)
                if scale_size < 5.0:
                    continue
                
                # Execute scale (add to position)
                logger.info("[SCALE] Adding $%.0f to winner %s (pnl: +%.1f%%, 1m: %.1f%%, 1h: %.1f%%)",
                           scale_size, symbol, pnl_pct, trend_1m, trend_1h)
                
                # Update position size and mark scaled
                position.size_usd += scale_size
                position._last_scale_time = datetime.now(timezone.utc)
                available -= scale_size
                
                scaled.append({
                    'symbol': symbol,
                    'added': scale_size,
                    'new_size': position.size_usd,
                    'pnl_pct': pnl_pct,
                    'trend_1m': trend_1m,
                    'trend_1h': trend_1h
                })
                
                if available < settings.scout_trade_usd:
                    break  # Out of budget
                    
            except Exception as e:
                logger.warning("[SCALE] Error scaling %s: %s", symbol, e)
                continue
        
        return scaled
    
    # === Rebalancing ===
    
    async def force_close_all(self, reason: str = "manual"):
        """Force close all positions."""
        return await self._rebalancer.force_close_all(reason)
    
    async def auto_rebalance(self, target_available_pct: float = 0.3) -> float:
        """Auto-rebalance to free up budget."""
        return await self._rebalancer.auto_rebalance(target_available_pct)
    
    async def trim_largest(self, amount_usd: float = 50.0) -> Optional[TradeResult]:
        """Trim largest position by dollar amount."""
        return await self._rebalancer.trim_largest(amount_usd)
    
    # === Properties ===
    
    @property
    def trade_history(self) -> list:
        return self._exit_manager.trade_history if self._exit_manager else []
    
    @property
    def _portfolio_value(self) -> float:
        return self._exchange_sync.portfolio_value
    
    @property
    def _portfolio_snapshot(self):
        return self._exchange_sync.portfolio_snapshot
    
    @_portfolio_snapshot.setter
    def _portfolio_snapshot(self, value):
        self._exchange_sync._portfolio_snapshot = value
    
    @property
    def _last_snapshot_at(self):
        return getattr(self._exchange_sync, '_last_snapshot_at', None)
    
    @property
    def _usd_balance(self) -> float:
        return self._exchange_sync.usd_balance
    
    @property
    def _available_balance(self) -> float:
        """What's actually available to trade."""
        return self._exchange_sync.available_balance
    
    @property
    def _total_unrealized_pnl(self) -> float:
        return self._exchange_sync.total_unrealized_pnl
    
    @property
    def _exchange_holdings(self) -> dict:
        return self._exchange_sync.exchange_holdings
    
    @property
    def _holdings_detail(self) -> dict:
        """Full details per holding from Coinbase."""
        return self._exchange_sync.holdings_detail
    
    # === Internal Helpers ===
    
    async def _execute_live_sell(self, symbol: str, qty: float):
        """Execute a live sell order."""
        client = self._exchange_sync._client
        if client is None:
            return
        
        try:
            # Cancel stop orders first
            try:
                open_orders = client.list_orders(product_id=symbol, order_status=["OPEN"])
                for order in getattr(open_orders, "orders", []) or []:
                    order_id = getattr(order, "order_id", None) or order.get("order_id")
                    if order_id:
                        client.cancel_orders(order_ids=[order_id])
                        logger.info("[LIVE] Cancelled stop order %s", order_id)
            except Exception as e:
                logger.warning("[LIVE] Could not cancel stop orders: %s", e)
            
            # Market sell
            order = client.market_order_sell(
                client_order_id=f"ct_sell_{symbol}_{int(datetime.now().timestamp())}",
                product_id=symbol,
                base_size=str(qty)
            )
            
            success = getattr(order, "success", None) or (order.get("success") if isinstance(order, dict) else True)
            if success or order:
                self._circuit_breaker.record_success()
                logger.info("[LIVE] Sold %s, qty %.6f", symbol, qty)
            else:
                self._circuit_breaker.record_failure()
                logger.error("[LIVE] Sell order failed: %s", order)
                
        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.error("[LIVE] Error selling: %s", e, exc_info=True)
    
    def _emit_order_event(self, event_type: str, position: Position, price: float, reason: str):
        """Emit order event to event bus."""
        if not self.event_bus:
            return
        
        event = OrderEvent(
            event_type=event_type,
            symbol=position.symbol,
            side=position.side,
            mode=self.mode.value,
            strategy_id=position.strategy_id or "",
            price=price,
            size_usd=position.size_usd,
            size_qty=position.size_qty,
            reason=reason,
        )
        self.event_bus.emit_order(event)
    
    def _log_entry(self, plan: TradePlan, position, entry_score, sizing):
        """Log entry for ML training and analysis."""
        ml_data = intelligence.get_live_ml(plan.intent.symbol)
        
        log_trade({
            "ts": utc_iso_str(),
            "type": "order_intent",
            "symbol": plan.intent.symbol,
            "strategy_id": plan.intent.strategy_id or "burst_flag",
            "side": plan.intent.side.value,
            "mode": self.mode.value,
            "entry_price": plan.intent.price,
            "size_usd": plan.size_usd,
            "stop_price": plan.stop_price,
            "tp1_price": plan.tp1_price,
            "tp2_price": plan.tp2_price,
            "rr_ratio": plan.rr_ratio,
            "score_total": getattr(entry_score, "total_score", 0),
            "btc_regime": intelligence._market_regime,
            "reason": plan.intent.reason,
            "correlation_id": plan.correlation_id,
            "plan_version": plan.version,
            "plan_meta": plan.metadata,
        })
        
        log_trade({
            "ts": utc_iso_str(),
            "type": "fill",
            "symbol": plan.intent.symbol,
            "side": plan.intent.side.value,
            "price": position.entry_price,
            "qty": position.size_qty,
            "correlation_id": plan.correlation_id,
        })
        
        intelligence.record_trade()
    
    def _record_rejection(self, reason, symbol="", details=None):
        """Record gate rejection (delegate to tracker)."""
        self._rejection_tracker.record(reason, symbol, details)
    
    def _update_cached_balances(self):
        """Update cached balances."""
        self._exchange_sync.update_cached_balances()
