"""Tests for multi-strategy architecture."""

import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from logic.strategies.base import StrategySignal, SignalDirection
from logic.strategies.orchestrator import StrategyOrchestrator, OrchestratorConfig


def test_strategy_signal_is_valid():
    """Test StrategySignal validation."""
    # Invalid: no direction
    sig = StrategySignal(symbol="BTC-USD", strategy_id="test")
    assert not sig.is_valid
    
    # Invalid: no price
    sig = StrategySignal(
        symbol="BTC-USD",
        strategy_id="test",
        direction=SignalDirection.LONG,
        edge_score_base=50,
    )
    assert not sig.is_valid
    
    # Valid
    sig = StrategySignal(
        symbol="BTC-USD",
        strategy_id="test",
        direction=SignalDirection.LONG,
        edge_score_base=50,
        entry_price=100.0,
        stop_price=95.0,
    )
    assert sig.is_valid


def test_orchestrator_selects_highest_score():
    """Test orchestrator selects highest edge_score_base."""
    orch = StrategyOrchestrator(OrchestratorConfig(
        enable_burst_flag=False,
        enable_vwap_reclaim=False,
        enable_mean_reversion=False,
    ))
    
    # Add mock strategies
    mock1 = MagicMock()
    mock1.strategy_id = "strategy_1"
    mock1.analyze.return_value = StrategySignal(
        symbol="ETH-USD",
        strategy_id="strategy_1",
        direction=SignalDirection.LONG,
        edge_score_base=40,
        entry_price=100.0,
        stop_price=95.0,
    )
    
    mock2 = MagicMock()
    mock2.strategy_id = "strategy_2"
    mock2.analyze.return_value = StrategySignal(
        symbol="ETH-USD",
        strategy_id="strategy_2",
        direction=SignalDirection.LONG,
        edge_score_base=70,  # Higher score
        entry_price=100.0,
        stop_price=95.0,
    )
    
    orch.strategies = [mock1, mock2]
    
    result = orch.analyze("ETH-USD", None, {}, {})
    
    assert result is not None
    assert result.strategy_id == "strategy_2"
    # Score is boosted by confluence (2 strategies agreed = +15)
    assert result.edge_score_base == 85  # 70 + 15 confluence boost
    assert result.confluence_count == 2


def test_orchestrator_returns_none_when_no_signals():
    """Test orchestrator returns None when no valid signals."""
    orch = StrategyOrchestrator(OrchestratorConfig(
        enable_burst_flag=False,
        enable_vwap_reclaim=False,
        enable_mean_reversion=False,
    ))
    
    mock = MagicMock()
    mock.analyze.return_value = None
    orch.strategies = [mock]
    
    result = orch.analyze("ETH-USD", None, {}, {})
    assert result is None


def test_orchestrator_stats():
    """Test orchestrator tracks statistics."""
    orch = StrategyOrchestrator(OrchestratorConfig(
        enable_burst_flag=False,
        enable_vwap_reclaim=False,
        enable_mean_reversion=False,
    ))
    
    mock = MagicMock()
    mock.strategy_id = "test_strat"
    mock.analyze.return_value = StrategySignal(
        symbol="SOL-USD",
        strategy_id="test_strat",
        direction=SignalDirection.LONG,
        edge_score_base=60,
        entry_price=50.0,
        stop_price=48.0,
    )
    orch.strategies = [mock]
    
    orch.analyze("SOL-USD", None, {}, {})
    orch.analyze("SOL-USD", None, {}, {})
    
    stats = orch.get_stats()
    assert stats["signals_generated"]["test_strat"] == 2
    assert stats["signals_selected"]["test_strat"] == 2


def test_signal_has_strategy_id():
    """Verify signals always have strategy_id for logging."""
    sig = StrategySignal(
        symbol="BTC-USD",
        strategy_id="burst_flag",
        direction=SignalDirection.LONG,
        edge_score_base=50,
        entry_price=100.0,
        stop_price=95.0,
    )
    
    # strategy_id should be preserved for logging
    assert sig.strategy_id == "burst_flag"
    assert "burst_flag" in str(sig.strategy_id)
