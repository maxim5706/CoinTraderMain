"""Paper executor implementation with simple slippage simulation."""

import random
from datetime import datetime, timezone
from typing import Optional, Tuple

from core.mode_configs import PaperModeConfig
from core.models import Position, PositionState, Side, TradeResult
from core.trading_interfaces import IExecutor


class PaperExecutor(IExecutor):
    """Executes simulated orders without touching the exchange."""

    def __init__(self, config: PaperModeConfig, portfolio=None):
        self.config = config
        self.portfolio = portfolio
        self.balance = getattr(portfolio, "balance", getattr(config, "paper_start_balance", 1000.0))
        self.enable_slippage = getattr(config, "enable_slippage", True)
        self.slippage_bps = getattr(config, "slippage_bps", 2.0)

    def _debit(self, amount: float) -> None:
        self.balance -= amount
        if self.portfolio and hasattr(self.portfolio, "debit"):
            self.portfolio.debit(amount)

    def _credit(self, amount: float) -> None:
        self.balance += amount
        if self.portfolio and hasattr(self.portfolio, "credit"):
            self.portfolio.credit(amount)

    def can_execute_order(self, size_usd: float, symbol: str | None = None) -> Tuple[bool, str]:
        if size_usd > self.balance:
            return False, f"Insufficient balance: ${self.balance:.2f} < ${size_usd:.2f}"
        if size_usd > self.config.max_trade_usd:
            return False, f"Trade size too large: ${size_usd:.2f} > ${self.config.max_trade_usd:.2f}"
        return True, "OK"

    def update_config(self, config: PaperModeConfig) -> None:
        """Update runtime config without resetting balances."""
        self.config = config
        self.enable_slippage = getattr(config, "enable_slippage", True)
        self.slippage_bps = getattr(config, "slippage_bps", 2.0)

    async def open_position(
        self,
        symbol: str,
        size_usd: float,
        price: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
    ) -> Optional[Position]:
        # Simulate slippage
        fill_price = price
        if self.enable_slippage:
            slippage = random.uniform(0, self.slippage_bps / 10000)
            fill_price = price * (1 + slippage)

        qty = size_usd / fill_price if fill_price else 0.0
        self._debit(size_usd)

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
        fill_price = price
        if self.enable_slippage:
            exit_slippage = random.uniform(0, self.slippage_bps * 1.5 / 10000)
            fill_price = price * (1 - exit_slippage)

        gross_pnl = (fill_price - position.entry_price) * position.size_qty
        entry_fee = position.entry_price * position.size_qty * 0.006
        exit_fee = fill_price * position.size_qty * 0.012
        net_pnl = gross_pnl - entry_fee - exit_fee
        pnl_pct = (net_pnl / position.size_usd) * 100 if position.size_usd else 0.0

        self._credit(position.size_usd + net_pnl)
        if self.portfolio and hasattr(self.portfolio, "record_realized_pnl"):
            self.portfolio.record_realized_pnl(net_pnl)

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
            exit_reason=reason,
            strategy_id=getattr(position, 'strategy_id', '') or '',
        )
