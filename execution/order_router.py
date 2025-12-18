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
from core.models import Position, Signal, SignalType, TradeResult
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
from core.helpers import GateReason

from execution.entry_gates import (
    EntryGateChecker,
    PositionSizer,
    calculate_stops,
    validate_rr_ratio,
)
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
        self.daily_stats = DailyStats()
        
        # Extracted modules
        self._circuit_breaker = CircuitBreaker()
        self._cooldown_persistence = CooldownPersistence()
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
    
    def _load_positions(self):
        """Load positions from persistence."""
        self.positions = self.persistence.load_positions()
        self._exchange_sync.prune_dust_positions("load")
        self._exchange_sync.sync_position_stores()
        
        for pos in self.positions.values():
            self.position_registry.add_position(pos)
        
        logger.info("[ORDER] Loaded %d positions", len(self.positions))
    
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
    
    # === Public API ===
    
    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions
    
    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)
    
    def set_candle_collector(self, collector):
        """Set candle collector for warmth checks."""
        self._candle_collector = collector
    
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
    
    async def open_position(self, signal: Signal) -> Optional[Position]:
        """Open a new position based on signal."""
        symbol = signal.symbol
        
        # Prevent race conditions
        if symbol in self._in_flight:
            logger.info("[ORDER] %s already in flight, skipping", symbol)
            return None
        
        self._in_flight.add(symbol)
        try:
            return await self._do_open_position(signal)
        finally:
            self._in_flight.discard(symbol)
    
    async def _do_open_position(self, signal: Signal) -> Optional[Position]:
        """Internal: Execute position open with all gate checks."""
        symbol = signal.symbol
        from core.profiles import is_test_profile
        is_test = is_test_profile(settings.profile)
        
        # Create gate checker
        gate_checker = EntryGateChecker(
            positions=self.positions,
            position_registry=self.position_registry,
            daily_stats=self.daily_stats,
            circuit_breaker=self._circuit_breaker,
            order_cooldown=self._order_cooldown,
            exchange_holdings=self._exchange_sync.exchange_holdings,
            cooldown_seconds=self._cooldown_seconds,
            get_candle_buffer_func=self._get_candle_buffer,
            is_test=is_test,
        )
        
        # Run all gate checks
        gate_result, entry_score = gate_checker.check_all_gates(signal)
        
        if not gate_result.passed:
            self._rejection_tracker.record(gate_result.gate, symbol, gate_result.details)
            return None
        
        logger.info("[INTEL] %s score %.0f/100 ✓ (%s)",
                   symbol, entry_score.total_score, ", ".join(entry_score.reasons[:2]))
        
        # Position sizing
        sizer = PositionSizer(self.positions, self.config)
        pv = self._exchange_sync.portfolio_value or 500.0
        sizing = sizer.calculate_size(entry_score, signal, pv)
        
        logger.info("[TIER] %s: %s bet $%.0f (score:%d, confluence:%d)",
                   symbol, sizing.tier, sizing.size_usd, sizing.score, sizing.confluence)
        
        # Pre-trade validation
        if not self._exchange_sync.validate_before_trade(symbol, self.get_price):
            self._rejection_tracker.record(GateReason.TRUTH, symbol, {"reason": "sync_failed"})
            return None
        
        # Budget check
        has_budget, available = sizer.check_budget(sizing.size_usd, pv, is_test)
        if not has_budget:
            logger.info("[ORDER] Budget limit: need $%.0f, have $%.0f", sizing.size_usd, available)
            self._rejection_tracker.record(GateReason.LIMITS, symbol, {"reason": "budget_exceeded"})
            return None
        
        # Executor check
        can_execute, reason = self.executor.can_execute_order(sizing.size_usd, symbol)
        if not can_execute:
            logger.info("[ORDER] Cannot execute %s: %s", symbol, reason)
            return None
        
        # Calculate stops
        price = signal.price
        is_fast = signal.type == SignalType.FAST_BREAKOUT
        stop_price, tp1_price, tp2_price, time_stop_min = calculate_stops(price, is_fast, self.config)
        
        # Validate R:R
        valid_rr, rr_ratio, rr_reason = validate_rr_ratio(
            price, stop_price, tp1_price, self.config.min_rr_ratio, is_test
        )
        if not valid_rr:
            self._rejection_tracker.record(GateReason.RR, symbol, {"rr_ratio": rr_ratio, "reason": rr_reason})
            return None
        
        # Update signal
        signal.stop_price = stop_price
        signal.tp1_price = tp1_price
        signal.tp2_price = tp2_price
        signal.rr_ratio = rr_ratio
        
        # Set cooldown before order
        self._order_cooldown[symbol] = datetime.now(timezone.utc)
        self._cooldown_persistence.save(self._order_cooldown)
        
        # Execute
        position = await self.executor.open_position(
            symbol=symbol,
            size_usd=sizing.size_usd,
            price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
        )
        
        if position is None:
            return None
        
        position.time_stop_min = time_stop_min
        
        # Register position
        self.position_registry.add_position(position)
        self.positions[symbol] = position
        self.pnl_engine.track_strategy_pnl(position.strategy_id, 0.0)
        self.persistence.save_positions(self.positions)
        
        # Verify in live mode
        if self.mode == TradingMode.LIVE:
            asyncio.create_task(
                self._exchange_sync.verify_position_created(symbol, sizing.size_usd / price)
            )
        
        # Log and alert
        self._log_entry(signal, position, entry_score, sizing, rr_ratio)
        asyncio.create_task(alert_trade_entry(
            symbol=symbol,
            price=position.entry_price,
            size_usd=position.size_usd,
            stop_price=stop_price,
            tp1_price=tp1_price,
        ))
        
        # Emit event
        self._emit_order_event("open", position, position.entry_price, signal.reason)
        
        logger.info("[%s] Opened LONG %s @ $%.4f, size $%.2f",
                   self.mode.value.upper(), symbol, price, sizing.size_usd)
        
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
    
    @property
    def _usd_balance(self) -> float:
        return self._exchange_sync.usd_balance
    
    @property
    def _exchange_holdings(self) -> dict:
        return self._exchange_sync.exchange_holdings
    
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
    
    def _log_entry(self, signal, position, entry_score, sizing, rr_ratio):
        """Log entry for ML training and analysis."""
        ml_data = intelligence.get_live_ml(signal.symbol)
        
        log_trade({
            "ts": utc_iso_str(),
            "type": "order_intent",
            "symbol": signal.symbol,
            "strategy_id": getattr(signal, "strategy_id", "burst_flag"),
            "side": "buy",
            "mode": self.mode.value,
            "entry_price": signal.price,
            "size_usd": sizing.size_usd,
            "stop_price": signal.stop_price,
            "tp1_price": signal.tp1_price,
            "tp2_price": signal.tp2_price,
            "rr_ratio": rr_ratio,
            "score_total": entry_score.total_score,
            "btc_regime": intelligence._market_regime,
            "reason": signal.reason
        })
        
        log_trade({
            "ts": utc_iso_str(),
            "type": "fill",
            "symbol": signal.symbol,
            "side": "buy",
            "price": position.entry_price,
            "qty": position.size_qty
        })
        
        intelligence.record_trade()
    
    def _record_rejection(self, reason, symbol="", details=None):
        """Record gate rejection (delegate to tracker)."""
        self._rejection_tracker.record(reason, symbol, details)
    
    def _update_cached_balances(self):
        """Update cached balances."""
        self._exchange_sync.update_cached_balances()
