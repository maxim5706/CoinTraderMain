"""Test helpers for the new DI architecture."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional, Tuple

from core.mode_configs import TradingMode, PaperModeConfig, LiveModeConfig
from core.models import Position, PositionState, Side, TradeResult
from core.trading_interfaces import (
    IExecutor, 
    IPortfolioManager, 
    IPositionPersistence, 
    IStopOrderManager
)


class MockPaperExecutor(IExecutor):
    """Mock paper executor for testing."""
    
    def __init__(self, balance: float = 1000.0):
        self.balance = balance
        
    def can_execute_order(self, size_usd: float, symbol: str | None = None) -> Tuple[bool, str]:
        if size_usd > self.balance:
            return False, f"Insufficient balance: ${self.balance:.2f} < ${size_usd:.2f}"
        return True, "OK"
    
    async def open_position(
        self, 
        symbol: str, 
        size_usd: float, 
        price: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float
    ) -> Optional[Position]:
        self.balance -= size_usd
        qty = size_usd / price
        
        return Position(
            symbol=symbol,
            side=Side.BUY,
            entry_price=price,
            entry_time=datetime.now(timezone.utc),
            size_usd=size_usd,
            size_qty=qty,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            state=PositionState.OPEN,
            strategy_id="test",
        )
    
    async def close_position(self, position: Position, price: float, reason: str) -> TradeResult:
        pnl = (price - position.entry_price) * position.size_qty
        pnl_pct = (pnl / position.size_usd) * 100
        self.balance += position.size_usd + pnl
        
        return TradeResult(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=price,
            entry_time=position.entry_time,
            exit_time=datetime.now(timezone.utc),
            size_usd=position.size_usd,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason
        )


class MockLiveExecutor(IExecutor):
    """Mock live executor for testing."""
    
    def __init__(self, balance: float = 1000.0):
        self.balance = balance
        
    def can_execute_order(self, size_usd: float, symbol: str | None = None) -> Tuple[bool, str]:
        if size_usd > self.balance:
            return False, f"Insufficient balance: ${self.balance:.2f} < ${size_usd:.2f}"
        return True, "OK"
    
    async def open_position(
        self, 
        symbol: str, 
        size_usd: float, 
        price: float,
        stop_price: float,
        tp1_price: float,
        tp2_price: float
    ) -> Optional[Position]:
        self.balance -= size_usd
        qty = size_usd / price
        
        return Position(
            symbol=symbol,
            side=Side.BUY,
            entry_price=price,
            entry_time=datetime.now(timezone.utc),
            size_usd=size_usd,
            size_qty=qty,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            state=PositionState.OPEN,
            strategy_id="live",
        )
    
    async def close_position(self, position: Position, price: float, reason: str) -> TradeResult:
        pnl = (price - position.entry_price) * position.size_qty
        pnl_pct = (pnl / position.size_usd) * 100
        self.balance += position.size_usd + pnl
        
        return TradeResult(
            symbol=position.symbol,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=price,
            entry_time=position.entry_time,
            exit_time=datetime.now(timezone.utc),
            size_usd=position.size_usd,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason
        )


class MockPortfolioManager(IPortfolioManager):
    """Mock portfolio manager for testing."""
    
    def __init__(self, balance: float = 1000.0):
        self.balance = balance
        
    def get_available_balance(self) -> float:
        return self.balance
        
    def get_total_portfolio_value(self) -> float:
        return self.balance
        
    def update_portfolio_state(self) -> None:
        pass


class MockPositionPersistence(IPositionPersistence):
    """Mock persistence for testing."""
    
    def __init__(self):
        self.positions = {}
        
    def save_positions(self, positions: dict[str, Position]) -> None:
        self.positions = positions.copy()
        
    def load_positions(self) -> dict[str, Position]:
        return self.positions.copy()
        
    def clear_position(self, symbol: str) -> None:
        self.positions.pop(symbol, None)


class MockStopOrderManager(IStopOrderManager):
    """Mock stop order manager for testing."""
    
    def __init__(self):
        self.stops = {}
        
    def place_stop_order(self, symbol: str, qty: float, stop_price: float) -> Optional[str]:
        order_id = f"stop_{symbol}_{int(datetime.now().timestamp())}"
        self.stops[symbol] = {
            'order_id': order_id,
            'qty': qty,
            'stop_price': stop_price
        }
        return order_id
        
    def update_stop_price(self, symbol: str, new_stop_price: float) -> bool:
        if symbol in self.stops:
            self.stops[symbol]['stop_price'] = new_stop_price
            return True
        return False
        
    def cancel_stop_order(self, symbol: str) -> bool:
        return self.stops.pop(symbol, None) is not None


def create_mock_trading_config(mode: TradingMode, **overrides) -> dict:
    """Create mock config for testing."""
    base_config = {
        'max_trade_usd': 50.0,
        'daily_max_loss_usd': 100.0,
        'max_positions': 10,
        'fixed_stop_pct': 0.025,  # 2.5% for better R:R ratio
        'tp1_pct': 0.04,
        'tp2_pct': 0.07,
        'min_rr_ratio': 1.5,  # Default R:R requirement
    }
    base_config.update(overrides)
    
    if mode == TradingMode.PAPER:
        return PaperModeConfig(
            **base_config,
            paper_start_balance=1000.0,
            enable_slippage=False,  # Disable for predictable tests
            slippage_bps=0.0
        )
    else:
        return LiveModeConfig(
            **base_config,
            api_key="test_key",
            api_secret="test_secret",
            use_limit_orders=False,
            limit_buffer_pct=0.0
        )


def create_test_router_with_mocks(
    mode: TradingMode = TradingMode.PAPER,
    balance: float = 1000.0,
    **config_overrides
):
    """Create OrderRouter with mock dependencies for testing."""
    from execution.order_router import OrderRouter
    
    # Create mock components
    if mode == TradingMode.PAPER:
        executor = MockPaperExecutor(balance)
    else:
        executor = MockLiveExecutor(balance)
        
    portfolio = MockPortfolioManager(balance)
    persistence = MockPositionPersistence()
    stop_manager = MockStopOrderManager()
    config = create_mock_trading_config(mode, **config_overrides)
    
    # Create router with mocks
    router = OrderRouter(
        get_price_func=lambda symbol: 100.0,  # Fixed price for predictable tests
        state=None,
        mode=mode,
        executor=executor,
        portfolio=portfolio,
        persistence=persistence,
        stop_manager=stop_manager,
        config=config
    )
    
    # Clear any persisted cooldowns for clean test state
    router._order_cooldown.clear()
    
    return router
