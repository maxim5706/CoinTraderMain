"""Live stop manager that delegates to the shared order_manager."""

from typing import Optional

from core.mode_configs import LiveModeConfig
from core.trading_interfaces import IStopOrderManager
from execution.order_manager import order_manager


class LiveStopManager(IStopOrderManager):
    """Uses exchange stop orders via the shared order manager."""

    def __init__(self, config: LiveModeConfig, client=None):
        self.config = config
        self._client = client
        if client:
            order_manager.init_client(client)

    def bind_client(self, client) -> None:
        self._client = client
        order_manager.init_client(client)

    def place_stop_order(self, symbol: str, qty: float, stop_price: float) -> Optional[str]:
        if self._client is None:
            return None
        return order_manager.place_stop_order(symbol=symbol, qty=qty, stop_price=stop_price)

    def update_stop_price(self, symbol: str, new_stop_price: float) -> bool:
        if self._client is None:
            return False
        return bool(order_manager.update_stop_price(symbol, new_stop_price))

    def cancel_stop_order(self, symbol: str) -> bool:
        if self._client is None:
            return False
        return bool(order_manager.cancel_stop_order(symbol))
