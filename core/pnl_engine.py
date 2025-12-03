"""
Centralized PnL calculation engine - single source of truth.

This module consolidates all PnL calculations to ensure consistency
across paper trading, live trading, and portfolio reporting.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime, timezone

from core.models import Position, TradeResult, Side
from core.mode_configs import BaseTradingConfig


@dataclass
class PnLBreakdown:
    """Complete PnL breakdown for a trade or position."""
    gross_pnl: float
    entry_fee: float
    exit_fee: float
    total_fees: float
    net_pnl: float
    pnl_pct: float
    fee_pct: float
    

@dataclass 
class AccountPnL:
    """Account-level PnL summary."""
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    portfolio_value: float
    cash_balance: float
    holdings_value: float
    

class PnLEngine:
    """
    Centralized PnL calculation engine.
    
    Handles:
    - Trade PnL with accurate fees
    - Position unrealized PnL  
    - Account-level PnL aggregation
    - Multi-strategy attribution
    """
    
    def __init__(self, config: BaseTradingConfig):
        self.config = config
        self._strategy_pnl: Dict[str, float] = {}
        
    def calculate_trade_pnl(
        self,
        entry_price: float,
        exit_price: float, 
        qty: float,
        side: Side = Side.BUY,
        realized_pnl: float = 0.0,  # From partial closes
    ) -> PnLBreakdown:
        """Calculate complete trade PnL with fees."""
        
        # Gross P&L (before fees)
        if side == Side.BUY:
            gross_pnl = (exit_price - entry_price) * qty + realized_pnl
        else:
            gross_pnl = (entry_price - exit_price) * qty + realized_pnl
            
        # Fee calculations (mode-aware)
        entry_value = entry_price * qty
        exit_value = exit_price * qty
        
        if hasattr(self.config, 'use_limit_orders') and self.config.use_limit_orders:
            entry_fee_rate = getattr(self.config, 'maker_fee_pct', 0.006)  
        else:
            entry_fee_rate = getattr(self.config, 'taker_fee_pct', 0.012)
            
        exit_fee_rate = getattr(self.config, 'taker_fee_pct', 0.012)  # Market sell
        
        entry_fee = entry_value * entry_fee_rate
        exit_fee = exit_value * exit_fee_rate
        total_fees = entry_fee + exit_fee
        
        # Net PnL
        net_pnl = gross_pnl - total_fees
        
        # Percentages
        total_fee_rate = entry_fee_rate + exit_fee_rate
        pnl_pct = ((exit_price / entry_price) - 1) * 100 - (total_fee_rate * 100)
        fee_pct = total_fee_rate * 100
        
        return PnLBreakdown(
            gross_pnl=gross_pnl,
            entry_fee=entry_fee,
            exit_fee=exit_fee, 
            total_fees=total_fees,
            net_pnl=net_pnl,
            pnl_pct=pnl_pct,
            fee_pct=fee_pct,
        )
    
    def calculate_unrealized_pnl(self, position: Position, current_price: float) -> float:
        """Calculate position unrealized PnL."""
        if position.side == Side.BUY:
            return (current_price - position.entry_price) * position.size_qty
        else:
            return (position.entry_price - current_price) * position.size_qty
    
    def calculate_account_pnl(
        self, 
        positions: Dict[str, Position],
        price_func,
        cash_balance: float = 0.0,
        exchange_snapshot = None,
    ) -> AccountPnL:
        """Calculate complete account PnL."""
        
        realized_pnl = 0.0  # From completed trades (daily stats)
        unrealized_pnl = 0.0
        holdings_value = 0.0
        
        # Calculate unrealized from positions
        for symbol, position in positions.items():
            current_price = price_func(symbol)
            if current_price > 0:
                pos_unrealized = self.calculate_unrealized_pnl(position, current_price)
                unrealized_pnl += pos_unrealized
                holdings_value += position.size_qty * current_price
        
        # Use exchange snapshot if available (live mode)
        if exchange_snapshot:
            unrealized_pnl = exchange_snapshot.total_unrealized_pnl
            holdings_value = exchange_snapshot.total_crypto
            cash_balance = exchange_snapshot.total_cash
        
        total_pnl = realized_pnl + unrealized_pnl
        portfolio_value = cash_balance + holdings_value
        
        return AccountPnL(
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=total_pnl,
            portfolio_value=portfolio_value,
            cash_balance=cash_balance,
            holdings_value=holdings_value,
        )
    
    def track_strategy_pnl(self, strategy_id: str, pnl: float):
        """Track PnL attribution by strategy."""
        if strategy_id not in self._strategy_pnl:
            self._strategy_pnl[strategy_id] = 0.0
        self._strategy_pnl[strategy_id] += pnl
    
    def get_strategy_pnl(self) -> Dict[str, float]:
        """Get PnL breakdown by strategy."""
        return dict(self._strategy_pnl)
    
    def reset_daily_stats(self):
        """Reset daily strategy PnL tracking."""
        self._strategy_pnl.clear()
    
    def get_total_pnl(self) -> float:
        """Get total PnL across all strategies."""
        return sum(self._strategy_pnl.values())
    
    def get_total_unrealized_pnl(self, positions: Dict[str, Position], price_func) -> float:
        """Get total unrealized PnL from all positions."""
        total_unrealized = 0.0
        for symbol, position in positions.items():
            current_price = price_func(symbol)
            if current_price > 0:
                total_unrealized += self.calculate_unrealized_pnl(position, current_price)
        return total_unrealized
