#!/usr/bin/env python3
"""
Comprehensive System Test Suite

Tests all critical components for live trading:
- Configuration
- Exchange connectivity
- Position management
- PnL calculations
- Order execution
- Dashboard rendering
- Integration accuracy

Run with: pytest tests/test_full_system.py -v
"""

import pytest
import sys
import os
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import settings
from core.models import Position, Side, Signal, SignalType
from core.pnl_engine import PnLEngine, PnLBreakdown, AccountPnL
from core.position_registry import PositionRegistry, PositionLimits
from core.mode_configs import TradingMode, PaperModeConfig, LiveModeConfig, BaseTradingConfig


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def mock_price_func():
    """Mock price function for testing."""
    def _get_price(symbol: str) -> float:
        prices = {
            "BTC-USD": 95000.0,
            "ETH-USD": 3500.0,
            "SOL-USD": 238.0,
            "DOGE-USD": 0.40,
            "ORCA-USD": 1.35,
            "IMX-USD": 0.31,
        }
        return prices.get(symbol, 100.0)
    return _get_price


@pytest.fixture
def sample_position():
    """Create a sample position for testing."""
    return Position(
        symbol="BTC-USD",
        side=Side.BUY,
        entry_price=94000.0,
        entry_time=datetime.now(timezone.utc),
        size_usd=100.0,
        size_qty=0.001064,
        stop_price=93000.0,
        tp1_price=96000.0,
        tp2_price=98000.0,
        strategy_id="burst_flag"
    )


@pytest.fixture
def paper_config():
    """Paper trading configuration."""
    return PaperModeConfig()


@pytest.fixture
def live_config():
    """Live trading configuration."""
    return LiveModeConfig()


@pytest.fixture
def pnl_engine(paper_config):
    """PnL engine instance."""
    return PnLEngine(paper_config)


@pytest.fixture
def position_registry():
    """Position registry instance."""
    limits = PositionLimits(max_positions=8)
    return PositionRegistry(limits)


# ============================================================
# CONFIGURATION TESTS
# ============================================================

class TestConfiguration:
    """Test configuration settings."""
    
    def test_settings_loaded(self):
        """Verify settings are loaded."""
        assert settings is not None
        
    def test_trading_mode_valid(self):
        """Verify trading mode is valid."""
        assert settings.trading_mode in ["paper", "live"]
        
    def test_max_trade_usd_positive(self):
        """Verify max trade size is positive."""
        assert settings.max_trade_usd > 0
        
    def test_daily_max_loss_set(self):
        """Verify daily max loss is set."""
        assert settings.daily_max_loss_usd > 0
        
    def test_portfolio_exposure_valid(self):
        """Verify portfolio exposure is valid percentage."""
        assert 0 < settings.portfolio_max_exposure_pct <= 1.0
        
    def test_entry_score_min_reasonable(self):
        """Verify entry score minimum is reasonable."""
        assert 0 <= settings.entry_score_min <= 100


# ============================================================
# PNL ENGINE TESTS
# ============================================================

