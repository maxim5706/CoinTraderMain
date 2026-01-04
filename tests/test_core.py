"""
Core module tests - verify imports and basic functionality.
"""

import pytest
from datetime import datetime, timezone


class TestImports:
    """Verify all key modules import without error."""
    
    def test_core_config(self):
        from core.config import settings
        assert settings is not None
        assert hasattr(settings, 'trading_mode')
    
    def test_core_models(self):
        from core.models import Position, Signal, Side, PositionState
        assert Position is not None
        assert Signal is not None
        assert Side.BUY is not None
        assert PositionState.OPEN is not None
    
    def test_core_persistence(self):
        from core.persistence import save_positions, load_positions
        assert callable(save_positions)
        assert callable(load_positions)
    
    def test_logic_strategies(self):
        from logic.strategies import StrategyOrchestrator
        orchestrator = StrategyOrchestrator()
        assert len(orchestrator.strategies) > 0
    
    def test_execution_order_router(self):
        from execution.order_router import OrderRouter
        assert OrderRouter is not None
    
    def test_ui_imports(self):
        from ui import web_app
        assert web_app is not None


class TestModels:
    """Test core data models."""
    
    def test_position_creation(self):
        from core.models import Position, Side, PositionState
        
        pos = Position(
            symbol="BTC-USD",
            side=Side.BUY,
            entry_price=50000.0,
            entry_time=datetime.now(timezone.utc),
            size_usd=100.0,
            size_qty=0.002,
            stop_price=48000.0,
            tp1_price=52000.0,
            tp2_price=55000.0,
            state=PositionState.OPEN,
            strategy_id="test",
        )
        
        assert pos.symbol == "BTC-USD"
        assert pos.entry_price == 50000.0
        assert pos.state == PositionState.OPEN
    
    def test_position_stop_check(self):
        from core.models import Position, Side, PositionState
        
        pos = Position(
            symbol="BTC-USD",
            side=Side.BUY,
            entry_price=50000.0,
            entry_time=datetime.now(timezone.utc),
            size_usd=100.0,
            size_qty=0.002,
            stop_price=48000.0,
            tp1_price=52000.0,
            tp2_price=55000.0,
            state=PositionState.OPEN,
            strategy_id="test",
        )
        
        # Price above stop - should not trigger
        assert pos.should_stop(49000.0) == False
        # Price below stop - should trigger
        assert pos.should_stop(47000.0) == True
    
    def test_position_tp_check(self):
        from core.models import Position, Side, PositionState
        
        pos = Position(
            symbol="BTC-USD",
            side=Side.BUY,
            entry_price=50000.0,
            entry_time=datetime.now(timezone.utc),
            size_usd=100.0,
            size_qty=0.002,
            stop_price=48000.0,
            tp1_price=52000.0,
            tp2_price=55000.0,
            state=PositionState.OPEN,
            strategy_id="test",
        )
        
        # Below TP1 - should not trigger
        assert pos.should_tp1(51000.0) == False
        # Above TP1 - should trigger
        assert pos.should_tp1(53000.0) == True


class TestStrategies:
    """Test strategy orchestrator."""
    
    def test_orchestrator_init(self):
        from logic.strategies import StrategyOrchestrator
        
        orchestrator = StrategyOrchestrator()
        assert len(orchestrator.strategies) >= 5
    
    def test_orchestrator_strategy_ids(self):
        from logic.strategies import StrategyOrchestrator
        
        orchestrator = StrategyOrchestrator()
        strategy_ids = [s.strategy_id for s in orchestrator.strategies]
        
        # Check some expected strategies exist
        assert "burst_flag" in strategy_ids
        assert "vwap_reclaim" in strategy_ids


class TestConfig:
    """Test configuration loading."""
    
    def test_settings_defaults(self):
        from core.config import settings
        
        # Check key settings exist
        assert hasattr(settings, 'max_positions')
        assert hasattr(settings, 'fixed_stop_pct')
        assert hasattr(settings, 'tp1_pct')
        assert hasattr(settings, 'tp2_pct')
    
    def test_settings_values_reasonable(self):
        from core.config import settings
        
        # Stop should be between 0 and 1 (percentage as decimal)
        assert 0 < settings.fixed_stop_pct < 1
        # TP should be positive
        assert settings.tp1_pct > 0
        assert settings.tp2_pct > settings.tp1_pct
