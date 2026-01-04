"""
Order Manager - Complete order lifecycle management.

Handles:
1. Order state sync with exchange
2. Real stop-loss orders on Coinbase
3. Position lifecycle (open → manage → close)
4. Order tracking and reconciliation
5. Retry logic with exponential backoff
6. Rate limiting
7. Understands some positions can't be sold delisted, too small of value to make minimum sale fee.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List
from enum import Enum
import json
import time
from decimal import Decimal, ROUND_HALF_UP

from core.logging_utils import get_logger

logger = get_logger(__name__)

from core.config import settings
from execution.order_utils import (
    rate_limiter, with_retry, OrderResult, parse_order_response,
    calculate_limit_price, OrderFatalError, OrderRetryableError
)


class OrderStatus(Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LIMIT = "STOP_LIMIT"


@dataclass
class ManagedOrder:
    """An order tracked by the manager."""
    order_id: str
    client_order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"
    order_type: OrderType
    status: OrderStatus
    
    # Prices
    price: float = 0.0           # Limit price or fill price
    stop_price: float = 0.0      # For stop orders
    
    # Sizes
    size_qty: float = 0.0        # Base size
    size_usd: float = 0.0        # Quote size
    filled_qty: float = 0.0
    filled_value: float = 0.0
    
    # Fees
    fee: float = 0.0
    
    # Timestamps
    created_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    
    # Link to position
    position_symbol: Optional[str] = None
    is_stop_order: bool = False


@dataclass
class PositionOrders:
    """Orders associated with a position."""
    symbol: str
    entry_order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None


class OrderManager:
    """
    Manages order lifecycle on Coinbase.
    
    Key responsibilities:
    1. Track all orders (open, filled, cancelled)
    2. Place and manage stop-loss orders
    3. Sync state with exchange on startup
    4. Provide order status for positions
    """
    
    def __init__(self):
        self._client = None
        self._orders: Dict[str, ManagedOrder] = {}  # order_id -> order
        self._position_orders: Dict[str, PositionOrders] = {}  # symbol -> orders
        self._last_sync: Optional[datetime] = None
        self._available_base_cache: Dict[str, tuple[float, float]] = {}
        self._available_base_cache_ttl_s: float = 10.0  # Reduced from 30s to avoid stale data
        
    def init_client(self, client):
        """Set the Coinbase client."""
        self._client = client

    def _get_available_base_qty(self, base_asset: str, force_refresh: bool = False) -> float:
        """Get available balance for base asset. Use force_refresh=True for critical operations like stop orders."""
        if not self._client or not base_asset:
            return 0.0

        now = time.time()
        cached = self._available_base_cache.get(base_asset)
        if not force_refresh and cached and (now - cached[1]) < self._available_base_cache_ttl_s:
            return float(cached[0])

        available = 0.0
        try:
            accounts = self._client.get_accounts()
            for acct in getattr(accounts, "accounts", []) or []:
                if isinstance(acct, dict):
                    currency = acct.get("currency", "")
                    bal = acct.get("available_balance", {})
                    value = bal.get("value", 0) if isinstance(bal, dict) else 0
                else:
                    currency = getattr(acct, "currency", "")
                    bal = getattr(acct, "available_balance", {})
                    value = bal.get("value", 0) if isinstance(bal, dict) else getattr(bal, "value", 0)
                if currency == base_asset:
                    try:
                        available = float(value or 0)
                    except Exception:
                        available = 0.0
                    break
        except Exception:
            available = 0.0

        self._available_base_cache[base_asset] = (available, now)
        return float(available)
        
    def sync_with_exchange(self) -> int:
        """
        Sync local order state with exchange.
        Returns number of orders synced.
        """
        if not self._client:
            logger.info("[ORDERS] No client, skipping sync")
            return 0
        
        try:
            # Get open orders
            open_orders = self._client.list_orders(order_status="OPEN")
            open_list = getattr(open_orders, 'orders', [])
            
            # Get recent filled orders (last 100)
            filled_orders = self._client.list_orders(limit=100)
            filled_list = getattr(filled_orders, 'orders', [])
            
            synced = 0
            
            for order in open_list + filled_list:
                order_id = getattr(order, 'order_id', '') or order.get('order_id', '')
                if not order_id:
                    continue
                
                managed = self._parse_order(order)
                if managed:
                    self._orders[order_id] = managed
                    synced += 1
            
            self._last_sync = datetime.now(timezone.utc)
            logger.info("[ORDERS] Synced %d orders (%d open, %d recent)", synced, len(open_list), len(filled_list))
            
            # Identify stop orders and link to positions
            self._link_stop_orders()
            
            # Persist orders to disk for comparison
            self._persist_orders()
            
            return synced
            
        except Exception as e:
            logger.warning("[ORDERS] Sync failed: %s", e)
            return 0
    
    def _parse_order(self, order) -> Optional[ManagedOrder]:
        """Parse Coinbase order response into ManagedOrder."""
        try:
            order_id = getattr(order, 'order_id', '') or order.get('order_id', '')
            client_id = getattr(order, 'client_order_id', '') or order.get('client_order_id', '')
            product = getattr(order, 'product_id', '') or order.get('product_id', '')
            side = getattr(order, 'side', '') or order.get('side', '')
            status_str = getattr(order, 'status', '') or order.get('status', '')
            
            # Determine order type and extract config
            order_config = getattr(order, 'order_configuration', {}) or order.get('order_configuration', {})
            order_config_str = str(order_config).lower()
            
            is_stop_limit = 'stop_limit' in order_config_str
            if is_stop_limit:
                order_type = OrderType.STOP_LIMIT
            elif 'limit' in order_config_str:
                order_type = OrderType.LIMIT
            else:
                order_type = OrderType.MARKET
            
            # Parse status
            try:
                status = OrderStatus[status_str] if status_str else OrderStatus.PENDING
            except KeyError:
                status = OrderStatus.PENDING
            
            # Get filled sizes
            filled_size = float(getattr(order, 'filled_size', 0) or order.get('filled_size', 0) or 0)
            filled_value = float(getattr(order, 'filled_value', 0) or order.get('filled_value', 0) or 0)
            avg_price = float(getattr(order, 'average_filled_price', 0) or order.get('average_filled_price', 0) or 0)
            fee = float(getattr(order, 'total_fees', 0) or order.get('total_fees', 0) or 0)
            
            # Extract base_size and stop_price from order_configuration
            # For stop_limit orders, the size is in order_configuration.stop_limit_stop_limit_gtc.base_size
            base_size = 0.0
            stop_price = 0.0
            limit_price = 0.0
            
            if isinstance(order_config, dict):
                # Try stop_limit_stop_limit_gtc (GTC stop limit)
                stop_config = order_config.get('stop_limit_stop_limit_gtc', {})
                if not stop_config:
                    # Try stop_limit_stop_limit_gtd (GTD stop limit)
                    stop_config = order_config.get('stop_limit_stop_limit_gtd', {})
                if not stop_config:
                    # Try limit configs for regular limit orders
                    stop_config = order_config.get('limit_limit_gtc', {}) or order_config.get('limit_limit_gtd', {})
                
                if stop_config:
                    base_size = float(stop_config.get('base_size', 0) or 0)
                    stop_price = float(stop_config.get('stop_price', 0) or 0)
                    limit_price = float(stop_config.get('limit_price', 0) or 0)
            
            # Use filled_size if base_size not available (for filled orders)
            size_qty = base_size if base_size > 0 else filled_size
            
            # Check if this is a stop order (by config or client_id convention)
            is_stop = is_stop_limit or ('stop_' in client_id.lower() if client_id else False)
            
            return ManagedOrder(
                order_id=order_id,
                client_order_id=client_id,
                symbol=product,
                side=side,
                order_type=order_type,
                status=status,
                price=limit_price if limit_price > 0 else avg_price,
                stop_price=stop_price,
                size_qty=size_qty,
                filled_qty=filled_size,
                filled_value=filled_value,
                fee=fee,
                is_stop_order=is_stop
            )
        except Exception as e:
            logger.warning("[ORDERS] Failed to parse order: %s", e)
            return None
    
    def _link_stop_orders(self):
        """Link stop orders to their positions."""
        for order_id, order in self._orders.items():
            if order.is_stop_order and order.status == OrderStatus.OPEN:
                symbol = order.symbol
                if symbol not in self._position_orders:
                    self._position_orders[symbol] = PositionOrders(symbol=symbol)
                self._position_orders[symbol].stop_order_id = order_id
                logger.info("[ORDERS] Linked stop order %s... to %s", order_id[:8], symbol)
    
    def _persist_orders(self):
        """Persist all orders to disk for comparison with exchange."""
        try:
            from execution.order_persistence import save_orders
            
            # Serialize orders
            orders_data = {}
            for order_id, order in self._orders.items():
                orders_data[order_id] = {
                    "order_id": order.order_id,
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side,
                    "order_type": order.order_type.value if order.order_type else None,
                    "status": order.status.value if order.status else None,
                    "stop_price": order.stop_price,
                    "price": order.price,
                    "size_qty": order.size_qty,
                    "is_stop_order": order.is_stop_order,
                    "created_at": order.created_at.isoformat() if order.created_at else None,
                }
            
            # Serialize position orders
            pos_orders_data = {}
            for symbol, pos_orders in self._position_orders.items():
                pos_orders_data[symbol] = {
                    "symbol": pos_orders.symbol,
                    "entry_order_id": pos_orders.entry_order_id,
                    "stop_order_id": pos_orders.stop_order_id,
                    "tp1_order_id": pos_orders.tp1_order_id,
                    "tp2_order_id": pos_orders.tp2_order_id,
                }
            
            save_orders(orders_data, pos_orders_data)
        except Exception as e:
            logger.warning("[ORDERS] Failed to persist orders: %s", e)
    
    def place_stop_order(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: Optional[float] = None,
        max_retries: int = 3
    ) -> Optional[str]:
        """
        Place a stop-loss order on the exchange with retry logic.
        Returns order_id if successful.
        """
        if not self._client:
            logger.info("[ORDERS] No client, can't place stop order")
            return None

        # Resolve price and size increments
        price_increment = 0.01
        base_increment = 0.00000001  # Default for most crypto
        base_min_size = 0.0
        try:
            product = self._client.get_product(symbol)
            price_increment = float(
                getattr(product, "price_increment", None)
                or getattr(product, "quote_increment", 0.01)
                or 0.01
            )
            base_increment = float(
                getattr(product, "base_increment", None)
                or 0.00000001
            )
            base_min_size = float(getattr(product, "base_min_size", 0) or 0)
        except Exception:
            price_increment = 0.01
            base_increment = 0.00000001
            base_min_size = 0.0

        def _quantize_price(value: float) -> float:
            """Round price to exchange-supported precision."""
            try:
                inc = Decimal(str(price_increment))
                return float(Decimal(str(value)).quantize(inc, rounding=ROUND_HALF_UP))
            except Exception:
                return round(value / price_increment) * price_increment if price_increment else value
        
        def _quantize_size(value: float) -> float:
            """Round size to exchange-supported precision."""
            try:
                inc = Decimal(str(base_increment))
                return float(Decimal(str(value)).quantize(inc, rounding=ROUND_DOWN))
            except Exception:
                return round(value / base_increment) * base_increment if base_increment else value
        
        # Default limit price with wider gap for flash crashes (2% below stop)
        if limit_price is None:
            limit_price = calculate_limit_price(stop_price)  # 0.98 = 2% gap

        try:
            base_asset = symbol.split("-")[0]
            # Force fresh balance fetch for stop orders to avoid INSUFFICIENT_FUND errors
            available_qty = self._get_available_base_qty(base_asset, force_refresh=True)
            if available_qty > 0 and qty > available_qty:
                logger.info("[ORDERS] Clamping stop qty %.8f -> %.8f (available) for %s", qty, available_qty * 0.999, symbol)
                qty = max(0.0, available_qty * 0.999)
        except Exception as e:
            logger.debug("[ORDERS] Could not fetch available balance for %s: %s", symbol, e)

        # Quantize prices and size to meet Coinbase precision requirements
        stop_price = _quantize_price(stop_price)
        limit_price = _quantize_price(limit_price)
        qty = _quantize_size(qty)

        min_qty = base_min_size if base_min_size and base_min_size > 0 else base_increment
        if qty <= 0 or (min_qty and qty < float(min_qty)):
            logger.warning("[ORDERS] Stop skipped (qty too small): %s qty=%.10f", symbol, qty)
            return None

        # Deduplicate: if an open stop already exists at effectively the same price, reuse it
        existing_stop = self.get_stop_order(symbol)
        if existing_stop and existing_stop.status == OrderStatus.OPEN:
            # Consider it the same if within one price increment
            if abs(existing_stop.stop_price - stop_price) < price_increment:
                logger.info("[ORDERS] Reusing existing stop for %s @ $%.4f", symbol, existing_stop.stop_price)
                return existing_stop.order_id
        
        client_order_id = f"stop_{symbol}_{int(datetime.now().timestamp())}"
        
        last_error = None
        for attempt in range(max_retries):
            try:
                # Rate limit
                rate_limiter.wait_if_needed()
                
                # Format numbers to avoid scientific notation (e.g. 2.2e-05)
                qty_str = f"{qty:.8f}".rstrip('0').rstrip('.')
                stop_str = f"{stop_price:.8f}".rstrip('0').rstrip('.')
                limit_str = f"{limit_price:.8f}".rstrip('0').rstrip('.')
                
                order = self._client.stop_limit_order_gtc_sell(
                    client_order_id=client_order_id,
                    product_id=symbol,
                    base_size=qty_str,
                    stop_price=stop_str,
                    limit_price=limit_str,
                    # Required by Advanced Trade API to indicate stop direction
                    stop_direction="STOP_DIRECTION_STOP_DOWN",
                )
                
                # Get order ID from response
                order_id = getattr(order, 'order_id', None)
                if not order_id and hasattr(order, 'success_response'):
                    sr = getattr(order, 'success_response', None)
                    order_id = (
                        getattr(sr, 'order_id', None)
                        or (sr.get('order_id') if isinstance(sr, dict) else None)
                    )
                if not order_id and isinstance(order, dict):
                    order_id = order.get('order_id') or order.get('success_response', {}).get('order_id')
                
                if order_id:
                    # Track the order
                    self._orders[order_id] = ManagedOrder(
                        order_id=order_id,
                        client_order_id=client_order_id,
                        symbol=symbol,
                        side="SELL",
                        order_type=OrderType.STOP_LIMIT,
                        status=OrderStatus.OPEN,
                        stop_price=stop_price,
                        price=limit_price,
                        size_qty=qty,
                        is_stop_order=True,
                        created_at=datetime.now(timezone.utc)
                    )
                    
                    # Link to position
                    if symbol not in self._position_orders:
                        self._position_orders[symbol] = PositionOrders(symbol=symbol)
                    self._position_orders[symbol].stop_order_id = order_id
                    
                    logger.info("[ORDERS] Stop order placed: %s @ $%.4f (limit $%.4f)", symbol, stop_price, limit_price)
                    
                    # Log stop order placement
                    from core.logger import log_stop_order, utc_iso_str
                    log_stop_order({
                        "ts": utc_iso_str(),
                        "type": "stop_placed",
                        "symbol": symbol,
                        "order_id": order_id,
                        "stop_price": stop_price,
                        "limit_price": limit_price,
                        "qty": qty,
                    })
                    
                    # Persist orders to disk
                    self._persist_orders()
                    
                    return order_id
                else:
                    # Surface error details when available
                    err_detail = None
                    if hasattr(order, "error_response"):
                        err_detail = getattr(order, "error_response", None)
                    elif isinstance(order, dict) and order.get("error_response"):
                        err_detail = order.get("error_response")

                    if err_detail:
                        logger.warning("[ORDERS] Stop order rejected: %s", err_detail)
                    else:
                        logger.info("[ORDERS] Stop order placed but no ID returned")
                    return None
                    
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                # Don't retry fatal errors
                if 'insufficient' in error_str or 'invalid' in error_str:
                    logger.error("[ORDERS] Fatal error placing stop: %s", e)
                    return None
                
                if attempt < max_retries - 1:
                    delay = 0.5 * (2 ** attempt)  # Exponential backoff
                    logger.warning("[ORDERS] Retry %d/%d for stop order: %s", attempt + 1, max_retries, e)
                    time.sleep(delay)
        
        logger.error("[ORDERS] Failed to place stop after %d attempts: %s", max_retries, last_error)
        return None
    
    def cancel_stop_order(self, symbol: str) -> bool:
        """Cancel the stop order for a position."""
        if not self._client:
            return False
        
        pos_orders = self._position_orders.get(symbol)
        if not pos_orders or not pos_orders.stop_order_id:
            logger.info("[ORDERS] No stop order found for %s", symbol)
            return True  # Nothing to cancel
        
        order_id = pos_orders.stop_order_id
        
        try:
            self._client.cancel_orders([order_id])
            
            # Update local state
            if order_id in self._orders:
                self._orders[order_id].status = OrderStatus.CANCELLED
            pos_orders.stop_order_id = None
            
            logger.info("[ORDERS] Cancelled stop order for %s", symbol)
            return True
            
        except Exception as e:
            logger.warning("[ORDERS] Failed to cancel stop order: %s", e)
            return False
    
    def get_open_orders(self, symbol: Optional[str] = None) -> List[ManagedOrder]:
        """Get all open orders, optionally filtered by symbol."""
        open_orders = [o for o in self._orders.values() if o.status == OrderStatus.OPEN]
        if symbol:
            open_orders = [o for o in open_orders if o.symbol == symbol]
        return open_orders
    
    def get_stop_order(self, symbol: str) -> Optional[ManagedOrder]:
        """Get the active stop order for a position."""
        pos_orders = self._position_orders.get(symbol)
        if pos_orders and pos_orders.stop_order_id:
            return self._orders.get(pos_orders.stop_order_id)
        return None
    
    def has_stop_order(self, symbol: str) -> bool:
        """Check if a position has an active stop order."""
        # First check linked position orders
        stop = self.get_stop_order(symbol)
        if stop is not None and stop.status == OrderStatus.OPEN:
            return True
        
        # Also check for ANY stop order for this symbol (catches orphaned orders)
        for order in self._orders.values():
            if (order.symbol == symbol and 
                order.order_type in ("stop_limit", "stop") and
                order.status == OrderStatus.OPEN):
                return True
        
        return False
    
    def update_stop_price(self, symbol: str, new_stop_price: float) -> bool:
        """
        Update stop order price (cancel and replace).
        Used for trailing stops.
        """
        pos_orders = self._position_orders.get(symbol)
        if not pos_orders or not pos_orders.stop_order_id:
            return False
        
        old_order = self._orders.get(pos_orders.stop_order_id)
        if not old_order:
            return False

        # De-dupe: if the requested price is effectively the same as the current stop,
        # do NOT churn cancel+replace (this spams stop_placed and can hit rate limits).
        try:
            price_increment = 0.01
            try:
                if self._client:
                    product = self._client.get_product(symbol)
                    price_increment = float(
                        getattr(product, "price_increment", None)
                        or getattr(product, "quote_increment", 0.01)
                        or 0.01
                    )
            except Exception:
                price_increment = 0.01

            current_stop = float(getattr(old_order, "stop_price", 0.0) or 0.0)
            if current_stop > 0 and abs(current_stop - float(new_stop_price)) < float(price_increment):
                logger.debug(
                    "[ORDERS] %s stop update skipped (no material change): %.8f ~ %.8f",
                    symbol,
                    current_stop,
                    float(new_stop_price),
                )
                return True
        except Exception:
            # If dedupe fails, fall back to normal update behavior.
            pass
        
        # Cancel old, place new
        if self.cancel_stop_order(symbol):
            new_order_id = self.place_stop_order(
                symbol=symbol,
                qty=old_order.size_qty,
                stop_price=new_stop_price
            )
            return new_order_id is not None
        
        return False
    
    def print_status(self):
        """Print current order status."""
        open_orders = self.get_open_orders()
        print(f"\n[ORDERS] Status: {len(open_orders)} open orders")
        for order in open_orders[:10]:
            stop_label = " [STOP]" if order.is_stop_order else ""
            print(f"  {order.symbol}: {order.side} @ ${order.stop_price or order.price:.4f}{stop_label}")


# Singleton instance
order_manager = OrderManager()
