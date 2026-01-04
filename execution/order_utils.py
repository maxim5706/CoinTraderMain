"""
Order execution utilities with retry logic, rate limiting, and error handling.
"""

import time
import asyncio
from datetime import datetime, timezone
from typing import Optional, Callable, Any, TypeVar
from dataclasses import dataclass
from enum import Enum
from functools import wraps

from core.logging_utils import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


class OrderError(Exception):
    """Base exception for order errors."""
    pass


class OrderRetryableError(OrderError):
    """Error that can be retried."""
    pass


class OrderFatalError(OrderError):
    """Error that should not be retried."""
    pass


@dataclass
class RateLimiter:
    """
    Simple rate limiter for API calls.
    Coinbase Advanced Trade: 10 requests/second for private endpoints.
    """
    max_requests: int = 8  # Conservative limit (below 10)
    window_seconds: float = 1.0
    _requests: list = None
    
    def __post_init__(self):
        self._requests = []
    
    def wait_if_needed(self):
        """Block if we've exceeded rate limit."""
        now = time.time()
        
        # Remove old requests outside window
        self._requests = [t for t in self._requests if now - t < self.window_seconds]
        
        if len(self._requests) >= self.max_requests:
            # Wait for oldest request to expire
            sleep_time = self.window_seconds - (now - self._requests[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
            self._requests = self._requests[1:]
        
        self._requests.append(now)
    
    async def async_wait_if_needed(self):
        """Async version of wait_if_needed."""
        now = time.time()
        self._requests = [t for t in self._requests if now - t < self.window_seconds]
        
        if len(self._requests) >= self.max_requests:
            sleep_time = self.window_seconds - (now - self._requests[0])
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            self._requests = self._requests[1:]
        
        self._requests.append(now)


# Global rate limiter instance
rate_limiter = RateLimiter()


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    retryable_exceptions: tuple = (Exception,)
):
    """
    Decorator for retry logic with exponential backoff.
    
    Args:
        max_attempts: Maximum number of attempts
        base_delay: Initial delay between retries (seconds)
        max_delay: Maximum delay between retries
        retryable_exceptions: Tuple of exceptions that trigger retry
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_error = None
            
            for attempt in range(max_attempts):
                try:
                    # Rate limit before each attempt
                    rate_limiter.wait_if_needed()
                    return func(*args, **kwargs)
                    
                except OrderFatalError:
                    # Don't retry fatal errors
                    raise
                    
                except retryable_exceptions as e:
                    last_error = e
                    
                    # Check if it's a rate limit error
                    error_str = str(e).lower()
                    is_rate_limit = 'rate' in error_str or '429' in error_str
                    is_timeout = 'timeout' in error_str
                    is_temporary = 'temporary' in error_str or '503' in error_str or '502' in error_str
                    
                    if not (is_rate_limit or is_timeout or is_temporary):
                        # Non-retryable API error
                        if 'insufficient' in error_str or 'balance' in error_str:
                            raise OrderFatalError(f"Insufficient funds: {e}")
                        if 'invalid' in error_str and 'size' in error_str:
                            raise OrderFatalError(f"Invalid order size: {e}")
                    
                    if attempt < max_attempts - 1:
                        # Exponential backoff with jitter
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        delay += delay * 0.1 * (time.time() % 1)  # Add jitter
                        
                        logger.info("[RETRY] Attempt %d/%d failed: %s", attempt + 1, max_attempts, e)
                        logger.info("[RETRY] Waiting %.1fs before retry...", delay)
                        time.sleep(delay)
            
            raise OrderRetryableError(f"Failed after {max_attempts} attempts: {last_error}")
        
        return wrapper
    return decorator


def with_retry_async(
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 5.0,
    retryable_exceptions: tuple = (Exception,)
):
    """Async version of with_retry decorator."""
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_error = None
            
            for attempt in range(max_attempts):
                try:
                    await rate_limiter.async_wait_if_needed()
                    return await func(*args, **kwargs)
                    
                except OrderFatalError:
                    raise
                    
                except retryable_exceptions as e:
                    last_error = e
                    error_str = str(e).lower()
                    
                    is_rate_limit = 'rate' in error_str or '429' in error_str
                    is_timeout = 'timeout' in error_str
                    is_temporary = 'temporary' in error_str or '503' in error_str
                    
                    if not (is_rate_limit or is_timeout or is_temporary):
                        if 'insufficient' in error_str or 'balance' in error_str:
                            raise OrderFatalError(f"Insufficient funds: {e}")
                    
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        logger.info("[RETRY] Attempt %d/%d failed: %s", attempt + 1, max_attempts, e)
                        await asyncio.sleep(delay)
            
            raise OrderRetryableError(f"Failed after {max_attempts} attempts: {last_error}")
        
        return wrapper
    return decorator


@dataclass
class OrderResult:
    """Result of an order execution."""
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    filled_value: Optional[float] = None
    fees: Optional[float] = None
    status: str = ""
    error: Optional[str] = None
    partial_fill: bool = False
    
    @property
    def is_partial(self) -> bool:
        return self.partial_fill


def parse_order_response(order, expected_qty: float = 0, expected_quote: float = 0, market_price: float = 0) -> OrderResult:
    """
    Parse Coinbase order response into OrderResult.
    Handles both object and dict responses.
    
    Args:
        order: The order response from Coinbase API
        expected_qty: Expected base quantity (for limit orders)
        expected_quote: Expected quote amount in USD (for market orders with quote_size)
    """
    try:
        # Extract fields - handle both object and dict
        def get_field(name, default=None):
            if hasattr(order, name):
                return getattr(order, name)
            if isinstance(order, dict):
                return order.get(name, default)
            return default
        
        order_id = get_field('order_id', '')
        if not order_id and hasattr(order, 'success_response'):
            order_id = getattr(order.success_response, 'order_id', '')
        if not order_id and isinstance(order, dict) and 'success_response' in order:
            order_id = order['success_response'].get('order_id', '')
        
        status = get_field('status', '')
        filled_size = float(get_field('filled_size', 0) or 0)
        filled_value = float(get_field('filled_value', 0) or 0)
        avg_price = float(get_field('average_filled_price', 0) or 0)
        fees = float(get_field('total_fees', 0) or 0)
        
        # Check for success
        success = bool(order_id) or get_field('success', False)
        
        # Determine if partial fill
        # For market orders with quote_size, check filled_value vs expected_quote (5% tolerance due to fees/slippage)
        # For limit orders, check filled_size vs expected_qty (1% tolerance)
        partial_fill = False
        if expected_quote > 0 and filled_value > 0:
            # Market order: compare filled USD value
            partial_fill = filled_value < expected_quote * 0.95
        elif expected_qty > 0 and filled_size > 0:
            # Limit order: compare filled quantity
            partial_fill = filled_size < expected_qty * 0.99
        
        # CRITICAL FIX: For orders where filled_size is 0 but we have an order_id,
        # the order was placed successfully. For limit orders, it may not fill immediately.
        # Estimate fill from expected values to prevent position tracking failures!
        estimated_qty = None
        estimated_price = None
        if success and filled_size <= 0:
            if expected_quote > 0:
                # Market order succeeded but no fill data yet - estimate!
                if market_price > 0:
                    estimated_price = market_price
                    estimated_qty = expected_quote / market_price
                    logger.warning("[ORDER] Market order %s succeeded but no fill data - using market price $%.4f", order_id, market_price)
                else:
                    logger.error("[ORDER] Market order %s succeeded but no fill data AND no market price - SKIPPING", order_id)
                    return OrderResult(success=False, error="No fill data and no market price")
            elif expected_qty > 0:
                # LIMIT order placed successfully - use expected qty and market price
                # The order is on the books and will fill (or has filled) at limit price
                if market_price > 0:
                    estimated_price = market_price
                    estimated_qty = expected_qty
                    logger.info("[ORDER] Limit order %s placed successfully - tracking with qty=%.4f @ $%.4f", order_id, expected_qty, market_price)
                else:
                    logger.error("[ORDER] Limit order %s placed but no market price for tracking - SKIPPING", order_id)
                    return OrderResult(success=False, error="Limit order placed but no market price")
        
        return OrderResult(
            success=success,
            order_id=order_id,
            fill_price=avg_price if avg_price > 0 else estimated_price,
            fill_qty=filled_size if filled_size > 0 else estimated_qty,
            filled_value=filled_value if filled_value > 0 else expected_quote if expected_quote > 0 else None,
            fees=fees if fees > 0 else None,
            status=status,
            partial_fill=partial_fill
        )
        
    except Exception as e:
        return OrderResult(
            success=False,
            error=str(e)
        )


# Stop-limit gap configuration
STOP_LIMIT_GAP_PCT = 0.98  # 2% below stop price (was 0.995 = 0.5%)


def calculate_limit_price(stop_price: float) -> float:
    """
    Calculate limit price for stop-limit order.
    Uses wider gap (2%) to handle flash crashes.
    """
    return stop_price * STOP_LIMIT_GAP_PCT


def calculate_limit_buy_price(market_price: float, buffer_pct: float = 0.001) -> float:
    """
    Calculate limit price for buy order.
    Slightly above market to ensure fill, but not market order.
    """
    return market_price * (1 + buffer_pct)  # 0.1% above market
