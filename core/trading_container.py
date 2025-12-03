"""Simple dependency injection container for trading components."""

from core.mode_configs import BaseTradingConfig, TradingMode
from core.trading_factory import TradingFactory
from core.trading_interfaces import (
    IExecutor,
    IPortfolioManager,
    IPositionPersistence,
    IStopOrderManager,
)


class TradingContainer:
    """Provides mode-specific component instances."""

    def __init__(self, mode: TradingMode, config: BaseTradingConfig):
        self.mode = mode
        self.config = config
        self._instances: dict[str, object] = {}

    def get_executor(self) -> IExecutor:
        if "executor" not in self._instances:
            portfolio = self.get_portfolio_manager()
            stops = self.get_stop_manager()
            self._instances["executor"] = TradingFactory.create_executor(
                self.mode,
                self.config,
                portfolio=portfolio,
                stop_manager=stops,
            )
        return self._instances["executor"]  # type: ignore[return-value]

    def get_portfolio_manager(self) -> IPortfolioManager:
        if "portfolio" not in self._instances:
            self._instances["portfolio"] = TradingFactory.create_portfolio_manager(self.mode, self.config)
        return self._instances["portfolio"]  # type: ignore[return-value]

    def get_persistence(self) -> IPositionPersistence:
        if "persistence" not in self._instances:
            self._instances["persistence"] = TradingFactory.create_persistence(self.mode)
        return self._instances["persistence"]  # type: ignore[return-value]

    def get_stop_manager(self) -> IStopOrderManager:
        if "stops" not in self._instances:
            self._instances["stops"] = TradingFactory.create_stop_manager(self.mode, self.config)
        return self._instances["stops"]  # type: ignore[return-value]
