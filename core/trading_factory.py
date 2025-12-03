"""Factory for constructing mode-specific trading components."""

from core.mode_configs import BaseTradingConfig, LiveModeConfig, PaperModeConfig, TradingMode
from core.trading_interfaces import (
    IExecutor,
    IPortfolioManager,
    IPositionPersistence,
    IStopOrderManager,
)


class TradingFactory:
    """Creates mode-specific implementations for trading components."""

    @staticmethod
    def create_executor(
        mode: TradingMode,
        config: BaseTradingConfig,
        portfolio=None,
        stop_manager=None,
    ) -> IExecutor:
        if mode == TradingMode.PAPER:
            from execution.paper_executor import PaperExecutor

            return PaperExecutor(config, portfolio=portfolio)  # type: ignore[return-value]
        from execution.live_executor import LiveExecutor

        return LiveExecutor(config, portfolio=portfolio, stop_manager=stop_manager)  # type: ignore[return-value]

    @staticmethod
    def create_portfolio_manager(mode: TradingMode, config: BaseTradingConfig) -> IPortfolioManager:
        if mode == TradingMode.PAPER:
            from core.paper_portfolio import PaperPortfolioManager

            return PaperPortfolioManager(config.paper_start_balance)  # type: ignore[return-value]
        from core.live_portfolio import LivePortfolioManager

        return LivePortfolioManager(config)  # type: ignore[return-value]

    @staticmethod
    def create_persistence(mode: TradingMode) -> IPositionPersistence:
        if mode == TradingMode.PAPER:
            from core.paper_persistence import PaperPositionPersistence

            return PaperPositionPersistence()
        from core.live_persistence import LivePositionPersistence

        return LivePositionPersistence()

    @staticmethod
    def create_stop_manager(mode: TradingMode, config: BaseTradingConfig) -> IStopOrderManager:
        if mode == TradingMode.PAPER:
            from execution.paper_stops import PaperStopManager

            return PaperStopManager()
        from execution.live_stops import LiveStopManager

        return LiveStopManager(config)  # type: ignore[return-value]
