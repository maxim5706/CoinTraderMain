"""Stop manager for paper mode (no-op)."""

from typing import Optional

from core.trading_interfaces import IStopOrderManager


class PaperStopManager(IStopOrderManager):
    """No-op stop manager for simulated mode."""

    def place_stop_order(self, symbol: str, qty: float, stop_price: float) -> Optional[str]:
        return None

    def update_stop_price(self, symbol: str, new_stop_price: float) -> bool:
        return True

    def cancel_stop_order(self, symbol: str) -> bool:
        return True
