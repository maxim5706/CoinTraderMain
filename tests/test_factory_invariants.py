import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.models import Candle, Signal, SignalType
from core.state import BotState
from datafeeds.collectors.candle_collector import CandleCollector
from datafeeds.universe.tiers import TierConfig, TierScheduler
from execution.order_router import OrderRouter
from features.live.live_features import LiveFeatureEngine, LiveIndicators
from logic.intelligence import CANONICAL_GATE_ORDER
from tools.health_check import check_startup_health, run_health_check


@pytest.mark.asyncio
async def test_candle_collector_update_symbols_preserves_buffers():
    collector = CandleCollector(symbols=["BTC-USD"])
    candle = Candle(
        timestamp=datetime.now(timezone.utc),
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100.0,
    )
    collector.buffers["BTC-USD"].add_1m(candle)

    await collector.update_symbols(["BTC-USD", "DOGE-USD", "BTC-USD"])

    assert collector.symbols == ["BTC-USD", "DOGE-USD"]
    assert len(collector.buffers["BTC-USD"].candles_1m) == 1
    assert "DOGE-USD" in collector.buffers
    assert collector.buffers["DOGE-USD"].candles_1m == []


def test_tier_scheduler_reassign_tiers_triggers_callbacks():
    scheduler = TierScheduler(TierConfig(tier1_size=1, tier2_size=1))
    adds: list[str] = []
    removes: list[str] = []

    scheduler.on_ws_add = lambda sym: adds.append(sym)
    scheduler.on_ws_remove = lambda sym: removes.append(sym)

    scheduler.reassign_tiers(["AAA-USD", "BBB-USD", "CCC-USD"])
    assert scheduler.get_tier1_symbols() == ["AAA-USD"]
    assert set(scheduler.get_tier2_symbols()) == {"BBB-USD"}
    assert adds == ["AAA-USD"]
    assert removes == []

    scheduler.reassign_tiers(["BBB-USD", "AAA-USD", "CCC-USD"])
    assert scheduler.get_tier1_symbols() == ["BBB-USD"]
    assert set(removes) == {"AAA-USD"}
    assert "BBB-USD" in adds


def test_live_indicators_staleness_detection():
    ind = LiveIndicators(symbol="TEST")
    assert not ind.is_stale(max_age_seconds=10)

    ind.timestamp = datetime.now(timezone.utc) - timedelta(seconds=300)
    assert ind.is_stale(max_age_seconds=10)


def test_feature_engine_update_higher_tf_wires_state():
    engine = LiveFeatureEngine()
    now = datetime.now(timezone.utc)
    candles_1h = [
        Candle(timestamp=now - timedelta(hours=1), open=1, high=2, low=0.5, close=1.5, volume=10),
        Candle(timestamp=now, open=1.5, high=2.5, low=1.0, close=2.0, volume=12),
    ]
    candles_1d = [
        Candle(timestamp=now - timedelta(days=1), open=1, high=2, low=0.5, close=1.5, volume=10),
        Candle(timestamp=now, open=1.5, high=2.5, low=1.0, close=2.0, volume=12),
    ]

    engine.update_higher_tf("TEST", candles_1h, candles_1d)

    state = engine.state["TEST"]
    assert state.closes_1h[-1] == 2.0
    assert state.closes_1d[-1] == 2.0
    assert len(state.closes_1h) == 2
    assert len(state.closes_1d) == 2


@pytest.mark.asyncio
async def test_order_router_respects_tier_warmth(monkeypatch):
    state = BotState()
    router = OrderRouter(get_price_func=lambda *_: 100.0, state=state)
    signal = Signal(
        symbol="COOL-USD",
        type=SignalType.FLAG_BREAKOUT,
        timestamp=datetime.now(timezone.utc),
        price=10.0,
        stop_price=9.0,
        tp1_price=11.0,
        tp2_price=12.0,
    )

    from datafeeds import universe

    monkeypatch.setattr(universe.tier_scheduler, "is_symbol_warm", lambda *_: False)

    result = await router.open_position(signal)

    assert result is None
    assert state.rejections_warmth == 1


def test_health_check_flags_stale_ml():
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    snapshot_path = data_dir / "status.json"

    original = snapshot_path.read_text() if snapshot_path.exists() else None
    snapshot = {
        "ws_ok": True,
        "ws_last_age": 1.0,
        "ml": {"fresh_pct": 20, "total_cached": 10},
        "universe": {"eligible": 30, "warm": 10, "cold": 20, "tier1": 10, "tier2": 10, "tier3": 10},
        "rejections": {"spread": 0, "warmth": 0, "regime": 0},
    }
    snapshot_path.write_text(json.dumps(snapshot))

    try:
        status = run_health_check()
        assert any("ML cache degraded" in issue for issue in status.issues)
    finally:
        if original is None:
            snapshot_path.unlink(missing_ok=True)
        else:
            snapshot_path.write_text(original)


def test_gate_order_constant():
    expected = [
        "warmth",
        "limits",
        "spread",
        "score_regime",
        "risk_reward",
        "budget",
        "ml_boost",
    ]
    assert CANONICAL_GATE_ORDER == expected


def test_check_startup_health_with_mocked_state():
    state = BotState()
    now = datetime.now(timezone.utc)
    state.ws_ok = True
    state.ws_last_msg_time = now
    state.ws_last_age = 2.0
    state.ws_reconnect_count = 0
    state.warm_symbols = 20
    state.cold_symbols = 5
    state.tier1_count = 15
    state.tier2_count = 10
    state.tier3_count = 5
    state.candles_last_5s = 10

    status = check_startup_health(state, min_runtime_s=10)

    assert status.is_healthy
    assert not status.issues
