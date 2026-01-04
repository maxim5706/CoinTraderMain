"""Tests for trade planning and sizing precedence."""

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core.config import settings
from core.helpers import GateReason
from core.models import Intent, Signal, SignalType
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.position_registry import PositionRegistry
from execution.entry_gates import PositionSizer
from execution.trade_planner import TradePlanner
from execution.risk import CircuitBreaker
from logic.intelligence import intelligence


@dataclass
class DummyConfig:
    max_trade_usd: float
    portfolio_max_exposure_pct: float
    min_position_usd: float


def _score(value: float):
    return SimpleNamespace(total_score=value)


def _signal(confluence_count: int = 1) -> Signal:
    signal = Signal(
        symbol="BTC-USD",
        type=SignalType.FLAG_BREAKOUT,
        timestamp=datetime.now(timezone.utc),
        price=100.0,
    )
    signal.confluence_count = confluence_count
    return signal


def _patch_sizing_settings(monkeypatch):
    monkeypatch.setattr(intelligence, "get_size_multiplier", lambda: 1.0)
    monkeypatch.setattr(settings, "whale_trade_usd", 50.0)
    monkeypatch.setattr(settings, "strong_trade_usd", 30.0)
    monkeypatch.setattr(settings, "normal_trade_usd", 20.0)
    monkeypatch.setattr(settings, "scout_trade_usd", 10.0)
    monkeypatch.setattr(settings, "whale_score_min", 85)
    monkeypatch.setattr(settings, "whale_confluence_min", 2)
    monkeypatch.setattr(settings, "strong_score_min", 70)
    monkeypatch.setattr(settings, "entry_score_min", 60.0)
    monkeypatch.setattr(settings, "scout_score_min", 50)
    monkeypatch.setattr(settings, "whale_max_positions", 99)
    monkeypatch.setattr(settings, "strong_max_positions", 99)
    monkeypatch.setattr(settings, "scout_max_positions", 99)
    monkeypatch.setattr(settings, "position_min_pct", 0.02)
    monkeypatch.setattr(settings, "position_max_pct", 0.08)


def test_sizing_precedence_by_score(monkeypatch):
    _patch_sizing_settings(monkeypatch)
    config = DummyConfig(max_trade_usd=50.0, portfolio_max_exposure_pct=0.8, min_position_usd=1.0)
    sizer = PositionSizer({}, config)
    portfolio_value = 1000.0

    whale = sizer.calculate_size(_score(90), _signal(confluence_count=2), portfolio_value)
    strong = sizer.calculate_size(_score(75), _signal(), portfolio_value)
    normal = sizer.calculate_size(_score(65), _signal(), portfolio_value)

    assert whale.size_usd == pytest.approx(50.0)
    assert strong.size_usd == pytest.approx(30.0)
    assert normal.size_usd == pytest.approx(20.0)


def test_sizing_exposure_cap(monkeypatch):
    _patch_sizing_settings(monkeypatch)
    positions = {"BTC-USD": SimpleNamespace(cost_basis=490.0)}
    config = DummyConfig(max_trade_usd=100.0, portfolio_max_exposure_pct=0.5, min_position_usd=1.0)
    sizer = PositionSizer(positions, config)

    sizing = sizer.calculate_size(_score(90), _signal(confluence_count=2), 1000.0)

    assert sizing.available_budget == pytest.approx(10.0)
    assert sizing.size_usd == pytest.approx(10.0)


def test_trade_planner_blocks_on_daily_loss():
    class DummyDailyStats:
        should_stop = True
        total_pnl = -100.0

    class DummyExchangeSync:
        exchange_holdings = {}

        def validate_before_trade(self, symbol, get_price_func):
            return True

    config = ConfigurationManager.get_config_for_mode(TradingMode.PAPER)
    registry = PositionRegistry(config)
    planner = TradePlanner(
        positions={},
        position_registry=registry,
        daily_stats=DummyDailyStats(),
        circuit_breaker=CircuitBreaker(),
        order_cooldown={},
        exchange_holdings={},
        cooldown_seconds=0,
        get_candle_buffer_func=lambda _: None,
        exchange_sync=DummyExchangeSync(),
        config=config,
        is_test=True,
    )

    intent = Intent(
        symbol="BTC-USD",
        type=SignalType.FLAG_BREAKOUT,
        timestamp=datetime.now(timezone.utc),
        price=100.0,
        strategy_id="test",
    )
    result = planner.plan_trade(intent, portfolio_value=1000.0, get_price_func=lambda _: 100.0)

    assert result.plan is None
    assert result.gate_result.reason == "daily_loss_limit"
    assert result.gate_result.gate == GateReason.RISK
