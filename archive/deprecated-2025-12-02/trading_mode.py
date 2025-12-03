"""Clean trading mode architecture with proper separation of concerns."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Protocol, Optional
from dataclasses import dataclass

from core.models import Position, TradeResult


class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


# === ABSTRACT INTERFACES ===

class IExecutor(Protocol):
    """Abstract interface for order execution."""
    
    async def open_position(self, symbol: str, size_usd: float, price: float) -> Optional[Position]:
        """Open a position (mode-specific implementation)."""
        ...
    
    async def close_position(self, position: Position, price: float, reason: str) -> TradeResult:
        """Close a position (mode-specific implementation)."""
        ...
    
    def can_execute_order(self, size_usd: float) -> tuple[bool, str]:
        """Check if order can be executed (balance, limits, etc.)."""
        ...


class IPortfolioManager(Protocol):
    """Abstract interface for portfolio/balance management."""
    
    def get_available_balance(self) -> float:
        """Get available trading balance."""
        ...
    
    def get_total_portfolio_value(self) -> float:
        """Get total portfolio value."""
        ...
    
    def update_portfolio_state(self) -> None:
        """Refresh portfolio state from source."""
        ...


class IPositionPersistence(Protocol):
    """Abstract interface for position persistence."""
    
    def save_positions(self, positions: dict[str, Position]) -> None:
        """Save positions to storage."""
        ...
    
    def load_positions(self) -> dict[str, Position]:
        """Load positions from storage."""
        ...
    
    def clear_position(self, symbol: str) -> None:
        """Clear a specific position."""
        ...


class IStopOrderManager(Protocol):
    """Abstract interface for stop order management."""
    
    def place_stop_order(self, symbol: str, qty: float, stop_price: float) -> Optional[str]:
        """Place a stop order (returns order ID if successful)."""
        ...
    
    def update_stop_price(self, symbol: str, new_stop_price: float) -> bool:
        """Update existing stop order price."""
        ...
    
    def cancel_stop_order(self, symbol: str) -> bool:
        """Cancel stop order for symbol."""
        ...


class IDashboardRenderer(Protocol):
    """Abstract interface for dashboard rendering."""
    
    def render_balance_section(self) -> str:
        """Render mode-specific balance display."""
        ...
    
    def render_positions_panel(self) -> str:
        """Render mode-specific positions panel."""
        ...
    
    def get_mode_indicator(self) -> str:
        """Get mode indicator for display."""
        ...


# === CONFIGURATION CLASSES ===

@dataclass
class BaseTradingConfig:
    """Shared configuration for all modes."""
    max_trade_usd: float
    daily_max_loss_usd: float
    max_positions: int
    fixed_stop_pct: float
    tp1_pct: float
    tp2_pct: float


@dataclass  
class PaperModeConfig(BaseTradingConfig):
    """Paper mode specific configuration."""
    paper_start_balance: float = 1000.0
    enable_slippage_simulation: bool = False
    slippage_bps: float = 2.0
    
    
@dataclass
class LiveModeConfig(BaseTradingConfig):
    """Live mode specific configuration."""
    api_key: str
    api_secret: str
    use_limit_orders: bool = True
    limit_buffer_pct: float = 0.003


# === FACTORY PATTERN ===

class TradingModeFactory:
    """Factory for creating mode-specific components."""
    
    @staticmethod
    def create_executor(mode: TradingMode, config: BaseTradingConfig) -> IExecutor:
        """Create appropriate executor for mode."""
        if mode == TradingMode.PAPER:
            from execution.paper_executor import PaperExecutor
            return PaperExecutor(config)
        else:
            from execution.live_executor import LiveExecutor
            return LiveExecutor(config)
    
    @staticmethod
    def create_portfolio_manager(mode: TradingMode, config: BaseTradingConfig) -> IPortfolioManager:
        """Create appropriate portfolio manager for mode."""
        if mode == TradingMode.PAPER:
            from core.paper_portfolio import PaperPortfolioManager
            return PaperPortfolioManager(config.paper_start_balance)
        else:
            from core.live_portfolio import LivePortfolioManager
            return LivePortfolioManager(config.api_key, config.api_secret)
    
    @staticmethod
    def create_persistence(mode: TradingMode) -> IPositionPersistence:
        """Create appropriate persistence layer for mode."""
        if mode == TradingMode.PAPER:
            from core.paper_persistence import PaperPositionPersistence
            return PaperPositionPersistence()
        else:
            from core.live_persistence import LivePositionPersistence
            return LivePositionPersistence()
    
    @staticmethod
    def create_stop_manager(mode: TradingMode, config: BaseTradingConfig) -> IStopOrderManager:
        """Create appropriate stop order manager for mode."""
        if mode == TradingMode.PAPER:
            from execution.paper_stops import PaperStopManager
            return PaperStopManager()
        else:
            from execution.live_stops import LiveStopManager
            return LiveStopManager(config.api_key, config.api_secret)
    
    @staticmethod
    def create_dashboard_renderer(mode: TradingMode) -> IDashboardRenderer:
        """Create appropriate dashboard renderer for mode."""
        if mode == TradingMode.PAPER:
            from apps.paper_dashboard import PaperDashboardRenderer
            return PaperDashboardRenderer()
        else:
            from apps.live_dashboard import LiveDashboardRenderer
            return LiveDashboardRenderer()


# === DEPENDENCY INJECTION CONTAINER ===

class TradingContainer:
    """Dependency injection container for trading components."""
    
    def __init__(self, mode: TradingMode, config: BaseTradingConfig):
        self.mode = mode
        self.config = config
        self._instances = {}
    
    def get_executor(self) -> IExecutor:
        """Get or create executor instance."""
        if 'executor' not in self._instances:
            self._instances['executor'] = TradingModeFactory.create_executor(
                self.mode, self.config
            )
        return self._instances['executor']
    
    def get_portfolio_manager(self) -> IPortfolioManager:
        """Get or create portfolio manager instance.""" 
        if 'portfolio' not in self._instances:
            self._instances['portfolio'] = TradingModeFactory.create_portfolio_manager(
                self.mode, self.config
            )
        return self._instances['portfolio']
    
    def get_persistence(self) -> IPositionPersistence:
        """Get or create persistence instance."""
        if 'persistence' not in self._instances:
            self._instances['persistence'] = TradingModeFactory.create_persistence(self.mode)
        return self._instances['persistence']
    
    def get_stop_manager(self) -> IStopOrderManager:
        """Get or create stop manager instance."""
        if 'stops' not in self._instances:
            self._instances['stops'] = TradingModeFactory.create_stop_manager(
                self.mode, self.config
            )
        return self._instances['stops']
    
    def get_dashboard_renderer(self) -> IDashboardRenderer:
        """Get or create dashboard renderer instance."""
        if 'dashboard' not in self._instances:
            self._instances['dashboard'] = TradingModeFactory.create_dashboard_renderer(self.mode)
        return self._instances['dashboard']


# === MODE-AGNOSTIC COMPONENTS ===

class CleanOrderRouter:
    """Clean order router with injected dependencies - no mode checks!"""
    
    def __init__(
        self,
        executor: IExecutor,
        portfolio: IPortfolioManager,
        persistence: IPositionPersistence,
        stop_manager: IStopOrderManager
    ):
        self.executor = executor
        self.portfolio = portfolio
        self.persistence = persistence
        self.stop_manager = stop_manager
        self.positions: dict[str, Position] = {}
    
    async def open_position(self, symbol: str, size_usd: float, price: float) -> Optional[Position]:
        """Open position using injected executor - no mode checks needed!"""
        
        # Shared validation logic (same for all modes)
        can_execute, reason = self.executor.can_execute_order(size_usd)
        if not can_execute:
            print(f"Cannot execute order: {reason}")
            return None
        
        # Execute using appropriate implementation
        position = await self.executor.open_position(symbol, size_usd, price)
        
        if position:
            self.positions[symbol] = position
            self.persistence.save_positions(self.positions)
            
            # Place stop order using appropriate manager
            self.stop_manager.place_stop_order(
                symbol, position.size_qty, position.stop_price
            )
        
        return position
    
    async def close_position(self, symbol: str, price: float, reason: str) -> Optional[TradeResult]:
        """Close position using injected executor."""
        position = self.positions.get(symbol)
        if not position:
            return None
        
        # Cancel stop order
        self.stop_manager.cancel_stop_order(symbol)
        
        # Execute close
        result = await self.executor.close_position(position, price, reason)
        
        # Clean up
        del self.positions[symbol]
        self.persistence.clear_position(symbol)
        
        return result
