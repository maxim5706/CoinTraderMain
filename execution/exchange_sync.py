"""Exchange synchronization for portfolio and position state.

Extracted from order_router.py - handles syncing local state with exchange,
balance refresh, and position verification.
"""

from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from core.config import settings
from core.logging_utils import get_logger, should_emit
from core.mode_configs import TradingMode
from core.models import Position, Side
from core.asset_class import get_risk_profile
from core.portfolio import portfolio_tracker

if TYPE_CHECKING:
    from core.position_registry import PositionRegistry
    from core.trading_interfaces import IPositionPersistence, IPortfolioManager

logger = get_logger(__name__)


class ExchangeSyncer:
    """Syncs local state with exchange reality."""
    
    def __init__(
        self,
        mode: TradingMode,
        positions: dict,
        position_registry: "PositionRegistry",
        persistence: "IPositionPersistence",
        portfolio: "IPortfolioManager",
        config,
    ):
        self.mode = mode
        self.positions = positions
        self.position_registry = position_registry
        self.persistence = persistence
        self.portfolio = portfolio
        self.config = config
        
        # Coinbase client (initialized later for live mode)
        self._client = None
        
        # Cached values
        self._usd_balance: float = 0.0
        self._available_balance: float = 0.0  # What's actually tradeable
        self._portfolio_value: float = 0.0
        self._total_unrealized_pnl: float = 0.0
        self._portfolio_snapshot = None
        self._last_snapshot_at: Optional[datetime] = None
        self._exchange_holdings: dict[str, float] = {}
        self._holdings_detail: dict[str, dict] = {}  # Full details per holding
        self._product_info: dict[str, dict] = {}
        self._portfolio_uuid: Optional[str] = None
        
        # Delisted/unsupported coins to skip
        self.DELISTED_COINS = {'CLV', 'NU', 'BOND', 'SNX', 'MANA', 'CGLD'}
        
        # State flags
        self._sync_degraded: bool = False
        self._portfolio_log_interval_s: float = float(getattr(settings, "portfolio_log_interval_s", 60.0))
    
    @property
    def usd_balance(self) -> float:
        return self._usd_balance
    
    @property
    def available_balance(self) -> float:
        """What's actually available to trade (excludes staked/locked)."""
        return self._available_balance
    
    @property
    def portfolio_value(self) -> float:
        return self._portfolio_value
    
    @property
    def total_unrealized_pnl(self) -> float:
        return self._total_unrealized_pnl
    
    @property
    def portfolio_snapshot(self):
        return self._portfolio_snapshot
    
    @property
    def exchange_holdings(self) -> dict:
        return self._exchange_holdings
    
    @property
    def holdings_detail(self) -> dict:
        """Full details per holding including cost basis, entry price, PnL."""
        return self._holdings_detail
    
    def is_staked(self, symbol: str) -> bool:
        """Check if a position is staked and cannot be sold."""
        detail = self._holdings_detail.get(symbol, {})
        return detail.get('is_staked', False) or not detail.get('can_trade', True)
    
    @property
    def sync_degraded(self) -> bool:
        return self._sync_degraded
    
    def init_live_client(self):
        """Initialize Coinbase client for live trading."""
        if self.mode != TradingMode.LIVE:
            return
        
        try:
            from coinbase.rest import RESTClient
            self._client = RESTClient(
                api_key=settings.coinbase_api_key,
                api_secret=settings.coinbase_api_secret
            )
            logger.info("[SYNC] Live client initialized")
            self._fetch_portfolio_uuid()
        except Exception as e:
            logger.error("[SYNC] Failed to init live client: %s", e, exc_info=True)
            self._client = None
    
    def _fetch_portfolio_uuid(self):
        """Fetch the default portfolio UUID for API calls."""
        if not self._client:
            return
        try:
            portfolios = self._client.get_portfolios()
            for p in getattr(portfolios, 'portfolios', []):
                p_type = p.get('type') if isinstance(p, dict) else getattr(p, 'type', '')
                p_uuid = p.get('uuid') if isinstance(p, dict) else getattr(p, 'uuid', '')
                if p_type == 'DEFAULT' and p_uuid:
                    self._portfolio_uuid = p_uuid
                    logger.info("[SYNC] Using portfolio UUID: %s", p_uuid)
                    return
            # Fallback to first portfolio
            if portfolios.portfolios:
                first = portfolios.portfolios[0]
                self._portfolio_uuid = first.get('uuid') if isinstance(first, dict) else getattr(first, 'uuid', '')
                logger.info("[SYNC] Using first portfolio UUID: %s", self._portfolio_uuid)
        except Exception as e:
            logger.warning("[SYNC] Failed to fetch portfolio UUID: %s", e)
    
    def refresh_full_portfolio(self):
        """
        Refresh portfolio using breakdown API for full details.
        Gets cost basis, entry price, unrealized PnL, available balance per holding.
        """
        if not self._client or not self._portfolio_uuid:
            self.refresh_balance()  # Fallback to basic refresh
            return
        
        try:
            breakdown = self._client.get_portfolio_breakdown(self._portfolio_uuid)
            if not breakdown or not hasattr(breakdown, 'breakdown'):
                self.refresh_balance()
                return
            
            pb = breakdown.breakdown
            data = pb.to_dict() if hasattr(pb, 'to_dict') else pb
            
            # Get balances
            balances = data.get('portfolio_balances', {})
            cash_bal = balances.get('total_cash_equivalent_balance', {})
            crypto_bal = balances.get('total_crypto_balance', {})
            
            self._usd_balance = float(cash_bal.get('value', 0) if isinstance(cash_bal, dict) else 0)
            crypto_value = float(crypto_bal.get('value', 0) if isinstance(crypto_bal, dict) else 0)
            self._portfolio_value = self._usd_balance + crypto_value
            
            # Reset holdings
            self._exchange_holdings.clear()
            self._holdings_detail.clear()
            self._total_unrealized_pnl = 0.0
            self._available_balance = self._usd_balance
            
            # Get spot positions with full details
            spot_positions = data.get('spot_positions', [])
            for pos in spot_positions:
                asset = pos.get('asset', '')
                
                # Skip cash and delisted
                if asset in ('USD', 'USDC') or asset in self.DELISTED_COINS:
                    continue
                
                value_usd = float(pos.get('total_balance_fiat', 0))
                if value_usd < settings.position_dust_usd:
                    continue
                
                symbol = f"{asset}-USD"
                
                # Get all the good stuff
                cost_basis_data = pos.get('cost_basis', {})
                cost_basis = float(cost_basis_data.get('value', 0) if isinstance(cost_basis_data, dict) else 0)
                
                entry_price_data = pos.get('average_entry_price', {})
                entry_price = float(entry_price_data.get('value', 0) if isinstance(entry_price_data, dict) else 0)
                
                unrealized_pnl = float(pos.get('unrealized_pnl', 0))
                self._total_unrealized_pnl += unrealized_pnl
                
                quantity = float(pos.get('total_balance_crypto', 0))
                available_qty = float(pos.get('available_to_trade_crypto', 0))
                available_fiat = float(pos.get('available_to_trade_fiat', 0))
                allocation = float(pos.get('allocation', 0)) * 100
                
                # Current price
                current_price = value_usd / quantity if quantity > 0 else 0
                
                # PnL %
                pnl_pct = (unrealized_pnl / cost_basis * 100) if cost_basis > 0 else 0
                
                # Store basic holdings (for gate checks)
                self._exchange_holdings[symbol] = value_usd
                
                # Store full details (for smart trading)
                self._holdings_detail[symbol] = {
                    'asset': asset,
                    'symbol': symbol,
                    'quantity': quantity,
                    'available_qty': available_qty,
                    'value_usd': value_usd,
                    'available_usd': available_fiat,
                    'cost_basis': cost_basis,
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'unrealized_pnl': unrealized_pnl,
                    'pnl_pct': pnl_pct,
                    'allocation': allocation,
                    'is_staked': pos.get('account_type') == 'ACCOUNT_TYPE_STAKED_FUNDS',
                    'can_trade': available_qty > 0,
                }
                
                # Add available crypto value to available balance
                self._available_balance += available_fiat
            
            holdings = len(self._holdings_detail)
            key = "sync_portfolio_full"
            if should_emit(key, self._portfolio_log_interval_s):
                logger.info(
                    "[SYNC] Portfolio: $%.2f total, $%.2f available, %d holdings, PnL: $%.2f",
                    self._portfolio_value,
                    self._available_balance,
                    holdings,
                    self._total_unrealized_pnl,
                )
            self._sync_degraded = False
            
        except Exception as e:
            logger.warning("[SYNC] Full portfolio refresh failed, using basic: %s", e)
            self.refresh_balance()
    
    def refresh_balance(self):
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
                elif currency in self.DELISTED_COINS:
                    continue  # Skip delisted coins silently
                elif value > settings.position_dust_usd:
                    symbol = f"{currency}-USD"
                    
                    if symbol in settings.ignored_symbol_set:
                        continue
                    
                    try:
                        product = self._client.get_product(symbol)
                        price = float(getattr(product, 'price', 0))
                        position_value = value * price
                        holdings_value += position_value
                        if position_value >= settings.position_min_usd:
                            self._exchange_holdings[symbol] = position_value
                    except Exception as e:
                        # Silently skip coins that can't be priced (likely delisted)
                        logger.debug("[SYNC] Skipping %s: %s", symbol, e)
            
            self._usd_balance = usd_bal + usdc_bal
            self._portfolio_value = self._usd_balance + holdings_value
            
            # Validate balance
            if self._portfolio_value < 50:
                fallback_value = self.portfolio.get_total_portfolio_value() if self.portfolio else 0
                if fallback_value > 100:
                    logger.warning("[SYNC] API balance low ($%.2f), using fallback ($%.2f)",
                                  self._portfolio_value, fallback_value)
                    self._portfolio_value = fallback_value
                    self._usd_balance = self.portfolio.get_available_balance() if self.portfolio else 0
                    self._sync_degraded = False
                else:
                    logger.error("[SYNC] API balance low ($%.2f) - marking degraded", self._portfolio_value)
                    self._sync_degraded = True
                    return
            else:
                self._sync_degraded = False
            
            key = "sync_portfolio_basic"
            if should_emit(key, self._portfolio_log_interval_s):
                logger.info(
                    "[SYNC] Portfolio: $%.2f (Cash: $%.2f, Holdings: $%.2f)",
                    self._portfolio_value,
                    self._usd_balance,
                    holdings_value,
                )
            
            # Get real portfolio snapshot
            try:
                self._portfolio_snapshot = portfolio_tracker.get_snapshot()
                if self._portfolio_snapshot:
                    self._last_snapshot_at = datetime.now(timezone.utc)
                    self._sync_degraded = False
                    key = "sync_real_pnl"
                    if should_emit(key, self._portfolio_log_interval_s):
                        logger.info(
                            "[SYNC] Real P&L: $%+.2f (%s positions)",
                            self._portfolio_snapshot.total_unrealized_pnl,
                            self._portfolio_snapshot.position_count,
                        )
            except Exception as pe:
                logger.warning("[SYNC] Portfolio snapshot failed: %s", pe)
                
        except Exception as e:
            logger.error("[SYNC] Balance check failed: %s", e, exc_info=True)
            self._sync_degraded = True
    
    def verify_exchange_truth(self) -> bool:
        """Verify our local positions match exchange reality."""
        if self.mode != TradingMode.LIVE:
            return True
        
        try:
            snapshot = None
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
            
            missing_local = exchange_symbols - local_symbols
            extra_local = local_symbols - exchange_symbols
            
            if missing_local or extra_local:
                logger.error("[TRUTH] Position drift! Missing: %s, Extra: %s", missing_local, extra_local)
                return self._recover_from_drift(exchange_positions)
            
            # Verify quantities
            for symbol in local_symbols & exchange_symbols:
                local_pos = self.positions[symbol]
                exchange_pos = exchange_positions[symbol]
                
                qty_diff = abs(local_pos.size_qty - exchange_pos.qty)
                if qty_diff > settings.position_qty_drift_tolerance:
                    logger.warning("[TRUTH] Quantity drift for %s: Local=%.8f, Exchange=%.8f",
                                  symbol, local_pos.size_qty, exchange_pos.qty)
            
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
            self.positions.clear()
            self.position_registry = type(self.position_registry)(self.config)
            
            for symbol, exchange_pos in exchange_positions.items():
                if exchange_pos.value_usd >= settings.position_min_usd:
                    try:
                        risk_profile = get_risk_profile(symbol)
                        time_stop_min = int(risk_profile.max_hold_hours * 60)
                    except Exception:
                        time_stop_min = int(getattr(settings, "max_hold_minutes", 120) or 120)

                    position = Position(
                        symbol=symbol,
                        side=Side.BUY,
                        entry_price=exchange_pos.entry_price,
                        entry_time=datetime.now(timezone.utc),
                        size_usd=exchange_pos.value_usd,
                        size_qty=exchange_pos.qty,
                        stop_price=exchange_pos.entry_price * (1 - settings.fixed_stop_pct),
                        tp1_price=exchange_pos.entry_price * (1 + settings.tp1_pct),
                        tp2_price=exchange_pos.entry_price * (1 + settings.tp2_pct),
                        time_stop_min=time_stop_min,
                        entry_cost_usd=exchange_pos.cost_basis,
                        strategy_id="recovered"
                    )
                    
                    self.positions[symbol] = position
                    self.position_registry.add_position(position)
            
            self.persistence.save_positions(self.positions)
            logger.info("[TRUTH] Recovery complete: %d positions restored", len(self.positions))
            return True
            
        except Exception as e:
            logger.error("[TRUTH] Recovery failed: %s", e)
            return False
    
    def validate_before_trade(self, symbol: str, get_price_func) -> bool:
        """Validate system state before placing any trade."""
        
        # Handle degraded state
        if self.mode == TradingMode.LIVE and self._sync_degraded:
            try:
                self.update_cached_balances()
            except Exception:
                pass
            
            if self._sync_degraded and self._portfolio_value > 100:
                logger.warning("[TRUTH] Sync degraded but allowing trade (portfolio=$%.0f)", self._portfolio_value)
                self._sync_degraded = False
            elif self._sync_degraded:
                logger.info("[TRUTH] Trading paused - no valid portfolio data")
                return False
        
        # Check exchange sync
        if not self.verify_exchange_truth():
            if self._portfolio_value > 100:
                logger.warning("[TRUTH] Sync failed but local state valid ($%.0f)", self._portfolio_value)
            else:
                logger.error("[TRUTH] Cannot trade - sync failed and portfolio too low ($%.2f)", self._portfolio_value)
                return False
        
        # Verify price data
        current_price = get_price_func(symbol)
        if current_price <= 0:
            logger.error("[TRUTH] Cannot trade %s - no price data", symbol)
            return False
        
        # Check for existing exchange position
        if self.mode == TradingMode.LIVE and hasattr(self.portfolio, 'get_snapshot'):
            snapshot = self.portfolio.get_snapshot()
            if snapshot:
                exchange_pos = snapshot.positions.get(symbol)
                if exchange_pos and exchange_pos.value_usd >= settings.position_min_usd:
                    logger.warning("[TRUTH] %s position already exists on exchange", symbol)
                    return False
        
        return True
    
    def update_cached_balances(self):
        """Refresh cached balance/portfolio values from injected portfolio manager."""
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
    
    def get_product_info(self, symbol: str) -> dict:
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
            logger.warning("[SYNC] Product info failed for %s: %s", symbol, e)
            return {"quote_min": 1.0, "base_min": 0.0001}
    
    def check_minimum(self, symbol: str, usd_amount: float) -> bool:
        """Check if order meets minimum requirements."""
        info = self.get_product_info(symbol)
        if usd_amount < info["quote_min"]:
            logger.info("[SYNC] %s: $%.2f below min $%s", symbol, usd_amount, info["quote_min"])
            return False
        return True
    
    async def verify_position_created(self, symbol: str, expected_qty: float):
        """Verify that a position was actually created on the exchange."""
        import asyncio
        await asyncio.sleep(2)
        
        try:
            if self.mode == TradingMode.LIVE and hasattr(self.portfolio, 'get_snapshot'):
                snapshot = self.portfolio.get_snapshot()
                if snapshot:
                    exchange_pos = snapshot.positions.get(symbol)
                    if exchange_pos and exchange_pos.qty >= expected_qty * settings.position_verify_tolerance:
                        logger.info("[TRUTH] ✓ Position verified: %s %.6f", symbol, exchange_pos.qty)
                    else:
                        logger.warning("[TRUTH] ⚠ Position NOT found on exchange: %s", symbol)
                        if symbol in self.positions:
                            self.positions[symbol].strategy_id += "_UNVERIFIED"
                            self.persistence.save_positions(self.positions)
        except Exception as e:
            logger.error("[TRUTH] Position verification failed for %s: %s", symbol, e)
    
    def prune_dust_positions(self, source: str = "load"):
        """Remove tiny positions that would immediately stop out."""
        from core.helpers import is_dust
        import core.persistence as persistence_backend
        
        dusty = [s for s, p in self.positions.items() if is_dust(getattr(p, "size_usd", 0))]
        for sym in dusty:
            try:
                val = getattr(self.positions[sym], "size_usd", 0)
            except Exception:
                val = 0
            logger.info("[SYNC] Dropping dust position %s ($%.4f) from %s", sym, val, source)
            self.positions.pop(sym, None)
            try:
                persistence_backend.clear_position(sym, self.mode)
            except Exception:
                pass
    
    def sync_position_stores(self):
        """Keep position registry in sync with positions dict."""
        for symbol, pos in self.positions.items():
            if not self.position_registry.has_position(symbol):
                self.position_registry.add_position(pos)
        
        for symbol in list(self.position_registry.get_all_positions().keys()):
            if symbol not in self.positions:
                self.position_registry.remove_position(symbol)
