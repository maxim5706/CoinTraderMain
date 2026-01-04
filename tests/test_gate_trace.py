from datetime import datetime, timezone

from core.models import Intent, SignalType
from core.mode_configs import PaperModeConfig
from core.position_registry import PositionRegistry
from core.shared_state import _serialize_state
from core.state import BotState
from execution.risk import CircuitBreaker, DailyStats
from execution.trade_planner import TradePlanner


class DummyExchangeSync:
    def validate_before_trade(self, symbol, get_price_func):
        return True


def test_gate_trace_populates():
    config = PaperModeConfig()
    planner = TradePlanner(
        positions={},
        position_registry=PositionRegistry(config),
        daily_stats=DailyStats(),
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
        confidence=0.9,
        spread_bps=5.0,
    )
    result = planner.plan_trade(intent, portfolio_value=10000, get_price_func=lambda _: 100.0)
    assert result.gate_result.trace
    assert any(g.name == "entry_score" for g in result.gate_result.trace)


def test_gate_trace_in_state_payload():
    state = BotState()
    state.last_gate_trace_by_symbol["BTC-USD"] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": "BTC-USD",
        "passed": False,
        "blocking_gate": "warmth",
        "blocking_reason": "not_warm",
        "gates": [],
    }
    payload = _serialize_state(state)
    assert "gate_traces" in payload
    assert payload["gate_traces"][0]["symbol"] == "BTC-USD"
