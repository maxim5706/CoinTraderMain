"""Trading component interfaces for paper/live implementations."""

from typing import Optional, Protocol, Tuple

from core.models import Position, TradeResult


class IExecutor(Protocol):
    """Executes orders for a given trading mode."""

    async def open_position(
        self,
        symbol: str,
        size_usd: float,
        price: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float,
    ) -> Optional[Position]:
        ...

    async def close_position(
        self,
        position: Position,
        price: float,
        reason: str,
    ) -> TradeResult:
        ...

    def can_execute_order(self, size_usd: float, symbol: str | None = None) -> Tuple[bool, str]:
        ...


class IPortfolioManager(Protocol):
    """Portfolio/balance provider."""

    def get_available_balance(self) -> float:
        ...

    def get_total_portfolio_value(self) -> float:
        ...

    def update_portfolio_state(self) -> None:
        ...


class IPositionPersistence(Protocol):
    """Position persistence abstraction."""

    def save_positions(self, positions: dict[str, Position]) -> None:
        ...

    def load_positions(self) -> dict[str, Position]:
        ...

    def clear_position(self, symbol: str) -> None:
        ...


class IStopOrderManager(Protocol):
    """Stop-order management abstraction."""

    def place_stop_order(self, symbol: str, qty: float, stop_price: float) -> Optional[str]:
        ...

    def update_stop_price(self, symbol: str, new_stop_price: float) -> bool:
        ...

    def cancel_stop_order(self, symbol: str) -> bool:
        ...