class TestPnLEngine:
    """Test PnL calculation engine."""
    
    def test_pnl_engine_init(self, pnl_engine):
        """Test PnL engine initialization."""
        assert pnl_engine is not None
        assert pnl_engine.config is not None
        
    def test_calculate_trade_pnl_profit(self, pnl_engine):
        """Test profitable trade PnL calculation."""
        result = pnl_engine.calculate_trade_pnl(
            entry_price=100.0,
            exit_price=110.0,
            qty=1.0,
            side=Side.BUY
        )
        
        assert isinstance(result, PnLBreakdown)
        assert result.gross_pnl == 10.0  # (110-100) * 1
        assert result.net_pnl < result.gross_pnl  # Fees reduce profit
        assert result.total_fees > 0
        
    def test_calculate_trade_pnl_loss(self, pnl_engine):
        """Test losing trade PnL calculation."""
        result = pnl_engine.calculate_trade_pnl(
            entry_price=100.0,
            exit_price=90.0,
            qty=1.0,
            side=Side.BUY
        )
        
        assert result.gross_pnl == -10.0  # (90-100) * 1
        assert result.net_pnl < result.gross_pnl  # Fees make loss worse
        
    def test_calculate_unrealized_pnl(self, pnl_engine, sample_position):
        """Test unrealized PnL calculation."""
        current_price = 95000.0  # Above entry
        unrealized = pnl_engine.calculate_unrealized_pnl(sample_position, current_price)
        
        expected = (95000.0 - 94000.0) * sample_position.size_qty
        assert abs(unrealized - expected) < 0.01
        
    def test_calculate_account_pnl(self, pnl_engine, sample_position, mock_price_func):
        """Test account-level PnL calculation."""
        positions = {"BTC-USD": sample_position}
        
        result = pnl_engine.calculate_account_pnl(
            positions=positions,
            price_func=mock_price_func,
            cash_balance=1000.0
        )
        
        assert isinstance(result, AccountPnL)
        assert result.cash_balance == 1000.0
        assert result.holdings_value > 0
        assert result.portfolio_value == result.cash_balance + result.holdings_value
        
    def test_strategy_pnl_tracking(self, pnl_engine):
        """Test strategy PnL attribution."""
        pnl_engine.track_strategy_pnl("burst_flag", 10.0)
        pnl_engine.track_strategy_pnl("burst_flag", 5.0)
        pnl_engine.track_strategy_pnl("mean_revert", -3.0)
        
        strategy_pnl = pnl_engine.get_strategy_pnl()
        
        assert strategy_pnl["burst_flag"] == 15.0
        assert strategy_pnl["mean_revert"] == -3.0
        
    def test_get_total_pnl(self, pnl_engine):
        """Test total PnL calculation."""
        pnl_engine.track_strategy_pnl("strategy1", 10.0)
        pnl_engine.track_strategy_pnl("strategy2", -3.0)
        
        total = pnl_engine.get_total_pnl()
        assert total == 7.0
        
    def test_reset_daily_stats(self, pnl_engine):
        """Test daily stats reset."""
        pnl_engine.track_strategy_pnl("test", 100.0)
        pnl_engine.reset_daily_stats()
        
        assert pnl_engine.get_total_pnl() == 0.0


# ============================================================
# POSITION REGISTRY TESTS
# ============================================================

class TestPositionRegistry:
    """Test position registry."""
    
    def test_registry_init(self, position_registry):
        """Test registry initialization."""
        assert position_registry is not None
        assert position_registry.limits.max_positions == 8
        
    def test_add_position(self, position_registry, sample_position):
        """Test adding a position."""
        position_registry.add_position(sample_position)
        
        active = position_registry.get_active_positions()
        assert len(active) == 1
        assert "BTC-USD" in active
        
    def test_remove_position(self, position_registry, sample_position):
        """Test removing a position."""
        position_registry.add_position(sample_position)
        position_registry.remove_position("BTC-USD")
        
        active = position_registry.get_active_positions()
        assert len(active) == 0 or "BTC-USD" not in active
        
    def test_can_open_position_under_limit(self, position_registry):
        """Test position opening when under limit."""
        can_open, reason = position_registry.can_open_position("test_strategy", 50.0)
        assert can_open is True
        assert reason == "OK"
        
    def test_can_open_position_at_limit(self, position_registry, sample_position):
        """Test position opening at limit."""
        # Add positions up to limit
        for i in range(8):
            pos = Position(
                symbol=f"TEST{i}-USD",
                side=Side.BUY,
                entry_price=100.0,
                entry_time=datetime.now(timezone.utc),
                size_usd=50.0,
                size_qty=0.5,
                stop_price=95.0,
                tp1_price=105.0,
                tp2_price=110.0,
                strategy_id="test"
            )
            position_registry.add_position(pos)
        
        can_open, reason = position_registry.can_open_position("test", 50.0)
        assert can_open is False
        assert "Max positions" in reason
        
    def test_get_stats(self, position_registry, sample_position, mock_price_func):
        """Test getting registry stats."""
        position_registry.add_position(sample_position)
        
        stats = position_registry.get_stats(mock_price_func)
        
        assert stats.active_positions == 1
        assert stats.total_exposure_usd > 0


