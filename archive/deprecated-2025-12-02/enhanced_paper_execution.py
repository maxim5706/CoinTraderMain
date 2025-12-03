"""Enhanced paper execution with realistic slippage and execution modeling."""

import random
from datetime import datetime, timezone
from typing import Optional

from core.models import Position, PositionState, Side
from core.config import settings


class EnhancedPaperExecution:
    """
    More realistic paper execution that models:
    - Slippage on market orders
    - Partial fills on limit orders  
    - Stop loss slippage and gaps
    - Order failure scenarios
    """

    def __init__(self):
        # Slippage parameters based on spread conditions
        self.market_slippage_bps = 2.0  # Average 2bps slippage on market orders
        self.stop_slippage_bps = 8.0    # Average 8bps slippage on stops
        self.partial_fill_rate = 0.05   # 5% chance of partial fill
        self.order_failure_rate = 0.02  # 2% chance of order failure

    def open_buy(
        self,
        symbol: str,
        price: float,
        size_usd: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
        time_stop_min: int,
        strategy_id: str = "",
        spread_bps: float = 10.0,
    ) -> Optional[Position]:
        """
        Simulate realistic buy order execution.
        
        Args:
            spread_bps: Current spread for slippage modeling
        """
        
        # Simulate order failure (network issues, insufficient balance, etc.)
        if random.random() < self.order_failure_rate:
            return None
        
        # Calculate realistic fill price with slippage
        # Tighter spreads = less slippage, wider spreads = more slippage
        slippage_multiplier = max(0.5, spread_bps / 20.0)  # Scale with spread
        slippage_bps = self.market_slippage_bps * slippage_multiplier
        slippage = random.uniform(0, slippage_bps / 10000)  # 0 to max slippage
        
        fill_price = price * (1 + slippage)  # Unfavorable slippage
        
        # Simulate partial fills (especially on limit orders)
        fill_ratio = 1.0
        if settings.use_limit_orders and random.random() < self.partial_fill_rate:
            fill_ratio = random.uniform(0.7, 0.95)  # 70-95% fill
        
        actual_size_usd = size_usd * fill_ratio
        qty = actual_size_usd / fill_price
        
        # Adjust stop price for realistic execution
        # Stops often slip more than entry orders
        realistic_stop = stop_price * (1 - self.stop_slippage_bps / 10000)
        
        return Position(
            symbol=symbol,
            side=Side.BUY,
            entry_price=fill_price,  # With slippage
            entry_time=datetime.now(timezone.utc),
            size_usd=actual_size_usd,  # Partial fill possible
            size_qty=qty,
            stop_price=realistic_stop,  # With stop slippage
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            time_stop_min=time_stop_min,
            state=PositionState.OPEN,
            strategy_id=strategy_id,
        )
    
    def should_stop_gap(self, current_price: float, stop_price: float) -> bool:
        """
        Model stop loss gaps - sometimes price gaps past the stop.
        Returns True if stop should execute, False if gapped.
        """
        if current_price >= stop_price:
            return False  # Not at stop yet
        
        # Calculate gap size
        gap_pct = abs(current_price - stop_price) / stop_price
        
        # Larger gaps have higher chance of skipping stop
        # Small gaps (< 0.5%) usually still hit stop
        # Large gaps (> 2%) often skip the stop completely
        skip_probability = min(0.8, gap_pct * 40)  # Max 80% skip chance
        
        return random.random() > skip_probability
    
    def get_realistic_stop_fill(
        self, 
        stop_price: float, 
        current_price: float
    ) -> float:
        """Get realistic stop fill price (usually worse than stop price)."""
        if current_price >= stop_price:
            return stop_price  # Normal stop execution
        
        # Price gapped down - fill somewhere between stop and current
        gap_fill_ratio = random.uniform(0.3, 0.8)  # Fill 30-80% through the gap
        return stop_price - (stop_price - current_price) * gap_fill_ratio
