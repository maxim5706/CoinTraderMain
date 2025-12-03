"""Clean paper executor implementation."""

import random
from typing import Optional
from datetime import datetime, timezone

from core.models import Position, PositionState, Side, TradeResult
from core.trading_interfaces import IExecutor
from core.mode_configs import BaseTradingConfig


class PaperExecutor:
    """Clean paper execution with realistic simulation."""
    
    def __init__(self, config: BaseTradingConfig):
        self.config = config
        self.balance = getattr(config, 'paper_start_balance', 1000.0)
        self.enable_slippage = getattr(config, 'enable_slippage', True)
        self.slippage_bps = getattr(config, 'slippage_bps', 2.0)
    
    async def open_position(self, symbol: str, size_usd: float, price: float) -> Optional[Position]:
        """Open paper position with realistic slippage."""
        
        # Simulate realistic fill with slippage
        if self.enable_slippage:
            slippage = random.uniform(0, self.slippage_bps / 10000)
            fill_price = price * (1 + slippage)  # Unfavorable slippage
        else:
            fill_price = price
        
        # Simulate order failure (1% chance)
        if random.random() < 0.01:
            return None
        
        qty = size_usd / fill_price
        
        # Calculate stops with realistic slippage
        stop_price = fill_price * (1 - self.config.fixed_stop_pct)
        tp1_price = fill_price * (1 + self.config.tp1_pct)
        tp2_price = fill_price * (1 + self.config.tp2_pct)
        
        # Deduct from balance
        self.balance -= size_usd
        
        return Position(
            symbol=symbol,
            side=Side.BUY,
            entry_price=fill_price,
            entry_time=datetime.now(timezone.utc),
            size_usd=size_usd,
            size_qty=qty,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            state=PositionState.OPEN,
            strategy_id="paper",
        )
    
    async def close_position(self, position: Position, price: float, reason: str) -> TradeResult:
        """Close paper position with realistic execution."""
        
        # Simulate exit slippage (market orders)
        if self.enable_slippage:
            exit_slippage = random.uniform(0, self.slippage_bps * 1.5 / 10000)  # Higher on exits
            fill_price = price * (1 - exit_slippage)  # Unfavorable for sells
        else:
            fill_price = price
        
        # Calculate PnL
        gross_pnl = (fill_price - position.entry_price) * position.size_qty
        
        # Apply fees (paper should match live fee structure)
        entry_fee = position.entry_price * position.size_qty * 0.006  # Maker fee
        exit_fee = fill_price * position.size_qty * 0.012  # Taker fee  
        net_pnl = gross_pnl - entry_fee - exit_fee
        pnl_pct = (net_pnl / position.size_usd) * 100
        
        # Add back to balance
        self.balance += position.size_usd + net_pnl
        
        return TradeResult(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=fill_price,
            entry_time=position.entry_time,
            exit_time=datetime.now(timezone.utc),
            size_usd=position.size_usd,
            pnl=net_pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason
        )
    
    def can_execute_order(self, size_usd: float) -> tuple[bool, str]:
        """Check if paper order can be executed."""
        if size_usd > self.balance:
            return False, f"Insufficient balance: ${self.balance:.2f} < ${size_usd:.2f}"
        
        if size_usd > self.config.max_trade_usd:
            return False, f"Trade size too large: ${size_usd:.2f} > ${self.config.max_trade_usd:.2f}"
        
        return True, "OK"