# ============================================================
# DASHBOARD TESTS
# ============================================================

class TestDashboard:
    """Test dashboard rendering."""
    
    def test_dashboard_import(self):
        """Test dashboard can be imported."""
        from apps.dashboard.dashboard_v2 import DashboardV2
        assert DashboardV2 is not None
        
    def test_dashboard_init(self):
        """Test dashboard initialization."""
        from apps.dashboard.dashboard_v2 import DashboardV2
        dashboard = DashboardV2()
        assert dashboard is not None
        assert dashboard.state is not None
        
    def test_render_top_bar(self):
        """Test top bar rendering."""
        from apps.dashboard.dashboard_v2 import DashboardV2
        dashboard = DashboardV2()
        
        # Set required state
        dashboard.state.mode = "paper"
        dashboard.state.paper_balance = 1000.0
        dashboard.state.paper_positions_value = 0.0
        dashboard.state.warm_symbols = 50
        dashboard.state.cold_symbols = 10
        
        top_bar = dashboard.render_top_bar()
        assert top_bar is not None
        
    def test_render_signal_panel(self):
        """Test signal panel rendering."""
        from apps.dashboard.dashboard_v2 import DashboardV2
        dashboard = DashboardV2()
        
        panel = dashboard.render_signal_panel()
        assert panel is not None
        
    def test_render_focus_panel(self):
        """Test focus panel rendering."""
        from apps.dashboard.dashboard_v2 import DashboardV2
        dashboard = DashboardV2()
        
        panel = dashboard.render_focus_panel()
        assert panel is not None
        
    def test_render_sanity_panel(self):
        """Test sanity panel rendering."""
        from apps.dashboard.dashboard_v2 import DashboardV2
        dashboard = DashboardV2()
        
        dashboard.state.startup_time = datetime.now(timezone.utc)
        dashboard.state.warm_symbols = 50
        dashboard.state.cold_symbols = 10
        
        panel = dashboard.render_sanity_panel()
        assert panel is not None
        
    def test_render_full(self):
        """Test full dashboard rendering."""
        from apps.dashboard.dashboard_v2 import DashboardV2
        dashboard = DashboardV2()
        
        dashboard.state.mode = "paper"
        dashboard.state.paper_balance = 1000.0
        dashboard.state.paper_positions_value = 0.0
        dashboard.state.warm_symbols = 50
        dashboard.state.cold_symbols = 10
        dashboard.state.startup_time = datetime.now(timezone.utc)
        
        full = dashboard.render_full()
        assert full is not None


# ============================================================
# ORDER ROUTER TESTS
# ============================================================

