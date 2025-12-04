"""Order routing with paper trading and live execution."""

import asyncio
import time
from dataclasses import field
from datetime import datetime, timezone
from typing import Optional

from datafeeds.universe import tier_scheduler
from services.alerts import alert_error, alert_trade_entry, alert_trade_exit
from core.config import settings
from core.logging_utils import get_logger
from core.logger import log_trade, utc_iso_str
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.models import Position, PositionState, Side, Signal, SignalType, TradeResult
from core.pnl_engine import PnLEngine
from core.position_registry import PositionRegistry
import core.persistence as persistence_backend
from core.persistence import sync_with_exchange
from core.portfolio import PortfolioSnapshot, portfolio_tracker
from core.trading_container import TradingContainer
from core.trading_interfaces import (
    IExecutor,
    IPortfolioManager,
    IPositionPersistence,
    IStopOrderManager,
)
from core.events import OrderEvent
from execution.order_manager import order_manager
from execution.order_utils import (
    OrderFatalError,
    OrderResult,
    calculate_limit_buy_price,
    parse_order_response,
    rate_limiter,
)
from logic.intelligence import EntryScore, intelligence
from trading.risk import DailyStats, CircuitBreaker, CooldownPersistence

logger = get_logger(__name__)


class OrderRouter:
    """Handles order execution in paper or live mode."""
    
    # Policy for unfilled limit orders:
    # - "cancel_skip": cancel and skip trade (default, safest)
    # - "market_fallback": cancel + market buy (not enabled)
    LIMIT_NO_FILL_POLICY = "cancel_skip"
    
    # Fees from config (Intro 1 tier by default)
    # Taker = market orders, Maker = limit orders
    @property
    def TAKER_FEE_PCT(self) -> float:
        return settings.taker_fee_pct  # 1.2% for Intro 1
    
    @property  
    def MAKER_FEE_PCT(self) -> float:
        return settings.maker_fee_pct  # 0.6% for Intro 1
    
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
        """
        Args:
            get_price_func: Callable that takes symbol and returns current price
        """
        self.get_price = get_price_func
        self.state = state  # Optional BotState for rejection counters
        self.mode = mode or ConfigurationManager.get_trading_mode()
        self.config = config or ConfigurationManager.get_config_for_mode(self.mode)
        self.container = TradingContainer(self.mode, self.config)
        self.executor = executor or self.container.get_executor()
        self.portfolio = portfolio or self.container.get_portfolio_manager()
        self.persistence = persistence or self.container.get_persistence()
        self.stop_manager = stop_manager or self.container.get_stop_manager()
        self.event_bus = event_bus

        # Initialize new unified components (keeping existing interface)
        self.pnl_engine = PnLEngine(self.config)
        self.position_registry = PositionRegistry(self.config)
        
        # Keep existing names but backed by new registry
        self.positions: dict[str, Position] = {}  # Will sync with position_registry
        self.trade_history: list[TradeResult] = []
        self.daily_stats = DailyStats()
        self._client = getattr(self.portfolio, "client", None)
        self._product_info: dict[str, dict] = {}  # Cache product minimums
        self._usd_balance: float = 0.0
        self._order_cooldown: dict[str, datetime] = {}  # Prevent duplicate orders
        self._cooldown_seconds = settings.order_cooldown_seconds
        self._in_flight: set[str] = set()  # Symbols with orders in progress (race condition prevention)
        self._symbol_exposure: dict[str, float] = {}  # Track total exposure per symbol
        self._portfolio_value: float = 0.0  # Tracked portfolio value
        self._exchange_holdings: dict[str, float] = {}  # Actual holdings from exchange
        self._portfolio_snapshot: Optional[PortfolioSnapshot] = None  # Real portfolio from Coinbase
        self._last_snapshot_at: Optional[datetime] = None  # Timestamp of last portfolio snapshot
        self._sync_degraded: bool = False  # If True, block new entries until snapshot succeeds
        self._last_stop_check: dict[str, datetime] = {}  # Rate-limit stop health checks
        self._recently_closed: dict[str, datetime] = {}  # Prevent re-adding positions we just closed
        
        # Circuit breaker for API failures
        self._circuit_breaker = CircuitBreaker()
        
        # Persistent cooldowns (survives restarts)
        self._cooldown_persistence = CooldownPersistence(self.mode)
        self._order_cooldown = self._cooldown_persistence.load()

        if hasattr(self.stop_manager, "bind_client") and self._client:
            try:
                self.stop_manager.bind_client(self._client)  # type: ignore[attr-defined]
            except Exception:
                pass

        # Refresh balances and load positions
        try:
            self.portfolio.update_portfolio_state()
        except Exception:
            pass
        self._usd_balance = self.portfolio.get_available_balance()
        self._portfolio_value = self.portfolio.get_total_portfolio_value()
        self._portfolio_snapshot = getattr(self.portfolio, "portfolio_snapshot", None)
        self._exchange_holdings = getattr(self.portfolio, "exchange_holdings", {})

        self.positions = self.persistence.load_positions()
        
        # Sync loaded positions into new registry
        for symbol, position in self.positions.items():
            self.position_registry.add_position(position)

        if self.mode == TradingMode.LIVE and self._client:
            order_manager.init_client(self._client)
            order_manager.sync_with_exchange()
            self.positions = persistence_backend.sync_with_exchange(self._client, self.positions, quiet=False)
            # Sync exchange positions to registry (ensures both stores match)
            for symbol, position in self.positions.items():
                if symbol not in self.position_registry.get_all_positions():
                    self.position_registry.add_position(position)
            total_cost = sum(p.cost_basis for p in self.positions.values())
            total_value = sum(p.size_usd for p in self.positions.values())
            logger.info(
                "[SYNC] Total: %s positions, cost $%s, value $%s",
                len(self.positions),
                f"{total_cost:.0f}",
                f"{total_value:.0f}",
            )
        if self.mode == TradingMode.PAPER and self.state:
            self.state.paper_balance_usd = self._usd_balance
            self.state.paper_balance = self._usd_balance
            self.state.live_balance_usd = 0.0
        self._update_cached_balances()
        self._sync_position_stores()
    
    def _sync_position_stores(self):
        """Keep self.positions dict in sync with position_registry."""
        # This maintains backward compatibility while using new registry
        registry_positions = self.position_registry.get_all_positions()
        
        # Update self.positions to match registry
        self.positions.clear()
        self.positions.update(registry_positions)
    
    def _verify_exchange_truth(self) -> bool:
        """Verify our local positions match exchange reality."""
        if self.mode != TradingMode.LIVE:
            return True  # Paper mode always in sync
        
        try:
            snapshot = None
            # Prefer fresh cached snapshot to avoid repeated API hits
            if self._snapshot_is_fresh():
                snapshot = self._portfolio_snapshot
            elif hasattr(self.portfolio, "get_snapshot"):
                snapshot = self.portfolio.get_snapshot()
                if snapshot:
                    self._portfolio_snapshot = snapshot
                    self._last_snapshot_at = datetime.now(timezone.utc)
                    self._sync_degraded = False
            if not snapshot:
                logger.warning("[TRUTH] Could not get exchange snapshot")
                self._sync_degraded = True
                return False
            
            # Compare positions
            exchange_positions = {
                pos.symbol: pos for pos in snapshot.positions.values() 
                if not pos.is_cash and pos.value_usd >= settings.position_min_usd
            }
            
            local_symbols = set(self.positions.keys())
            exchange_symbols = set(exchange_positions.keys())
            
            # Check for drift
            missing_local = exchange_symbols - local_symbols
            extra_local = local_symbols - exchange_symbols
            
            if missing_local or extra_local:
                logger.error(
                    "[TRUTH] Position drift detected! Missing local: %s, Extra local: %s",
                    missing_local, extra_local
                )
                
                # Attempt automatic reconciliation
                return self._recover_from_drift(exchange_positions)
            
            # Verify quantities for existing positions
            for symbol in local_symbols & exchange_symbols:
                local_pos = self.positions[symbol]
                exchange_pos = exchange_positions[symbol]
                
                # Allow small differences due to rounding
                qty_diff = abs(local_pos.size_qty - exchange_pos.qty)
                if qty_diff > settings.position_qty_drift_tolerance:
                    logger.warning(
                        "[TRUTH] Quantity drift for %s: Local=%.8f, Exchange=%.8f",
                        symbol, local_pos.size_qty, exchange_pos.qty
                    )
            
            logger.debug("[TRUTH] Exchange sync verified ✓")
            return True
            
        except Exception as e:
            logger.error("[TRUTH] Exchange verification failed: %s", e)
            self._sync_degraded = True
            return False
    
    def _recover_from_drift(self, exchange_positions: dict) -> bool:
        """Recover from position drift by syncing with exchange."""
        logger.info("[TRUTH] Attempting drift recovery...")
        
        try:
            # Clear local positions
            self.positions.clear()
            self.position_registry = PositionRegistry(self.config)
            
            # Rebuild from exchange truth
            for symbol, exchange_pos in exchange_positions.items():
                if exchange_pos.value_usd >= settings.position_min_usd:
                    
                    # Create position from exchange data
                    from core.models import Position, Side
                    
                    position = Position(
                        symbol=symbol,
                        side=Side.BUY,  # Assume long positions
                        entry_price=exchange_pos.entry_price,
                        entry_time=datetime.now(timezone.utc),  # Unknown, use now
                        size_usd=exchange_pos.value_usd,
                        size_qty=exchange_pos.qty,
                        stop_price=exchange_pos.entry_price * (1 - settings.fixed_stop_pct),
                        tp1_price=exchange_pos.entry_price * (1 + settings.tp1_pct),
                        tp2_price=exchange_pos.entry_price * (1 + settings.tp2_pct),
                        entry_cost_usd=exchange_pos.cost_basis,
                        strategy_id="recovered"  # Mark as recovered
                    )
                    
                    # Add to both stores
                    self.positions[symbol] = position
                    self.position_registry.add_position(position)
            
            # Save recovered positions
            self.persistence.save_positions(self.positions)
            
            logger.info("[TRUTH] Recovery complete: %d positions restored", len(self.positions))
            return True
            
        except Exception as e:
            logger.error("[TRUTH] Recovery failed: %s", e)
            return False
    
    def _validate_before_trade(self, symbol: str) -> bool:
        """Validate system state before placing any trade."""
        
        # If we recently failed to sync truth, try to recover but don't block forever
        if self.mode == TradingMode.LIVE and self._sync_degraded:
            # Try to refresh portfolio one more time
            try:
                self._update_balance_cache()
            except Exception:
                pass
            
            # If still degraded but we have SOME positions data, allow with warning
            if self._sync_degraded and self._portfolio_value > 100:
                logger.warning("[TRUTH] Sync degraded but allowing trade (portfolio=$%.0f)", self._portfolio_value)
                self._sync_degraded = False  # Reset to allow trading
            elif self._sync_degraded:
                logger.info("[TRUTH] Trading paused - no valid portfolio data")
                self._record_rejection("truth", symbol, {"reason": "sync_degraded"})
                return False
        
        # Check exchange sync - but don't block if we have valid local state
        if not self._verify_exchange_truth():
            # If we have positions and portfolio value, allow trading with warning
            if len(self.positions) > 0 and self._portfolio_value > 100:
                logger.warning("[TRUTH] Exchange sync failed but local state valid - allowing trade")
            else:
                logger.error("[TRUTH] Cannot trade - exchange sync failed and no local state")
                return False
        
        # Verify we have current price data
        current_price = self.get_price(symbol)
        if current_price <= 0:
            logger.error("[TRUTH] Cannot trade %s - no price data", symbol)
            return False
        
        # Check if we already have this position on exchange
        if self.mode == TradingMode.LIVE and hasattr(self.portfolio, 'get_snapshot'):
            snapshot = self.portfolio.get_snapshot()
            if snapshot:
                exchange_position = snapshot.positions.get(symbol)
                if exchange_position and exchange_position.value_usd >= settings.position_min_usd:
                    logger.warning("[TRUTH] %s position already exists on exchange", symbol)
                    return False
        
        return True
    
    async def _verify_position_created(self, symbol: str, expected_qty: float):
        """Verify that a position was actually created on the exchange."""
        await asyncio.sleep(2)  # Give exchange time to update
        
        try:
            if self.mode == TradingMode.LIVE and hasattr(self.portfolio, 'get_snapshot'):
                snapshot = self.portfolio.get_snapshot()
                if snapshot:
                    exchange_position = snapshot.positions.get(symbol)
                    if exchange_position and exchange_position.qty >= expected_qty * settings.position_verify_tolerance:
                        logger.info("[TRUTH] ✓ Position verified on exchange: %s %.6f", 
                                  symbol, exchange_position.qty)
                    else:
                        logger.warning("[TRUTH] ⚠ Position NOT found on exchange: %s", symbol)
                        # Mark position as problematic
                        if symbol in self.positions:
                            self.positions[symbol].strategy_id += "_UNVERIFIED"
                            self.persistence.save_positions(self.positions)
        except Exception as e:
            logger.error("[TRUTH] Position verification failed for %s: %s", symbol, e)
    
    def _init_live_client(self):
        """Initialize Coinbase client for live trading."""
        try:
            from coinbase.rest import RESTClient
            self._client = RESTClient(
                api_key=settings.coinbase_api_key,
                api_secret=settings.coinbase_api_secret
            )
            logger.info("[ORDER] Live client initialized")
        except Exception as e:
            logger.error("[ORDER] Failed to init live client: %s", e, exc_info=True)
            self._client = None
    
    def _refresh_balance(self):
        """Refresh USD + USDC balance and calculate total portfolio value."""
        if not self._client:
            return
        try:
            accounts = self._client.get_accounts()
            usd_bal = 0.0
            usdc_bal = 0.0
            holdings_value = 0.0
            
            for acct in getattr(accounts, 'accounts', []):
                currency = getattr(acct, 'currency', '')
                bal = getattr(acct, 'available_balance', {})
                value = float(bal.get('value', 0) if isinstance(bal, dict) else getattr(bal, 'value', 0))
                
                if currency == 'USD':
                    usd_bal = value
                elif currency == 'USDC':
                    usdc_bal = value
                elif value > settings.position_dust_usd:
                    # Estimate value of holdings and track them
                    symbol = f"{currency}-USD"
                    
                    # Skip ignored/delisted coins (avoid 404 errors)
                    if symbol in settings.ignored_symbol_set:
                        continue
                    
                    try:
                        product = self._client.get_product(symbol)
                        price = float(getattr(product, 'price', 0))
                        position_value = value * price
                        holdings_value += position_value
                        # Track for blocking new buys
                        if position_value >= settings.position_min_usd:
                            self._exchange_holdings[symbol] = position_value
                    except Exception as e:
                        logger.warning("[ORDER] Failed to value holding %s: %s", symbol, e, exc_info=True)
                        self._sync_degraded = True
            
            # Calculate totals
            self._usd_balance = usd_bal + usdc_bal
            self._portfolio_value = self._usd_balance + holdings_value
            
            # Require a believable balance; if missing, pause new entries
            if self._portfolio_value < 50:
                logger.error(
                    "[ORDER] API balance low ($%s) - marking sync degraded and blocking new trades",
                    f"{self._portfolio_value:.2f}",
                )
                self._sync_degraded = True
                return
            else:
                # Balance looks sane again
                self._sync_degraded = False
            
            logger.info(
                "[ORDER] Portfolio: $%s (Cash: $%s, Holdings: $%s)",
                f"{self._portfolio_value:.2f}",
                f"{self._usd_balance:.2f}",
                f"{holdings_value:.2f}",
            )
            
            # Get REAL portfolio snapshot from Coinbase Portfolio API
            try:
                self._portfolio_snapshot = portfolio_tracker.get_snapshot()
                if self._portfolio_snapshot:
                    self._last_snapshot_at = datetime.now(timezone.utc)
                    self._sync_degraded = False
                    logger.info(
                        "[ORDER] Real P&L: $%s (%s positions)",
                        f"{self._portfolio_snapshot.total_unrealized_pnl:+.2f}",
                        self._portfolio_snapshot.position_count,
                    )
            except Exception as pe:
                logger.warning("[ORDER] Portfolio snapshot failed: %s", pe, exc_info=True)
                
        except Exception as e:
            logger.error("[ORDER] Balance check failed: %s", e, exc_info=True)
            self._sync_degraded = True
    
    def _get_product_info(self, symbol: str) -> dict:
        """Get product minimums (cached)."""
        if symbol in self._product_info:
            return self._product_info[symbol]
        
        if not self._client:
            return {"quote_min": 1.0, "base_min": 0.0001}
        
        try:
            product = self._client.get_product(symbol)
            info = {
                "quote_min": float(getattr(product, 'quote_min_size', 1) or 1),
                "base_min": float(getattr(product, 'base_min_size', 0.0001) or 0.0001),
                "base_increment": float(getattr(product, 'base_increment', 0.0001) or 0.0001),
            }
            self._product_info[symbol] = info
            return info
        except Exception as e:
            logger.warning("[ORDER] Product info failed for %s: %s", symbol, e, exc_info=True)
            return {"quote_min": 1.0, "base_min": 0.0001}
    
    def _check_minimum(self, symbol: str, usd_amount: float) -> bool:
        """Check if order meets minimum requirements."""
        info = self._get_product_info(symbol)
        if usd_amount < info["quote_min"]:
            logger.info("[ORDER] %s: $%s below min $%s", symbol, f"{usd_amount:.2f}", info["quote_min"])
            return False
        return True
    
    def has_position(self, symbol: str) -> bool:
        return symbol in self.positions
    
    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)
    
    def _get_candle_buffer(self, symbol: str):
        """Get candle buffer for a symbol (from collector)."""
        if hasattr(self, '_candle_collector') and self._candle_collector:
            return self._candle_collector.get_buffer(symbol)
        return None

    def _update_cached_balances(self):
        """Refresh cached balance/portfolio values from the injected portfolio manager."""
        try:
            if hasattr(self.portfolio, "positions_value"):
                try:
                    self.portfolio.positions_value = sum(p.size_usd for p in self.positions.values())
                except Exception:
                    pass
            self._usd_balance = self.portfolio.get_available_balance()
            self._portfolio_value = self.portfolio.get_total_portfolio_value()
            self._portfolio_snapshot = getattr(self.portfolio, "portfolio_snapshot", self._portfolio_snapshot)
            self._exchange_holdings = getattr(self.portfolio, "exchange_holdings", self._exchange_holdings)
        except Exception:
            pass

    def _snapshot_is_fresh(self, max_age_seconds: int = 10) -> bool:
        """Check if we have a recent portfolio snapshot."""
        if not self._portfolio_snapshot or not self._last_snapshot_at:
            return False
        age = (datetime.now(timezone.utc) - self._last_snapshot_at).total_seconds()
        return age <= max_age_seconds

    def _emit_order_event(
        self,
        event_type: str,
        position: Position,
        price: float,
        reason: str = "",
        pnl: float = 0.0,
        pnl_pct: float = 0.0,
        size_usd: float | None = None,
        size_qty: float | None = None,
    ) -> None:
        """Emit a normalized order event for downstream consumers."""
        if not self.event_bus:
            return
        try:
            evt = OrderEvent(
                event_type=event_type,
                symbol=position.symbol,
                side=position.side,
                mode=self.mode.value if isinstance(self.mode, TradingMode) else str(self.mode),
                strategy_id=getattr(position, "strategy_id", "") or "",
                price=price,
                size_usd=size_usd if size_usd is not None else position.size_usd,
                size_qty=size_qty if size_qty is not None else position.size_qty,
                reason=reason,
                pnl=pnl,
                pnl_pct=pnl_pct,
                ts=datetime.now(timezone.utc),
            )
            self.event_bus.emit_order(evt)
        except Exception:
            logger.debug("[EVENT] Failed to emit order event for %s", position.symbol, exc_info=True)

    def _cancel_order(self, order_id: str):
        """Best-effort cancel for unfilled orders."""
        if not self._client or not order_id:
            return
        try:
            self._client.cancel_orders(order_ids=[order_id])
        except Exception:
            pass
    
    def set_candle_collector(self, collector):
        """Set reference to candle collector for thesis checks."""
        self._candle_collector = collector

    def _record_rejection(self, reason: str, symbol: str = "", details: dict = None):
        """Track rejection counters on shared state and log for analysis."""
        # Update state counters
        if self.state:
            if reason == "warmth":
                self.state.rejections_warmth += 1
            elif reason == "regime":
                self.state.rejections_regime += 1
            elif reason == "score":
                self.state.rejections_score += 1
            elif reason == "rr":
                self.state.rejections_rr += 1
            elif reason == "limits":
                self.state.rejections_limits += 1
            elif reason == "spread":
                self.state.rejections_spread += 1
        
        # Log for post-analysis
        from core.logger import log_rejection, utc_iso_str
        record = {
            "ts": utc_iso_str(),
            "symbol": symbol,
            "gate": reason,
        }
        if details:
            record.update(details)
        log_rejection(record)

    def _categorize_score_rejection(self, entry_score: EntryScore) -> str:
        """Determine whether rejection was regime-driven or score-driven."""
        regime = intelligence._market_regime
        if regime != "normal" and not entry_score.btc_trend_ok:
            return "regime"
        # Fallback: treat as score rejection
        return "score"
    
    async def open_position(self, signal: Signal) -> Optional[Position]:
        """Open a new position based on signal."""
        symbol = signal.symbol
        
        # CRITICAL: Prevent race conditions - check in_flight FIRST
        if symbol in self._in_flight:
            logger.info("[ORDER] %s order already in flight, skipping", symbol)
            return None
        
        # Mark as in-flight immediately to block concurrent calls
        self._in_flight.add(symbol)
        
        try:
            return await self._do_open_position(signal)
        finally:
            # Always remove from in_flight when done
            self._in_flight.discard(symbol)
    
    async def _do_open_position(self, signal: Signal) -> Optional[Position]:
        """Internal: Actually open the position (called with in_flight protection)."""
        symbol = signal.symbol
        
        if self.daily_stats.should_stop:
            logger.info("[ORDER] Daily loss limit reached, skipping trade")
            return None
        
        # Circuit breaker check - block trades if API is failing
        if not self._circuit_breaker.can_trade():
            logger.warning("[ORDER] Circuit breaker OPEN - blocking trade for %s", symbol)
            self._record_rejection("circuit_breaker", symbol)
            return None
        
        # Check max positions (soft limit - exposure is the real limit)
        from core.profiles import is_test_profile
        is_test = is_test_profile(settings.profile)
        if not is_test and len(self.positions) >= settings.max_positions:
            logger.debug("[ORDER] Soft position limit (%s) reached, checking exposure", settings.max_positions)
            # Don't block - exposure budget check below will handle it
        
        # Accept both FLAG_BREAKOUT and FAST_BREAKOUT
        if signal.type not in [SignalType.FLAG_BREAKOUT, SignalType.FAST_BREAKOUT]:
            return None
        
        if self.has_position(signal.symbol):
            logger.info("[ORDER] Already have position in %s", signal.symbol)
            return None
        
        # Skip stablecoins/stable-like pairs (not worth trading)
        base = symbol.split("-")[0] if "-" in symbol else symbol
        if base in {"USDT", "USDC", "DAI", "USD", "EURC", "FDUSD", "PYUSD", "GUSD", "TUSD"}:
            logger.info("[ORDER] %s is a stable/pegged asset - skipping", symbol)
            self._record_rejection("limits", symbol, {"reason": "stablecoin"})
            return None
        
        # Check if we already have holdings on exchange (even if not tracked)
        symbol = signal.symbol
        if symbol in self._exchange_holdings:
            current_value = self._exchange_holdings[symbol]
            logger.info(
                "[ORDER] Already holding $%s of %s on exchange, skipping",
                f"{current_value:.2f}",
                symbol,
            )
            self._record_rejection("limits", symbol, {"reason": "already_holding"})
            return None
        
        # Check cooldown to prevent duplicate orders
        symbol = signal.symbol
        if symbol in self._order_cooldown:
            elapsed = (datetime.now(timezone.utc) - self._order_cooldown[symbol]).total_seconds()
            min_cooldown = settings.order_cooldown_min_seconds
            if elapsed < min_cooldown:
                logger.info(
                    "[ORDER] %s on hard cooldown (%ss remaining)",
                    symbol,
                    int(min_cooldown - elapsed),
                )
                return None
            if elapsed < self._cooldown_seconds:
                logger.info(
                    "[ORDER] %s on cooldown (%ss remaining)",
                    symbol,
                    int(self._cooldown_seconds - elapsed),
                )
                return None
        
        # Check if symbol is warm (has enough candle history) - skip in test profile
        if not is_test and not tier_scheduler.is_symbol_warm(symbol):
            logger.info("[ORDER] %s not warm (needs more candle history)", symbol)
            self._record_rejection("warmth", symbol)
            return None
        
        # Check if we already have exposure to this symbol (prevent stacking) - skip in test profile
        current_symbol_exposure = sum(
            p.cost_basis for p in self.positions.values() 
            if p.symbol == symbol
        )
        if not is_test and current_symbol_exposure >= 15.0:  # Max $15 per symbol
            logger.info(
                "[ORDER] %s already has $%s exposure, skipping",
                symbol,
                f"{current_symbol_exposure:.2f}",
            )
            self._record_rejection("limits", symbol, {"reason": "symbol_exposure"})
            return None
        
        # Intelligence layer: Check position limits (sector, global cooldown)
        intelligence.update_sector_counts(self.positions)
        allowed, limit_reason = intelligence.check_position_limits(
            symbol, 15.0, self.positions
        )
        if not allowed:
            logger.info("[INTEL] %s blocked: %s", symbol, limit_reason)
            self._record_rejection("limits", symbol, {"reason": limit_reason})
            return None
        
        # Spread gate: reject garbage liquidity before wasting scoring cycles
        signal_spread = getattr(signal, "spread_bps", 0.0)
        if not is_test and signal_spread > settings.spread_max_bps:
            logger.info(
                "[ORDER] %s spread %.1fbps > %sbps max",
                symbol,
                signal_spread,
                settings.spread_max_bps,
            )
            self._record_rejection("spread", symbol, {"spread_bps": signal_spread})
            return None
        
        # Whitelist gate: only trade historically profitable symbols if enabled
        if not is_test and settings.use_whitelist:
            whitelist = [s.strip() for s in settings.symbol_whitelist.split(",")]
            if symbol not in whitelist:
                logger.info("[ORDER] %s not in whitelist, skipping", symbol)
                self._record_rejection("whitelist", symbol)
                return None
        
        # Intelligence layer: Score entry confidence
        burst_metrics = {
            "vol_spike": getattr(signal, "vol_spike", 1.0),
            "range_spike": getattr(signal, "range_spike", 1.0),
            "trend_15m": getattr(signal, "trend_15m", 0.0),
            "vwap_distance": getattr(signal, "vwap_distance", 0.0),
            "spread_bps": getattr(signal, "spread_bps", 50.0),
            "tier": getattr(signal, "tier", "unknown"),
        }
        entry_score = intelligence.score_entry(signal, burst_metrics, self.positions)
        
        # Liquidity-aware tighten: if spread is high (but still within max), require a stronger setup
        if (not is_test and signal_spread > settings.spread_max_bps * 0.7 and
                entry_score.total_score < settings.entry_score_min + 5):
            logger.info(
                "[ORDER] %s spread %.1fbps requires higher score (%s < %s)",
                symbol,
                signal_spread,
                f"{entry_score.total_score:.0f}",
                settings.entry_score_min + 5,
            )
            self._record_rejection("spread", symbol, {"spread_bps": signal_spread, "score": entry_score.total_score})
            return None
        
        if not entry_score.should_enter:
            reasons = ", ".join(entry_score.reasons[:3])
            logger.info(
                "[INTEL] %s score %s/100 - SKIP (%s)",
                symbol,
                f"{entry_score.total_score:.0f}",
                reasons,
            )
            self._record_rejection(self._categorize_score_rejection(entry_score), symbol, {
                "score": entry_score.total_score,
                "reasons": entry_score.reasons[:3]
            })
            return None
        
        logger.info(
            "[INTEL] %s score %s/100 ✓ (%s)",
            symbol,
            f"{entry_score.total_score:.0f}",
            ", ".join(entry_score.reasons[:2]),
        )
        
        # Daily loss limit check
        is_halted, halt_reason = intelligence.is_trading_halted()
        if is_halted:
            logger.warning("[RISK] Trading halted: %s", halt_reason)
            self._record_rejection("risk", symbol, {"reason": "daily_loss_limit"})
            return None
        
        # New position limits check using registry 
        # Use the calculated size_usd that will be determined later
        estimated_size_usd = settings.max_trade_usd  # Conservative estimate
        can_open, limit_reason = self.position_registry.can_open_position(
            signal.strategy_id or "default", 
            estimated_size_usd
        )
        if not can_open:
            logger.info("[LIMITS] %s blocked by position registry: %s", symbol, limit_reason)
            self._record_rejection("limits", symbol, {"reason": limit_reason})
            return None
        
        price = signal.price
        is_fast = signal.type == SignalType.FAST_BREAKOUT
        
        # Dynamic position sizing based on portfolio and confidence
        # Get portfolio value for sizing
        pv = self._portfolio_value
        if self._portfolio_snapshot:
            pv = self._portfolio_snapshot.total_value
        if pv <= 0:
            pv = 500.0  # Fallback
        
        # TIERED SIZING: Bet big on best setups, small on others
        confluence_count = getattr(signal, 'confluence_count', 1)
        score = entry_score.total_score if hasattr(entry_score, 'total_score') else entry_score
        
        # Count current position tiers
        whale_threshold = settings.whale_trade_usd * 0.8  # 80% of whale size
        strong_threshold = settings.strong_trade_usd * 0.8
        whale_count = sum(1 for p in self.positions.values() 
                         if getattr(p, 'entry_cost_usd', 0) >= whale_threshold)
        strong_count = sum(1 for p in self.positions.values() 
                          if strong_threshold <= getattr(p, 'entry_cost_usd', 0) < whale_threshold)
        
        # Determine tier based on score + confluence (all from config)
        is_whale = score >= settings.whale_score_min and confluence_count >= settings.whale_confluence_min
        is_strong = score >= settings.strong_score_min or confluence_count >= settings.whale_confluence_min
        
        if is_whale and whale_count < settings.whale_max_positions:
            size_usd = settings.whale_trade_usd
            tier = "🐋 WHALE"
        elif is_strong and strong_count < settings.strong_max_positions:
            size_usd = settings.strong_trade_usd
            tier = "💪 STRONG"
        else:
            size_usd = settings.normal_trade_usd
            tier = "📊 NORMAL"
        
        logger.info("[TIER] %s: %s bet $%.0f (score:%d, confluence:%d)", 
                   symbol, tier, size_usd, score, confluence_count)
        
        # Apply time-of-day multiplier (reduce in dead zones)
        session_mult = intelligence.get_size_multiplier()
        if session_mult < 1.0:
            logger.info("[TIME] Dead zone - reducing size by %.0f%%", (1 - session_mult) * 100)
            size_usd *= session_mult
        
        # Clamp to portfolio limits
        min_size = pv * settings.position_min_pct
        max_size = pv * settings.position_max_pct
        size_usd = max(min_size, min(max_size, size_usd))
        
        # Cap at max_trade_usd to prevent executor rejection
        if size_usd > settings.max_trade_usd:
            logger.debug("[TIER] Capping %s from $%.0f to max $%.0f", symbol, size_usd, settings.max_trade_usd)
            size_usd = settings.max_trade_usd
        
        # TRUTH CHECK: Validate system state before trading
        if not self._validate_before_trade(symbol):
            logger.error("[TRUTH] Pre-trade validation failed for %s", symbol)
            self._record_rejection("truth", symbol, {"reason": "sync_failed"})
            return None
        
        # Sync before order to get latest positions  
        if self._client and not is_test:
            try:
                self.positions = sync_with_exchange(self._client, self.positions, quiet=True)
                # Filter out recently closed positions (5 min grace period for sells to settle)
                now = datetime.now(timezone.utc)
                for sym in list(self.positions.keys()):
                    if sym in self._recently_closed:
                        closed_at = self._recently_closed[sym]
                        if (now - closed_at).total_seconds() < 300:  # 5 min
                            del self.positions[sym]
                            logger.debug("[SYNC] Skipping recently closed: %s", sym)
                        else:
                            # Grace period expired, remove from cache
                            del self._recently_closed[sym]
                self._sync_position_stores()  # Keep registry in sync
            except Exception as e:
                logger.warning("[ORDER] Pre-order sync failed: %s", e, exc_info=True)
        
        portfolio_value = self._portfolio_value
        if self._portfolio_snapshot:
            portfolio_value = self._portfolio_snapshot.total_value
        
        bot_budget = portfolio_value * settings.portfolio_max_exposure_pct
        current_exposure = sum(p.cost_basis for p in self.positions.values())
        available_budget = bot_budget - current_exposure
        if is_test:
            available_budget = 10000.0
        
        if size_usd > available_budget:
            logger.info(
                "[ORDER] Budget limit: $%s/$%s used, need $%s, have $%s",
                f"{current_exposure:.0f}",
                f"{bot_budget:.0f}",
                f"{size_usd:.0f}",
                f"{available_budget:.0f}",
            )
            self._record_rejection("limits", symbol, {"reason": "budget_exceeded"})
            return None
        
        size_qty = size_usd / price if price else 0.0
        
        can_execute, reason = self.executor.can_execute_order(size_usd, symbol)
        if not can_execute:
            logger.info("[ORDER] Cannot execute %s: %s", symbol, reason)
            return None
        
        # Determine stops and targets based on mode
        # V2.1 geometry: 1.5% stop, 2.5% TP1 → R:R = 1.67
        if is_fast:
            # FAST mode: fixed percentage-based stops/targets
            stop_price = price * (1 - settings.fast_stop_pct / 100)  # 1.5% below
            tp1_price = price * (1 + settings.fast_tp1_pct / 100)    # 2.5% above
            tp2_price = price * (1 + settings.fast_tp2_pct / 100)    # 5% above
            time_stop_min = settings.fast_time_stop_min
        else:
            # Normal mode: ALWAYS use fixed percentages for profitable geometry
            # Signal values can be way too tight (0.1% stops!) which fees destroy
            # Override with config values from DI container
            stop_price = price * (1 - self.config.fixed_stop_pct)
            tp1_price = price * (1 + self.config.tp1_pct)
            tp2_price = price * (1 + self.config.tp2_pct)
            time_stop_min = getattr(settings, 'max_hold_minutes', 120)
            
            # Log if we're overriding different signal values
            signal_stop_pct = (price - signal.stop_price) / price * 100 if signal.stop_price else 0
            if signal_stop_pct > 0 and signal_stop_pct < self.config.fixed_stop_pct * 100 * 0.8:
                logger.info(
                    "[ORDER] %s override: signal stop %.2f%% → fixed %.1f%%",
                    symbol,
                    signal_stop_pct,
                    self.config.fixed_stop_pct * 100,
                )
        
        # Update signal object so dashboard shows correct values
        signal.stop_price = stop_price
        signal.tp1_price = tp1_price
        signal.tp2_price = tp2_price
        
        # Enforce minimum R:R ratio (risk to TP1)
        risk_per_share = price - stop_price
        reward_to_tp1 = tp1_price - price
        
        if risk_per_share > 0:
            rr_ratio = reward_to_tp1 / risk_per_share
            # Skip R:R check in test profile
            if not is_test and rr_ratio < self.config.min_rr_ratio:
                logger.info(
                    "[ORDER] %s R:R too low: %.2f < %s (risk $%.4f, reward $%.4f)",
                    symbol,
                    rr_ratio,
                    self.config.min_rr_ratio,
                    risk_per_share,
                    reward_to_tp1,
                )
                self._record_rejection("rr", symbol, {"rr_ratio": rr_ratio})
                return None
            # Store R:R for logging/dashboard
            signal.rr_ratio = rr_ratio
        else:
            # Invalid stop is always an error
            logger.info("[ORDER] %s invalid stop: stop >= entry", symbol)
            self._record_rejection("rr", symbol, {"reason": "invalid_stop"})
            return None
        
        # Set cooldown BEFORE order attempt to prevent duplicates
        self._order_cooldown[symbol] = datetime.now(timezone.utc)
        self._cooldown_persistence.save(self._order_cooldown)

        position = await self.executor.open_position(
            symbol=symbol,
            size_usd=size_usd,
            price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
        )

        if position is None:
            return None

        position.time_stop_min = time_stop_min
        mode_label = "[FAST]" if is_fast else f"[{self.mode.value.upper()}]"
        logger.info(
            "%s Opened LONG %s @ $%s, size $%s",
            mode_label,
            symbol,
            f"{price:.4f}",
            f"{size_usd:.2f}",
        )
        logger.info(
            "Stop: $%s | TP1: $%s | TP2: $%s | Time: %sm",
            f"{stop_price:.4f}",
            f"{tp1_price:.4f}",
            f"{tp2_price:.4f}",
            time_stop_min,
        )
        
        # Add to both position stores (maintain compatibility)
        self.position_registry.add_position(position)
        self.positions[symbol] = position
        
        # Track strategy PnL attribution
        self.pnl_engine.track_strategy_pnl(position.strategy_id, 0.0)  # Initial entry
        
        # Persist immediately so dashboard and restarts see it
        self.persistence.save_positions(self.positions)
        self._update_cached_balances()
        
        # TRUTH VERIFICATION: Verify position was actually created (in live mode)
        if self.mode == TradingMode.LIVE:
            asyncio.create_task(self._verify_position_created(symbol, size_qty))
        
        # Create thesis state for invalidation tracking
        try:
            from logic.edge_model import edge_model
            buffer = self._get_candle_buffer(symbol)
            if buffer and len(buffer.candles_1m) >= 10:
                edge_model.create_thesis_state(
                    symbol,
                    position.entry_price,
                    buffer.candles_1m,
                    buffer.candles_5m,
                    buffer.vwap(30)
                )
        except Exception:
            pass  # Thesis tracking is optional
        
        # Set cooldown to prevent duplicate orders
        self._order_cooldown[symbol] = datetime.now(timezone.utc)
        self._cooldown_persistence.save(self._order_cooldown)
        
        # Record trade for global cooldown
        intelligence.record_trade()
        
        # Log ML training data for entry
        burst_metrics = {
            "vol_spike": signal.vol_spike,
            "range_spike": signal.range_spike,
            "trend_15m": signal.trend_15m,
            "vwap_distance": signal.vwap_distance,
        }
        intelligence.log_trade_entry(symbol, entry_score, burst_metrics)
        
        # Get ML data for logging
        ml_data = intelligence.get_live_ml(symbol)
        ml_score = ml_data.raw_score if ml_data else None
        ml_conf = ml_data.confidence if ml_data else None
        
        # Log order intent with complete measurement data
        log_trade({
            "ts": utc_iso_str(),
            "type": "order_intent",
            "symbol": signal.symbol,
            "strategy_id": getattr(signal, "strategy_id", "burst_flag"),
            "side": "buy",
            "mode": self.mode.value,
            "entry_price": price,
            "size_usd": size_usd,
            "stop_price": stop_price,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "rr_ratio": rr_ratio,
            "stop_pct": (price - stop_price) / price * 100,
            # Score components
            "score_total": entry_score.total_score,
            "score_trend": entry_score.trend_score,
            "score_volume": entry_score.volume_score,
            "score_vwap": entry_score.vwap_score,
            "score_range": entry_score.range_score,
            "score_tier": entry_score.tier_score,
            # Burst metrics
            "impulse_pct": signal.trend_15m,
            "vol_spike": signal.vol_spike,
            "range_spike": signal.range_spike,
            "vwap_distance": signal.vwap_distance,
            # ML
            "ml_score": ml_score,
            "ml_confidence": ml_conf,
            # Context
            "btc_regime": intelligence._market_regime,
            "reason": signal.reason
        })
        
        # Log fill
        log_trade({
            "ts": utc_iso_str(),
            "type": "fill",
            "symbol": symbol,
            "side": "buy",
            "price": position.entry_price,
            "qty": position.size_qty
        })
        
        # Persist positions to disk
        self.persistence.save_positions(self.positions)

        # Emit normalized order event
        self._emit_order_event(
            event_type="open",
            position=position,
            price=position.entry_price,
            reason=signal.reason,
            size_usd=position.size_usd,
            size_qty=position.size_qty,
        )
        
        # Send alert (non-blocking)
        asyncio.create_task(alert_trade_entry(
            symbol=symbol,
            price=position.entry_price,
            size_usd=position.size_usd,
            stop_price=position.stop_price,
            score=entry_score.total_score
        ))
        
        return position
    
    async def _execute_live_buy(
        self, 
        symbol: str, 
        qty: float, 
        price: float, 
        signal: Signal,
        stop_price: float,      # V2.1 calculated stop
        tp1_price: float,       # V2.1 calculated TP1
        tp2_price: float,       # V2.1 calculated TP2
        time_stop_min: int = 30,
        use_limit: bool = False,  # Set True to use limit orders
        max_retries: int = 3
    ) -> Optional[Position]:
        """Execute a live buy order with stop-loss and retry logic."""
        if self._client is None:
            logger.error("[ORDER] No live client available")
            return None
        
        order_id = f"ct_{symbol}_{int(datetime.now().timestamp())}"
        last_error = None
        order = None
        
        for attempt in range(max_retries):
            try:
                # Rate limit before API call
                rate_limiter.wait_if_needed()
                
                if use_limit:
                    # Use limit order slightly above market for better fill
                    # Buffer ensures fill while still getting maker fee discount
                    limit_price = calculate_limit_buy_price(price, buffer_pct=settings.limit_buffer_pct)
                    order = self._client.limit_order_gtc_buy(
                        client_order_id=order_id,
                        product_id=symbol,
                        base_size=str(settings.max_trade_usd / limit_price),
                        limit_price=str(limit_price)
                    )
                else:
                    # Market order for immediate fill
                    order = self._client.market_order_buy(
                        client_order_id=order_id,
                        product_id=symbol,
                        quote_size=str(settings.max_trade_usd)  # Buy $X worth
                    )
                
                # Parse response - use expected_quote for market orders (not expected_qty)
                # Pass market price so we don't use hardcoded placeholder if no fill data!
                result = parse_order_response(order, expected_quote=settings.max_trade_usd, market_price=price)
                
                # Log order attempt
                from core.logger import log_order, utc_iso_str
                log_order({
                    "ts": utc_iso_str(),
                    "type": "buy_attempt",
                    "symbol": symbol,
                    "order_type": "limit" if use_limit else "market",
                    "qty": qty,
                    "price": price,
                    "success": result.success,
                    "order_id": result.order_id,
                    "fill_qty": result.fill_qty,
                    "fill_price": result.fill_price,
                    "error": result.error,
                })
                
                if result.success:
                    self._circuit_breaker.record_success()
                    break  # Success, exit retry loop
                else:
                    raise Exception(result.error or "Order failed")
                    
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                # Don't retry fatal errors
                if 'insufficient' in error_str or 'balance' in error_str:
                    logger.error("[LIVE] Insufficient funds: %s", e)
                    return None
                if 'invalid' in error_str and 'size' in error_str:
                    logger.error("[LIVE] Invalid order size: %s", e)
                    return None
                
                if attempt < max_retries - 1:
                    delay = 0.5 * (2 ** attempt)
                    logger.warning("[LIVE] Retry %s/%s: %s", attempt + 1, max_retries, e)
                    await asyncio.sleep(delay)
        
        if order is None:
            logger.error("[LIVE] Failed after %s attempts: %s", max_retries, last_error)
            self._circuit_breaker.record_failure()
            return None
        
        try:
            # Parse final result - use expected_quote for market orders
            # Pass market price so we don't use hardcoded placeholder if no fill data!
            if use_limit:
                result = parse_order_response(order, expected_qty=qty, market_price=price)
            else:
                result = parse_order_response(order, expected_quote=settings.max_trade_usd, market_price=price)
            
            if result.success:
                fill_qty = result.fill_qty or 0.0
                fill_price = result.fill_price or price

                # Guard against "success" without any fills (e.g., resting limit)
                if fill_qty <= 0:
                    logger.error(
                        "[LIVE] Order returned no fills, skipping position (policy=%s)",
                        self.LIMIT_NO_FILL_POLICY,
                    )
                    if use_limit and self.LIMIT_NO_FILL_POLICY == "cancel_skip":
                        self._cancel_order(result.order_id)
                    # Explicitly skip: no market fallback in measurement mode
                    return None
                
                # Log partial fill warning
                if result.partial_fill:
                    logger.warning(
                        "[LIVE] Partial fill: got %s, expected %s",
                        f"{fill_qty:.6f}",
                        f"{qty:.6f}",
                    )
                
                # Use V2.1 calculated stop price (passed as parameter)
                # Adjust stop relative to actual fill price if different from expected
                if fill_price != price and price > 0:
                    # Scale stop proportionally to fill price
                    stop_ratio = stop_price / price
                    adjusted_stop = fill_price * stop_ratio
                else:
                    adjusted_stop = stop_price
                
                # Place REAL stop-loss order on exchange via order manager
                stop_order_id = order_manager.place_stop_order(
                    symbol=symbol,
                    qty=fill_qty,
                    stop_price=adjusted_stop
                )
                if stop_order_id:
                    logger.info("[LIVE] Real stop-loss on exchange @ $%s", f"{adjusted_stop:.4f}")
                else:
                    logger.error("[LIVE] Stop order failed - unwinding position to avoid unprotected exposure")
                    try:
                        await self._execute_live_sell(symbol, fill_qty)
                    except Exception as sell_error:
                        logger.critical("[LIVE] Failed to unwind unprotected position: %s", sell_error, exc_info=True)
                    self._record_rejection("stop_failure", symbol, {"order_id": result.order_id})
                    return None
                
                # Adjust TPs relative to fill price too
                if fill_price != price and price > 0:
                    tp1_ratio = tp1_price / price
                    tp2_ratio = tp2_price / price
                    adjusted_tp1 = fill_price * tp1_ratio
                    adjusted_tp2 = fill_price * tp2_ratio
                else:
                    adjusted_tp1 = tp1_price
                    adjusted_tp2 = tp2_price
                
                entry_cost = fill_price * fill_qty  # Original cost at entry
                
                # Get entry confidence from signal
                entry_conf = getattr(signal, "confidence", 0.0) * 100  # Convert to 0-100
                ml_score = 0.0
                ml_result = intelligence.get_live_ml(symbol)
                if ml_result and not ml_result.is_stale():
                    ml_score = ml_result.raw_score
                
                position = Position(
                    symbol=symbol,
                    side=Side.BUY,
                    entry_price=fill_price,
                    entry_time=datetime.now(timezone.utc),
                    size_usd=entry_cost,  # Current value (will update)
                    size_qty=fill_qty,
                    stop_price=adjusted_stop,
                    tp1_price=adjusted_tp1,
                    tp2_price=adjusted_tp2,
                    time_stop_min=time_stop_min,
                    state=PositionState.OPEN,
                    strategy_id=getattr(signal, "strategy_id", "burst_flag"),
                    entry_cost_usd=entry_cost,  # Original cost (never changes!)
                    entry_confidence=entry_conf,
                    current_confidence=entry_conf,
                    peak_confidence=entry_conf,
                    ml_score_entry=ml_score,
                    ml_score_current=ml_score,
                )
                logger.info(
                    "[LIVE] Opened LONG %s @ $%s, qty %s",
                    symbol,
                    f"{fill_price:.4f}",
                    f"{fill_qty:.6f}",
                )
                return position
            else:
                logger.error("[LIVE] Order failed: %s", result.error)
                return None
                
        except Exception as e:
            logger.error("[LIVE] Error placing order: %s", e, exc_info=True)
            return None
    
    async def check_exits(self, symbol: str) -> Optional[TradeResult]:
        """Check if position should exit with smart trailing stops."""
        
        # Skip recently closed positions (prevents exit loop spam)
        if symbol in self._recently_closed:
            return None
        
        position = self.positions.get(symbol)
        if position is None:
            return None
        
        current_price = self.get_price(symbol)
        if current_price <= 0:
            return None

        # Stop-loss sanity: ensure an active exchange stop exists for live positions
        if self.mode == TradingMode.LIVE and position.stop_price:
            now = datetime.now(timezone.utc)
            last_check = self._last_stop_check.get(symbol)
            if not last_check or (now - last_check).total_seconds() > settings.stop_health_check_interval:
                self._last_stop_check[symbol] = now
                if not order_manager.has_stop_order(symbol):
                    placed_id = order_manager.place_stop_order(
                        symbol=symbol,
                        qty=position.size_qty,
                        stop_price=position.stop_price,
                    )
                    if placed_id:
                        logger.warning(
                            "[LIVE] Re-armed missing stop for %s @ $%s",
                            symbol,
                            f"{position.stop_price:.4f}",
                        )
                    else:
                        logger.error("[LIVE] Missing stop for %s and failed to re-arm", symbol)
        
        # Calculate current profit
        pnl_pct = ((current_price / position.entry_price) - 1) * 100
        
        # Trailing stop (decimals → %)
        trail_start = settings.trail_start_pct * 100
        trail_lock = settings.trail_lock_pct
        be_trigger = settings.trail_be_trigger_pct * 100
        btc_regime = intelligence._market_regime
        
        if btc_regime == "risk_off" and pnl_pct > 0:
            # Tighten trail: lock 70% of gains instead of 50%
            trail_lock = 0.70
            # Lower the trail trigger: start trailing at 0.5% instead of 1%
            trail_start = 0.5
            # Move to BE immediately if any profit
            if position.stop_price < position.entry_price:
                position.stop_price = position.entry_price * 1.001
                logger.info("[REGIME] %s: BTC RISK_OFF - moving stop to BE", symbol)
                self.persistence.save_positions(self.positions)
        
        # If we're up trail_start%+, trail stop to lock in gains
        if pnl_pct >= trail_start:
            # Trail stop at trail_lock% of gains
            new_stop = position.entry_price * (1 + pnl_pct * trail_lock / 100)
            if new_stop > position.stop_price:
                old_stop = position.stop_price
                position.stop_price = new_stop
                # Update REAL stop order on exchange (paper manager is no-op)
                self.stop_manager.update_stop_price(symbol, new_stop)
                logger.info(
                    "[TRAIL] %s: Stop raised $%s → $%s (lock %.1f%%)",
                    symbol,
                    f"{old_stop:.4f}",
                    f"{new_stop:.4f}",
                    pnl_pct * trail_lock,
                )
                self.persistence.save_positions(self.positions)
        
        # If we're up be_trigger%+, move stop to breakeven
        elif pnl_pct >= be_trigger and position.stop_price < position.entry_price:
            position.stop_price = position.entry_price * 1.001  # Tiny profit
            # Update REAL stop order on exchange (paper manager is no-op)
            self.stop_manager.update_stop_price(symbol, position.stop_price)
            logger.info(
                "[TRAIL] %s: Stop moved to breakeven @ $%s",
                symbol,
                f"{position.stop_price:.4f}",
            )
            self.persistence.save_positions(self.positions)
        
        exit_reason = None
        exit_partial = False
        
        # Check stop loss
        if position.should_stop(current_price):
            exit_reason = "stop"
        
        # Check TP1 (partial)
        elif position.should_tp1(current_price):
            exit_reason = "tp1"
            exit_partial = True
        
        # Check TP2 (full)
        elif position.should_tp2(current_price):
            exit_reason = "tp2"
        
        # Check thesis invalidation using cached live indicators (no recompute)
        elif pnl_pct < 0:  # Only check thesis if in a loss
            try:
                ind = intelligence.get_live_indicators(symbol)
                ml = intelligence.get_live_ml(symbol)
                
                if ind and ind.is_ready:
                    # Thesis invalidation rules:
                    # 1. 5m trend flipped bearish
                    if ind.trend_5m < settings.thesis_trend_flip_5m:
                        exit_reason = f"thesis_invalid: 5m trend {ind.trend_5m:.1f}%"
                        logger.info(
                            "[THESIS] %s: 5m trend flipped to %.1f%% - exiting",
                            symbol,
                            ind.trend_5m,
                        )
                    
                    # 2. Choppy price action (tighten stop, maybe exit)
                    # BUT: Don't exit if we're near 30-day support!
                    elif ind.is_choppy and pnl_pct < -1.0:
                        # Check if near daily/weekly low (potential support)
                        daily_range_pos = getattr(ind, 'daily_range_position', 0.5)
                        week_range_pos = getattr(ind, 'week_range_position', 0.5)
                        
                        if daily_range_pos < 0.15 or week_range_pos < 0.2:
                            # Near support - give it more room, don't exit yet
                            logger.info(
                                "[THESIS] %s: Choppy but near support (daily %.0f%%, week %.0f%%) - holding",
                                symbol,
                                daily_range_pos * 100,
                                week_range_pos * 100,
                            )
                        else:
                            exit_reason = "thesis_invalid: choppy_losing"
                            logger.info(
                                "[THESIS] %s: Choppy + losing %.1f%% - exiting",
                                symbol,
                                pnl_pct,
                            )
                    
                    # 3. ML bearish with confidence
                    elif ml and ml.bearish and ml.confidence > 0.6 and pnl_pct < -0.5:
                        exit_reason = f"thesis_invalid: ml_bearish ({ml.raw_score:.2f})"
                        logger.info(
                            "[THESIS] %s: ML bearish %.2f - exiting",
                            symbol,
                            ml.raw_score,
                        )
                    
                    # 4. Below VWAP significantly
                    elif ind.vwap_distance < settings.thesis_vwap_distance:
                        exit_reason = f"thesis_invalid: below_vwap {ind.vwap_distance:.1f}%"
                        logger.info(
                            "[THESIS] %s: %.1f%% below VWAP - exiting",
                            symbol,
                            ind.vwap_distance,
                        )
            except Exception:
                pass  # Thesis check is optional
        
        # Check very weak confidence - exit plays that have degraded significantly
        if exit_reason is None and position.current_confidence < 15:
            # Only exit if we're not in a strong profit
            if pnl_pct < 3.0:  # If less than 3% profit, exit weak play
                exit_reason = f"weak_confidence: {position.current_confidence:.0f}%"
                logger.info(
                    "[WEAK] %s: Confidence degraded to %.0f%% - exiting to free capital",
                    symbol,
                    position.current_confidence,
                )
        
        # Check time stop - but only if enabled and in profit or small loss
        # DISABLED by default (time_stop_enabled=False) - positions exit on TP/stop/thesis only
        elif settings.time_stop_enabled and position.time_stop_min and position.hold_duration_minutes() >= position.time_stop_min:
            if pnl_pct > -0.5:  # Only time stop if not deep in loss
                exit_reason = "time_stop"
            # If in loss and time expired, give 5 more minutes to recover
            elif position.hold_duration_minutes() >= position.time_stop_min + 5:
                exit_reason = "time_stop_extended"
        
        if exit_reason is None:
            return None
        
        # Log exit decision
        from core.logger import log_exit_decision, utc_iso_str
        log_exit_decision({
            "ts": utc_iso_str(),
            "symbol": symbol,
            "exit_reason": exit_reason,
            "current_price": current_price,
            "entry_price": position.entry_price,
            "stop_price": position.stop_price,
            "tp1_price": position.tp1_price,
            "pnl_pct": pnl_pct,
            "hold_minutes": position.hold_duration_minutes(),
            "is_partial": exit_partial and not position.partial_closed,
        })
        
        if exit_partial and not position.partial_closed:
            # Partial exit at TP1
            return await self._close_partial(position, current_price, exit_reason)
        else:
            # Full exit
            return await self._close_full(position, current_price, exit_reason)
    
    def update_position_confidence(self, symbol: str):
        """Update confidence tracking for an open position."""
        position = self.positions.get(symbol)
        if position is None:
            return
        
        # Get current ML score
        ml_result = intelligence.get_live_ml(symbol)
        if ml_result and not ml_result.is_stale():
            position.ml_score_current = ml_result.raw_score
            
            # Update confidence based on ML and price action
            current_price = self.get_price(symbol)
            if current_price > 0 and position.entry_price > 0:
                pnl_pct = ((current_price / position.entry_price) - 1) * 100
                
                # Base confidence from entry
                conf = position.entry_confidence
                
                # Adjust based on price action
                if pnl_pct > 2.0:  # In profit
                    conf += 10
                elif pnl_pct < -1.5:  # Losing
                    conf -= 15
                
                # Adjust based on ML score change (only for bot-opened positions)
                # Synced positions have ml_score_entry=0, so skip ML penalty for them
                if position.ml_score_entry != 0:
                    ml_delta = position.ml_score_current - position.ml_score_entry
                    conf += ml_delta * 20  # ML score is -1 to 1, so *20 = -20 to +20
                
                # Clamp to 0-100
                position.current_confidence = max(0, min(100, conf))
                
                # Track peak
                if position.current_confidence > position.peak_confidence:
                    position.peak_confidence = position.current_confidence
    
    def update_all_position_confidence(self):
        """Update confidence for all open positions."""
        for symbol in self.positions:
            self.update_position_confidence(symbol)
    
    async def _close_partial(
        self, 
        position: Position, 
        price: float, 
        reason: str
    ) -> Optional[TradeResult]:
        """Close partial position (TP1)."""
        
        close_qty = position.size_qty * settings.tp1_partial_pct
        close_usd = close_qty * price
        closed_cost = close_qty * position.entry_price if position.entry_price else 0.0
        
        # Calculate partial PnL first (need it for paper credit)
        pnl = (price - position.entry_price) * close_qty
        
        if self.mode == TradingMode.PAPER:
            logger.info("[PAPER] Partial close %s @ $%s (%s)", position.symbol, f"{price:.4f}", reason)
            # Credit paper balance: return closed portion + PnL
            if hasattr(self.portfolio, 'credit'):
                self.portfolio.credit(close_usd + pnl)
        else:
            await self._execute_live_sell(position.symbol, close_qty)
        position.realized_pnl += pnl
        position.partial_closed = True
        position.size_qty -= close_qty
        position.size_usd -= close_usd
        
        # Move stop to breakeven after TP1
        old_stop = position.stop_price
        position.stop_price = position.entry_price * 1.001
        
        # Update stop order on exchange with new qty and price
        self.stop_manager.cancel_stop_order(position.symbol)
        self.stop_manager.place_stop_order(
            symbol=position.symbol,
            qty=position.size_qty,  # Remaining qty
            stop_price=position.stop_price
        )
        
        logger.info(
            "Partial PnL: $%s, stop raised to breakeven $%s",
            f"{pnl:.2f}",
            f"{position.stop_price:.4f}",
        )
        self.persistence.save_positions(self.positions)
        self._update_cached_balances()

        # Emit normalized partial close event
        pnl_pct = (pnl / closed_cost * 100) if closed_cost > 0 else 0.0
        self._emit_order_event(
            event_type="partial_close",
            position=position,
            price=price,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            size_usd=close_usd,
            size_qty=close_qty,
        )
        
        return None  # Position still open
    
    async def _close_full(
        self, 
        position: Position, 
        price: float, 
        reason: str
    ) -> TradeResult:
        """Close full position."""
        
        # Calculate final PnL using PnLEngine (centralized, accurate)
        pnl_breakdown = self.pnl_engine.calculate_trade_pnl(
            entry_price=position.entry_price,
            exit_price=price,
            qty=position.size_qty,
            side=position.side,
            realized_pnl=position.realized_pnl
        )
        
        if self.mode == TradingMode.PAPER:
            logger.info("[PAPER] Closed %s @ $%s (%s)", position.symbol, f"{price:.4f}", reason)
            # Credit paper balance: return cost basis + net PnL
            if hasattr(self.portfolio, 'credit'):
                self.portfolio.credit(position.size_usd + pnl_breakdown.net_pnl)
        else:
            # Cancel any stop order first to avoid double-sell
            self.stop_manager.cancel_stop_order(position.symbol)
            await self._execute_live_sell(position.symbol, position.size_qty)
        
        # Extract values for backward compatibility
        gross_pnl = pnl_breakdown.gross_pnl
        total_fees = pnl_breakdown.total_fees
        pnl = pnl_breakdown.net_pnl
        pnl_pct = pnl_breakdown.pnl_pct
        fee_pct = pnl_breakdown.fee_pct
        
        # Print fee breakdown
        fee_type = "limit+market" if settings.use_limit_orders else "market+market"
        logger.info(
            "Gross: $%s | Fees: $%s (%s) | Net: $%s (%s%%)",
            f"{gross_pnl:.2f}",
            f"{total_fees:.2f}",
            fee_type,
            f"{pnl:.2f}",
            f"{pnl_pct:+.1f}",
        )
        
        result = TradeResult(
            symbol=position.symbol,
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
        
        # Track strategy PnL attribution
        if position.strategy_id:
            self.pnl_engine.track_strategy_pnl(position.strategy_id, pnl)
        
        # Don't count TEST trades in daily stats
        if not position.symbol.startswith("TEST"):
            self.daily_stats.record_trade(pnl)
        
        # Remove from both position stores (maintain compatibility)
        self.position_registry.remove_position(position.symbol)
        del self.positions[position.symbol]
        
        # Track as recently closed to prevent sync from re-adding (5 min grace period)
        self._recently_closed[position.symbol] = datetime.now(timezone.utc)
        
        # Log ML training data for exit
        intelligence.log_trade_exit(
            symbol=position.symbol,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            hold_minutes=position.hold_duration_minutes()
        )
        
        # Send alert (non-blocking)
        asyncio.create_task(alert_trade_exit(
            symbol=position.symbol,
            entry_price=position.entry_price,
            exit_price=price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            reason=reason
        ))
        
        # Update persistence
        self.persistence.save_positions(self.positions)
        self.persistence.clear_position(position.symbol)
        self._update_cached_balances()
        
        # Calculate R multiple (pnl / initial risk)
        initial_risk = position.entry_price - position.stop_price
        r_multiple = (pnl / position.size_usd) / (initial_risk / position.entry_price) if initial_risk > 0 else 0
        
        # Log trade close with R multiple
        log_trade({
            "ts": utc_iso_str(result.exit_time),
            "type": "trade_close",
            "symbol": result.symbol,
            "strategy_id": position.strategy_id,
            "entry_price": position.entry_price,
            "exit_price": result.exit_price,
            "stop_price": position.stop_price,
            "size_usd": position.size_usd,
            "pnl": result.pnl,
            "pnl_pct": result.pnl_pct,
            "r_multiple": round(r_multiple, 2),
            "exit_reason": result.exit_reason,
            "hold_minutes": position.hold_duration_minutes(),
            "tp1_hit": position.partial_closed,
        })
        
        # Log sell fill
        log_trade({
            "ts": utc_iso_str(),
            "type": "fill",
            "symbol": position.symbol,
            "side": "sell",
            "price": price,
            "qty": position.size_qty
        })

        # Emit normalized close event
        self._emit_order_event(
            event_type="close",
            position=position,
            price=price,
            reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            size_usd=position.size_usd,
            size_qty=position.size_qty,
        )
        
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        logger.info("Final PnL: %s (%s%%)", pnl_str, f"{pnl_pct:+.2f}")
        
        return result
    
    async def _execute_live_sell(self, symbol: str, qty: float):
        """Execute a live sell order and cancel any open stop orders."""
        if self._client is None:
            return
        
        try:
            # First cancel any open stop orders for this symbol
            # Note: Coinbase API doesn't allow combining OPEN with other statuses
            try:
                open_orders = self._client.list_orders(product_id=symbol, order_status=["OPEN"])
                orders_list = getattr(open_orders, "orders", []) or []
                for order in orders_list:
                    order_id = getattr(order, "order_id", None) or order.get("order_id")
                    if order_id:
                        self._client.cancel_orders(order_ids=[order_id])
                        logger.info("[LIVE] Cancelled stop order %s", order_id)
            except Exception as e:
                logger.warning("[LIVE] Warning: Could not cancel stop orders: %s", e, exc_info=True)
            
            # Execute market sell
            order = self._client.market_order_sell(
                client_order_id=f"ct_sell_{symbol}_{int(datetime.now().timestamp())}",
                product_id=symbol,
                base_size=str(qty)
            )
            
            success = getattr(order, "success", None) or (order.get("success") if isinstance(order, dict) else True)
            if success or order:
                self._circuit_breaker.record_success()
                logger.info("[LIVE] Sold %s, qty %s", symbol, f"{qty:.6f}")
            else:
                self._circuit_breaker.record_failure()
                logger.error("[LIVE] Sell order failed: %s", order)
                
        except Exception as e:
            self._circuit_breaker.record_failure()
            logger.error("[LIVE] Error selling: %s", e, exc_info=True)
    
    async def force_close_all(self, reason: str = "manual"):
        """Force close all positions."""
        for symbol in list(self.positions.keys()):
            position = self.positions[symbol]
            price = self.get_price(symbol)
            await self._close_full(position, price, reason)
    
    async def auto_rebalance(self, target_available_pct: float = 0.3) -> float:
        """
        Auto-rebalance: sell enough of largest positions to free up budget.
        
        Args:
            target_available_pct: Target % of budget to have available (0.3 = 30%)
        
        Returns:
            Amount freed up in USD
        """
        if self.mode == TradingMode.PAPER:
            logger.info("[REBALANCE] Not available in paper mode")
            return 0.0
        
        # Calculate current state
        portfolio_value = self._portfolio_value
        if self._portfolio_snapshot:
            portfolio_value = self._portfolio_snapshot.total_value
        
        bot_budget = portfolio_value * settings.portfolio_max_exposure_pct
        current_exposure = sum(p.size_usd for p in self.positions.values())
        available = bot_budget - current_exposure
        target_available = bot_budget * target_available_pct
        
        # How much do we need to free up?
        need_to_free = target_available - available
        
        if need_to_free <= 0:
            logger.info(
                "[REBALANCE] Already have $%s available (target: $%s)",
                f"{available:.0f}",
                f"{target_available:.0f}",
            )
            return 0.0
        
        logger.info(
            "[REBALANCE] Need to free $%s to reach $%s available",
            f"{need_to_free:.0f}",
            f"{target_available:.0f}",
        )
        
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
            
            # Sell this position
            symbol = position.symbol
            price = self.get_price(symbol)
            
            logger.info("[REBALANCE] Closing %s ($%s) to free budget", symbol, f"{position.size_usd:.0f}")
            result = await self._close_full(position, price, "rebalance")
            
            if result:
                freed += position.size_usd
                logger.info(
                    "[REBALANCE] Freed $%s, total freed: $%s",
                    f"{position.size_usd:.0f}",
                    f"{freed:.0f}",
                )
        
        logger.info(
            "[REBALANCE] Complete. Freed $%s. New available: $%s",
            f"{freed:.0f}",
            f"{available + freed:.0f}",
        )
        return freed
    
    async def trim_largest(self, amount_usd: float = 50.0) -> Optional[TradeResult]:
        """
        Trim the largest position by a specific dollar amount.
        Useful for gradually freeing up budget.
        
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
            return await self._close_full(largest, price, "trim")
        
        # Partial close - calculate qty to sell
        price = self.get_price(largest.symbol)
        qty_to_sell = amount_usd / price if price > 0 else 0
        
        if qty_to_sell <= 0:
            return None
        
        logger.info("[TRIM] Selling $%s of %s", f"{amount_usd:.0f}", largest.symbol)
        
        # Execute partial sell
        try:
            if self._client:
                await self._execute_live_sell(largest.symbol, qty_to_sell)
                
                # Update position
                largest.size_qty -= qty_to_sell
                largest.size_usd -= amount_usd
                
                if largest.size_usd < 5:
                    # Position dust, remove it
                    self.position_registry.remove_position(largest.symbol)
                    del self.positions[largest.symbol]
                
                self.persistence.save_positions(self.positions)
                self._update_cached_balances()
                logger.info("[TRIM] Done. %s now $%s", largest.symbol, f"{largest.size_usd:.0f}")
                
                return TradeResult(
                    symbol=largest.symbol,
                    entry_price=largest.entry_price,
                    exit_price=price,
                    exit_time=datetime.now(timezone.utc),
                    pnl=0,  # Simplified
                    pnl_pct=0,
                    exit_reason="trim"
                )
        except Exception as e:
            logger.error("[TRIM] Error: %s", e, exc_info=True)
        
        return None
