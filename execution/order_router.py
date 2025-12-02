"""Order routing with paper trading and live execution."""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from core.config import settings
from core.logging_utils import get_logger
from core.logger import log_trade, utc_iso_str
from core.models import Signal, SignalType, Position, PositionState, Side, TradeResult
from core.persistence import save_positions, load_positions, sync_with_exchange, clear_position
from core.portfolio import portfolio_tracker, PortfolioSnapshot
from execution.order_manager import order_manager
from execution.order_utils import (
    calculate_limit_buy_price,
    OrderFatalError,
    OrderResult,
    parse_order_response,
    rate_limiter,
)
from execution.paper_execution import PaperExecution
from logic.intelligence import EntryScore, intelligence
from datafeeds.universe import tier_scheduler
from services.alerts import alert_error, alert_trade_entry, alert_trade_exit

logger = get_logger(__name__)


@dataclass
class DailyStats:
    """Daily trading statistics with compounding metrics."""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0
    
    # For avg win/loss calculation
    total_win_pnl: float = 0.0
    total_loss_pnl: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    
    # Track the date these stats are for
    stats_date: str = ""
    
    def check_reset(self):
        """Reset stats if it's a new day (UTC)."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.stats_date != today:
            logger.info("[STATS] New day detected (%s → %s), resetting daily stats", self.stats_date, today)
            self.trades = 0
            self.wins = 0
            self.losses = 0
            self.total_pnl = 0.0
            self.max_drawdown = 0.0
            self.peak_pnl = 0.0
            self.total_win_pnl = 0.0
            self.total_loss_pnl = 0.0
            self.biggest_win = 0.0
            self.biggest_loss = 0.0
            self.stats_date = today
    
    def record_trade(self, pnl: float):
        self.trades += 1
        if pnl > 0:
            self.wins += 1
            self.total_win_pnl += pnl
            self.biggest_win = max(self.biggest_win, pnl)
        else:
            self.losses += 1
            self.total_loss_pnl += abs(pnl)
            self.biggest_loss = max(self.biggest_loss, abs(pnl))
        self.total_pnl += pnl
        self.peak_pnl = max(self.peak_pnl, self.total_pnl)
        self.max_drawdown = min(self.max_drawdown, self.total_pnl - self.peak_pnl)
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0
    
    @property
    def avg_win(self) -> float:
        return self.total_win_pnl / self.wins if self.wins > 0 else 0.0
    
    @property
    def avg_loss(self) -> float:
        return self.total_loss_pnl / self.losses if self.losses > 0 else 0.0
    
    @property
    def profit_factor(self) -> float:
        """Sum of wins / sum of losses. >1.0 is profitable."""
        return self.total_win_pnl / self.total_loss_pnl if self.total_loss_pnl > 0 else float('inf')
    
    @property
    def avg_r(self) -> float:
        """Average R per trade (avg_win / avg_loss ratio weighted by win rate)."""
        if self.avg_loss == 0:
            return 0.0
        return (self.avg_win * self.win_rate - self.avg_loss * (1 - self.win_rate)) / self.avg_loss if self.avg_loss > 0 else 0.0
    
    @property
    def should_stop(self) -> bool:
        return self.total_pnl <= -settings.daily_max_loss_usd
    
    @property
    def loss_limit_pct(self) -> float:
        """How close to daily loss limit (0-100%)."""
        if self.total_pnl >= 0:
            return 0.0
        return min(100.0, abs(self.total_pnl) / settings.daily_max_loss_usd * 100)


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
    
    def __init__(self, get_price_func, state=None):
        """
        Args:
            get_price_func: Callable that takes symbol and returns current price
        """
        self.get_price = get_price_func
        self.state = state  # Optional BotState for rejection counters
        self.positions: dict[str, Position] = {}
        self.trade_history: list[TradeResult] = []
        self.daily_stats = DailyStats()
        self._client = None
        self._product_info: dict[str, dict] = {}  # Cache product minimums
        self._usd_balance: float = 0.0
        self._order_cooldown: dict[str, datetime] = {}  # Prevent duplicate orders
        self._cooldown_seconds = 1800  # 30 minute cooldown per symbol
        self._in_flight: set[str] = set()  # Symbols with orders in progress (race condition prevention)
        self._symbol_exposure: dict[str, float] = {}  # Track total exposure per symbol
        self._portfolio_value: float = 0.0  # Tracked portfolio value
        self._exchange_holdings: dict[str, float] = {}  # Actual holdings from exchange
        self._portfolio_snapshot: Optional[PortfolioSnapshot] = None  # Real portfolio from Coinbase
        self.paper_executor = PaperExecution() if settings.is_paper else None
        
        # Paper defaults to a simulated cash balance so exposure gates don't block
        if settings.is_paper:
            paper_balance = settings.paper_start_balance_usd
            self._usd_balance = paper_balance
            self._portfolio_value = paper_balance
            if self.state:
                self.state.paper_balance_usd = paper_balance
                self.state.paper_balance = paper_balance  # Also set state.paper_balance
                self.state.live_balance_usd = 0.0
        
        if not settings.is_paper and settings.is_configured:
            self._init_live_client()
            self._refresh_balance()
            # Initialize order manager with client
            if self._client:
                order_manager.init_client(self._client)
                order_manager.sync_with_exchange()
            # Load and sync positions with exchange
            self.positions = load_positions()
            logger.info("[PERSIST] Loaded %s LIVE positions", len(self.positions))
            if self._client:
                self.positions = sync_with_exchange(self._client, self.positions, quiet=False)
                # Print what we synced for debugging
                total_cost = sum(p.cost_basis for p in self.positions.values())
                total_value = sum(p.size_usd for p in self.positions.values())
                logger.info(
                    "[SYNC] Total: %s positions, cost $%s, value $%s",
                    len(self.positions),
                    f"{total_cost:.0f}",
                    f"{total_value:.0f}",
                )
        else:
            # Explicitly keep live client disabled in paper mode even if keys are present
            self._client = None
    
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
                elif value > 0.0001:
                    # Estimate value of holdings and track them
                    symbol = f"{currency}-USD"
                    
                    # Skip delisted coins (avoid 404 errors)
                    DELISTED = {'BOND-USD', 'NU-USD', 'CLV-USD', 'SNX-USD'}
                    if symbol in DELISTED:
                        continue
                    
                    try:
                        product = self._client.get_product(symbol)
                        price = float(getattr(product, 'price', 0))
                        position_value = value * price
                        holdings_value += position_value
                        # Track for blocking new buys
                        if position_value >= 1.0:  # Ignore dust
                            self._exchange_holdings[symbol] = position_value
                    except:
                        pass
            
            # Calculate totals
            self._usd_balance = usd_bal + usdc_bal
            self._portfolio_value = self._usd_balance + holdings_value
            
            # Fallback: if portfolio seems too low, use estimate
            # (API sometimes doesn't return USD fiat balance)
            if self._portfolio_value < 50:
                logger.warning("[ORDER] API balance low ($%s), using fallback", f"{self._portfolio_value:.2f}")
                self._portfolio_value = 500.0  # Conservative estimate
                self._usd_balance = 450.0  # Most is cash
            
            logger.info(
                "[ORDER] Portfolio: $%s (Cash: $%s, Holdings: $%s)",
                f"{self._portfolio_value:.2f}",
                f"{self._usd_balance:.2f}",
                f"{holdings_value:.2f}",
            )
            logger.info("[ORDER] Trade size: $10.00 (fixed)")
            
            # Get REAL portfolio snapshot from Coinbase Portfolio API
            try:
                self._portfolio_snapshot = portfolio_tracker.get_snapshot()
                if self._portfolio_snapshot:
                    logger.info(
                        "[ORDER] Real P&L: $%s (%s positions)",
                        f"{self._portfolio_snapshot.total_unrealized_pnl:+.2f}",
                        self._portfolio_snapshot.position_count,
                    )
            except Exception as pe:
                logger.warning("[ORDER] Portfolio snapshot failed: %s", pe, exc_info=True)
                
        except Exception as e:
            logger.error("[ORDER] Balance check failed: %s", e, exc_info=True)
    
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
        
        # Check max positions (skip in test profile)
        from core.profiles import is_test_profile
        is_test = is_test_profile(settings.profile)
        if not is_test and len(self.positions) >= settings.max_positions:
            logger.info("[ORDER] Max positions (%s) reached", settings.max_positions)
            self._record_rejection("limits", symbol, {"reason": "max_positions"})
            return None
        
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
        
        # Check cooldown to prevent duplicate orders (30 min for normal, 5 min minimum for just-ordered)
        symbol = signal.symbol
        if symbol in self._order_cooldown:
            elapsed = (datetime.now(timezone.utc) - self._order_cooldown[symbol]).total_seconds()
            min_cooldown = 300  # 5 minute MINIMUM cooldown after any order attempt
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
        
        price = signal.price
        is_fast = signal.type == SignalType.FAST_BREAKOUT
        
        # Fixed position size - small for testing, allows more positions
        base_size_usd = settings.max_trade_usd  # Use config value (default $5)
        size_usd = intelligence.get_position_size(base_size_usd, entry_score)
        
        # Sync before order to get latest positions
        if self._client and not is_test:
            try:
                self.positions = sync_with_exchange(self._client, self.positions, quiet=True)
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
        
        size_qty = size_usd / price
        
        # Check minimum order size (live mode)
        if not settings.is_paper:
            if not self._check_minimum(symbol, size_usd):
                return None
            # Check we have enough balance
            if self._usd_balance < size_usd:
                logger.info(
                    "[ORDER] Insufficient balance: $%s < $%s",
                    f"{self._usd_balance:.2f}",
                    f"{size_usd:.2f}",
                )
                self._refresh_balance()  # Maybe stale
                if self._usd_balance < size_usd:
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
            # Override with config values: 2.5% stop, 4% TP1, 7% TP2
            stop_price = price * (1 - settings.fixed_stop_pct)
            tp1_price = price * (1 + settings.tp1_pct)
            tp2_price = price * (1 + settings.tp2_pct)
            time_stop_min = settings.max_hold_minutes
            
            # Log if we're overriding different signal values
            signal_stop_pct = (price - signal.stop_price) / price * 100 if signal.stop_price else 0
            if signal_stop_pct > 0 and signal_stop_pct < settings.fixed_stop_pct * 100 * 0.8:
                logger.info(
                    "[ORDER] %s override: signal stop %.2f%% → fixed %.1f%%",
                    symbol,
                    signal_stop_pct,
                    settings.fixed_stop_pct * 100,
                )
        
        # Update signal object so dashboard shows correct values
        signal.stop_price = stop_price
        signal.tp1_price = tp1_price
        signal.tp2_price = tp2_price
        
        # TODO-LOGIC-1: Enforce minimum R:R ratio (risk to TP1)
        # This ensures average winner > average loser for compounding
        risk_per_share = price - stop_price
        reward_to_tp1 = tp1_price - price
        
        if risk_per_share > 0:
            rr_ratio = reward_to_tp1 / risk_per_share
            # Skip R:R check in test profile
            if not is_test and rr_ratio < settings.min_rr_ratio:
                logger.info(
                    "[ORDER] %s R:R too low: %.2f < %s (risk $%.4f, reward $%.4f)",
                    symbol,
                    rr_ratio,
                    settings.min_rr_ratio,
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
        
        if settings.is_paper:
            # Paper trade - simulate via paper executor
            position = self.paper_executor.open_buy(
                symbol=symbol,
                price=price,
                size_usd=size_usd,
                stop_price=stop_price,
                tp1_price=tp1_price,
                tp2_price=tp2_price,
                time_stop_min=time_stop_min,
                strategy_id=getattr(signal, "strategy_id", "burst_flag"),
            )
            mode_label = "[FAST]" if is_fast else "[PAPER]"
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
        else:
            # CRITICAL: Set cooldown BEFORE order attempt to prevent duplicates!
            self._order_cooldown[symbol] = datetime.now(timezone.utc)
            
            # Live trade - pass calculated stop/TP values (not original signal values)
            # Use limit orders if configured (saves 0.6% per trade in fees!)
            position = await self._execute_live_buy(
                symbol=symbol, 
                qty=size_qty, 
                price=price, 
                signal=signal,
                stop_price=stop_price,      # V2.1 calculated
                tp1_price=tp1_price,        # V2.1 calculated
                tp2_price=tp2_price,        # V2.1 calculated
                time_stop_min=time_stop_min,
                use_limit=settings.use_limit_orders  # From config
            )
            if position is None:
                # Even if order failed, keep cooldown to prevent hammering
                return None
        
        self.positions[symbol] = position
        
        # Persist immediately so dashboard and restarts see it
        save_positions(self.positions)
        
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
            "mode": settings.trading_mode,
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
        save_positions(self.positions)
        
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
                    logger.warning("[LIVE] Stop order failed - position unprotected!")
                
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
        
        position = self.positions.get(symbol)
        if position is None:
            return None
        
        current_price = self.get_price(symbol)
        if current_price <= 0:
            return None
        
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
                save_positions(self.positions)
        
        # If we're up trail_start%+, trail stop to lock in gains
        if pnl_pct >= trail_start:
            # Trail stop at trail_lock% of gains
            new_stop = position.entry_price * (1 + pnl_pct * trail_lock / 100)
            if new_stop > position.stop_price:
                old_stop = position.stop_price
                position.stop_price = new_stop
                # Update REAL stop order on exchange (paper mode skips this)
                if not settings.is_paper:
                    order_manager.update_stop_price(symbol, new_stop)
                logger.info(
                    "[TRAIL] %s: Stop raised $%s → $%s (lock %.1f%%)",
                    symbol,
                    f"{old_stop:.4f}",
                    f"{new_stop:.4f}",
                    pnl_pct * trail_lock,
                )
                save_positions(self.positions)
        
        # If we're up be_trigger%+, move stop to breakeven
        elif pnl_pct >= be_trigger and position.stop_price < position.entry_price:
            position.stop_price = position.entry_price * 1.001  # Tiny profit
            # Update REAL stop order on exchange (paper mode skips this)
            if not settings.is_paper:
                order_manager.update_stop_price(symbol, position.stop_price)
            logger.info(
                "[TRAIL] %s: Stop moved to breakeven @ $%s",
                symbol,
                f"{position.stop_price:.4f}",
            )
            save_positions(self.positions)
        
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
                
                # Adjust based on ML score change
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
        
        if settings.is_paper:
            logger.info("[PAPER] Partial close %s @ $%s (%s)", position.symbol, f"{price:.4f}", reason)
        else:
            await self._execute_live_sell(position.symbol, close_qty)
        
        # Calculate partial PnL
        pnl = (price - position.entry_price) * close_qty
        position.realized_pnl += pnl
        position.partial_closed = True
        position.size_qty -= close_qty
        position.size_usd -= close_usd
        
        # Move stop to breakeven after TP1
        old_stop = position.stop_price
        position.stop_price = position.entry_price * 1.001
        
        # Update stop order on exchange with new qty and price
        if not settings.is_paper:
            # Cancel old stop and place new one with reduced qty
            order_manager.cancel_stop_order(position.symbol)
            order_manager.place_stop_order(
                symbol=position.symbol,
                qty=position.size_qty,  # Remaining qty
                stop_price=position.stop_price
            )
        
        logger.info(
            "Partial PnL: $%s, stop raised to breakeven $%s",
            f"{pnl:.2f}",
            f"{position.stop_price:.4f}",
        )
        save_positions(self.positions)
        
        return None  # Position still open
    
    async def _close_full(
        self, 
        position: Position, 
        price: float, 
        reason: str
    ) -> TradeResult:
        """Close full position."""
        
        if settings.is_paper:
            logger.info("[PAPER] Closed %s @ $%s (%s)", position.symbol, f"{price:.4f}", reason)
        else:
            # Cancel any stop order first to avoid double-sell
            order_manager.cancel_stop_order(position.symbol)
            await self._execute_live_sell(position.symbol, position.size_qty)
        
        # Calculate final PnL (including fees)
        gross_pnl = (price - position.entry_price) * position.size_qty + position.realized_pnl
        # Fees: entry uses maker if limit orders enabled, exit is taker (market sell)
        entry_fee_rate = self.MAKER_FEE_PCT if settings.use_limit_orders else self.TAKER_FEE_PCT
        entry_fee = position.entry_price * position.size_qty * entry_fee_rate
        exit_fee = price * position.size_qty * self.TAKER_FEE_PCT
        total_fees = entry_fee + exit_fee
        pnl = gross_pnl - total_fees
        fee_pct = (entry_fee_rate + self.TAKER_FEE_PCT) * 100
        pnl_pct = ((price / position.entry_price) - 1) * 100 - fee_pct
        
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
        
        # Don't count TEST trades in daily stats
        if not position.symbol.startswith("TEST"):
            self.daily_stats.record_trade(pnl)
        
        del self.positions[position.symbol]
        
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
        save_positions(self.positions)
        clear_position(position.symbol)
        
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
                logger.info("[LIVE] Sold %s, qty %s", symbol, f"{qty:.6f}")
            else:
                logger.error("[LIVE] Sell order failed: %s", order)
                
        except Exception as e:
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
        if settings.is_paper:
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
        if settings.is_paper or not self.positions:
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
                    del self.positions[largest.symbol]
                
                save_positions(self.positions)
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