class TestOrderRouter:
    """Test order router functionality."""
    
    def test_order_router_import(self):
        """Test order router can be imported."""
        from execution.order_router import OrderRouter
        assert OrderRouter is not None
        
    def test_order_router_init(self, mock_price_func):
        """Test order router initialization."""
        from execution.order_router import OrderRouter
        
        router = OrderRouter(get_price_func=mock_price_func)
        assert router is not None
        
    def test_has_pnl_engine(self, mock_price_func):
        """Test order router has PnL engine."""
        from execution.order_router import OrderRouter
        
        router = OrderRouter(get_price_func=mock_price_func)
        assert hasattr(router, 'pnl_engine')
        assert router.pnl_engine is not None
        
    def test_has_position_registry(self, mock_price_func):
        """Test order router has position registry."""
        from execution.order_router import OrderRouter
        
        router = OrderRouter(get_price_func=mock_price_func)
        assert hasattr(router, 'position_registry')
        assert router.position_registry is not None
        
    def test_has_truth_sync_methods(self, mock_price_func):
        """Test order router has truth sync methods."""
        from execution.order_router import OrderRouter
        
        router = OrderRouter(get_price_func=mock_price_func)
        
        assert hasattr(router, '_verify_exchange_truth')
        assert hasattr(router, '_recover_from_drift')
        assert hasattr(router, '_validate_before_trade')
        
    def test_validate_before_trade(self, mock_price_func):
        """Test pre-trade validation."""
        from execution.order_router import OrderRouter
        
        router = OrderRouter(get_price_func=mock_price_func)
        
        # Should return True or False (not raise)
        result = router._validate_before_trade("BTC-USD")
        assert isinstance(result, bool)


# ============================================================
# INTEGRATION TESTS
# ============================================================

class TestIntegration:
    """Test component integration."""
    
    def test_pnl_engine_with_position_registry(self, pnl_engine, position_registry, sample_position, mock_price_func):
        """Test PnL engine works with position registry."""
        position_registry.add_position(sample_position)
        
        # get_active_positions returns a dict {symbol: Position}
        positions = position_registry.get_active_positions()
        
        account_pnl = pnl_engine.calculate_account_pnl(
            positions=positions,
            price_func=mock_price_func,
            cash_balance=1000.0
        )
        
        assert account_pnl.holdings_value > 0
        
    def test_order_router_components_synced(self, mock_price_func):
        """Test order router components stay in sync."""
        from execution.order_router import OrderRouter
        
        router = OrderRouter(get_price_func=mock_price_func)
        
        # Position counts should match (including dust positions)
        router_count = len(router.positions)
        registry_count = len(router.position_registry.get_all_positions())
        
        assert router_count == registry_count


# ============================================================
# TRADING MODE TESTS
# ============================================================

class TestTradingModes:
    """Test trading mode configurations."""
    
    def test_paper_config_has_required_fields(self, paper_config):
        """Test paper config has all required fields."""
        assert hasattr(paper_config, 'maker_fee_pct')
        assert hasattr(paper_config, 'taker_fee_pct')
        assert hasattr(paper_config, 'max_positions')
        
    def test_live_config_has_required_fields(self, live_config):
        """Test live config has all required fields."""
        assert hasattr(live_config, 'use_limit_orders')
        assert hasattr(live_config, 'maker_fee_pct')
        assert hasattr(live_config, 'taker_fee_pct')
        
    def test_fee_rates_reasonable(self, paper_config, live_config):
        """Test fee rates are reasonable."""
        for config in [paper_config, live_config]:
            assert 0 <= config.maker_fee_pct <= 0.01  # Max 1%
            assert 0 <= config.taker_fee_pct <= 0.02  # Max 2%


# ============================================================
# SIGNAL TESTS
# ============================================================

class TestSignals:
    """Test signal handling."""
    
    def test_signal_creation(self):
        """Test signal can be created."""
        signal = Signal(
            symbol="BTC-USD",
            type=SignalType.FLAG_BREAKOUT,
            timestamp=datetime.now(timezone.utc),
            price=95000.0,
            strategy_id="burst_flag",
            confidence=85.0,
            stop_price=94000.0,
            tp1_price=96000.0,
            tp2_price=98000.0,
            reason="Test signal"
        )
        
        assert signal.symbol == "BTC-USD"
        assert signal.confidence == 85.0
        
    def test_signal_types_exist(self):
        """Test all signal types exist."""
        assert SignalType.FLAG_BREAKOUT is not None
        assert SignalType.IMPULSE_FOUND is not None
        assert SignalType.BURST_DETECTED is not None


# ============================================================
# RUN ALL TESTS
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
