"""
Order Manager - Complete order lifecycle management.

Handles:
1. Order state sync with exchange
2. Real stop-loss orders on Coinbase
3. Position lifecycle (open → manage → close)
4. Order tracking and reconciliation
5. Retry logic with exponential backoff
6. Rate limiting
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List
from enum import Enum
import json
import time

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
        
    def init_client(self, client):
        """Set the Coinbase client."""
        self._client = client
        
    def sync_with_exchange(self) -> int:
        """
        Sync local order state with exchange.
        Returns number of orders synced.
        """
        if not self._client:
            print("[ORDERS] No client, skipping sync")
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
            print(f"[ORDERS] Synced {synced} orders ({len(open_list)} open, {len(filled_list)} recent)")
            
            # Identify stop orders and link to positions
            self._link_stop_orders()
            
            return synced
            
        except Exception as e:
            print(f"[ORDERS] Sync failed: {e}")
            return 0
    
    def _parse_order(self, order) -> Optional[ManagedOrder]:
        """Parse Coinbase order response into ManagedOrder."""
        try:
            order_id = getattr(order, 'order_id', '') or order.get('order_id', '')
            client_id = getattr(order, 'client_order_id', '') or order.get('client_order_id', '')
            product = getattr(order, 'product_id', '') or order.get('product_id', '')
            side = getattr(order, 'side', '') or order.get('side', '')
            status_str = getattr(order, 'status', '') or order.get('status', '')
            
            # Determine order type
            order_config = getattr(order, 'order_configuration', {}) or order.get('order_configuration', {})
            if 'stop_limit' in str(order_config).lower():
                order_type = OrderType.STOP_LIMIT
            elif 'limit' in str(order_config).lower():
                order_type = OrderType.LIMIT
            else:
                order_type = OrderType.MARKET
            
            # Parse status
            try:
                status = OrderStatus[status_str] if status_str else OrderStatus.PENDING
            except KeyError:
                status = OrderStatus.PENDING
            
            # Get prices and sizes
            filled_size = float(getattr(order, 'filled_size', 0) or order.get('filled_size', 0) or 0)
            filled_value = float(getattr(order, 'filled_value', 0) or order.get('filled_value', 0) or 0)
            avg_price = float(getattr(order, 'average_filled_price', 0) or order.get('average_filled_price', 0) or 0)
            fee = float(getattr(order, 'total_fees', 0) or order.get('total_fees', 0) or 0)
            
            # Check if this is a stop order (by client_id convention)
            is_stop = 'stop_' in client_id.lower() if client_id else False
            
            return ManagedOrder(
                order_id=order_id,
                client_order_id=client_id,
                symbol=product,
                side=side,
                order_type=order_type,
                status=status,
                price=avg_price,
                filled_qty=filled_size,
                filled_value=filled_value,
                fee=fee,
                is_stop_order=is_stop
            )
        except Exception as e:
            print(f"[ORDERS] Failed to parse order: {e}")
            return None
    
    def _link_stop_orders(self):
        """Link stop orders to their positions."""
        for order_id, order in self._orders.items():
            if order.is_stop_order and order.status == OrderStatus.OPEN:
                symbol = order.symbol
                if symbol not in self._position_orders:
                    self._position_orders[symbol] = PositionOrders(symbol=symbol)
                self._position_orders[symbol].stop_order_id = order_id
                print(f"[ORDERS] Linked stop order {order_id[:8]}... to {symbol}")
    
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
            print("[ORDERS] No client, can't place stop order")
            return None
        
        # Default limit price with wider gap for flash crashes (2% below stop)
        if limit_price is None:
            limit_price = calculate_limit_price(stop_price)  # 0.98 = 2% gap
        
        client_order_id = f"stop_{symbol}_{int(datetime.now().timestamp())}"
        
        last_error = None
        for attempt in range(max_retries):
            try:
                # Rate limit
                rate_limiter.wait_if_needed()
                
                order = self._client.stop_limit_order_gtc_sell(
                    client_order_id=client_order_id,
                    product_id=symbol,
                    base_size=str(qty),
                    stop_price=str(stop_price),
                    limit_price=str(limit_price)
                )
                
                # Get order ID from response
                order_id = getattr(order, 'order_id', None)
                if not order_id and hasattr(order, 'success_response'):
                    order_id = getattr(order.success_response, 'order_id', None)
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
                    
                    print(f"[ORDERS] ✅ Stop order placed: {symbol} @ ${stop_price:.4f} (limit ${limit_price:.4f})")
                    
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
                    
                    return order_id
                else:
                    print(f"[ORDERS] Stop order placed but no ID returned")
                    return None
                    
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                
                # Don't retry fatal errors
                if 'insufficient' in error_str or 'invalid' in error_str:
                    print(f"[ORDERS] ❌ Fatal error placing stop: {e}")
                    return None
                
                if attempt < max_retries - 1:
                    delay = 0.5 * (2 ** attempt)  # Exponential backoff
                    print(f"[ORDERS] ⚠️ Retry {attempt + 1}/{max_retries} for stop order: {e}")
                    time.sleep(delay)
        
        print(f"[ORDERS] ❌ Failed to place stop after {max_retries} attempts: {last_error}")
        return None
    
    def cancel_stop_order(self, symbol: str) -> bool:
        """Cancel the stop order for a position."""
        if not self._client:
            return False
        
        pos_orders = self._position_orders.get(symbol)
        if not pos_orders or not pos_orders.stop_order_id:
            print(f"[ORDERS] No stop order found for {symbol}")
            return True  # Nothing to cancel
        
        order_id = pos_orders.stop_order_id
        
        try:
            self._client.cancel_orders([order_id])
            
            # Update local state
            if order_id in self._orders:
                self._orders[order_id].status = OrderStatus.CANCELLED
            pos_orders.stop_order_id = None
            
            print(f"[ORDERS] ✅ Cancelled stop order for {symbol}")
            return True
            
        except Exception as e:
            print(f"[ORDERS] Failed to cancel stop order: {e}")
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
        stop = self.get_stop_order(symbol)
        return stop is not None and stop.status == OrderStatus.OPEN
    
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
