from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.config import settings
from core.models import Signal, SignalType
from core.state import BotState
from execution.order_router import OrderRouter
from logic.intelligence import EntryScore, intelligence
from logic.live_features import LiveIndicators, LiveMLResult, live_scorer
from datafeeds.universe import tier_scheduler


def make_signal(
    symbol: str = "TEST-USD",
    price: float = 100.0,
    stop_price: float = 99.0,
    tp1: float = 100.5,
    tp2: float = 104.0,
    spread_bps: float = 5.0,  # Default tight spread
) -> Signal:
    sig = Signal(
        symbol=symbol,
        type=SignalType.FLAG_BREAKOUT,
        timestamp=datetime.now(timezone.utc),
        price=price,
        stop_price=stop_price,
        tp1_price=tp1,
        tp2_price=tp2,
        reason="unit-test",
    )
    sig.spread_bps = spread_bps
    return sig


@pytest.mark.asyncio
async def test_warmth_gate_blocks_before_scoring(monkeypatch):
    from tests.test_helpers import create_test_router_with_mocks
    from core.mode_configs import TradingMode
    
    state = BotState()
    router = create_test_router_with_mocks(
        mode=TradingMode.PAPER,
        balance=1000.0,
        fixed_stop_pct=0.01,  # 1% for tight stop
        min_rr_ratio=1.0      # Allow any R:R for this test
    )
    router.state = state

    # Warmth gate should trigger first and avoid ML/score calls
    monkeypatch.setattr(tier_scheduler, "is_symbol_warm", lambda *_: False)

    score_called = {"called": False}

    def fake_score_entry(*args, **kwargs):
        score_called["called"] = True
        return None

    monkeypatch.setattr(intelligence, "score_entry", fake_score_entry)

    signal = make_signal()
    result = await router.open_position(signal)

    assert result is None
    assert state.rejections_warmth == 1
    assert state.rejections_regime == 0
    assert state.rejections_score == 0
    assert not score_called["called"]


@pytest.mark.asyncio
async def test_regime_rejection_counters(monkeypatch):
    state = BotState()
    router = OrderRouter(get_price_func=lambda _: 100.0, state=state)
    router._portfolio_value = 1000.0
    router._usd_balance = 1000.0

    monkeypatch.setattr(tier_scheduler, "is_symbol_warm", lambda *_: True)
    intelligence._market_regime = "risk_off"

    entry_score = EntryScore(symbol="XYZ")
    entry_score.total_score = 70
    entry_score.volume_score = 15
    entry_score.trend_score = 15
    entry_score.symbol_trend_ok = True
    entry_score.not_overbought = True
    entry_score.btc_regime = "risk_off"
    entry_score.btc_trend_ok = False  # Regime gate should block
    entry_score.reasons.append("risk_off")

    monkeypatch.setattr(intelligence, "score_entry", lambda *_, **__: entry_score)
    monkeypatch.setattr(intelligence, "check_position_limits", lambda *_, **__: (True, "OK"))
    intelligence._last_trade_time = None

    signal = make_signal()
    result = await router.open_position(signal)

    assert result is None
    assert state.rejections_regime == 1
    assert state.rejections_score == 0
    assert state.rejections_rr == 0


@pytest.mark.asyncio
async def test_rr_rejection_counter(monkeypatch):
    from tests.test_helpers import create_test_router_with_mocks
    from core.mode_configs import TradingMode
    
    state = BotState()
    router = create_test_router_with_mocks(
        mode=TradingMode.PAPER,
        balance=1000.0,
        fixed_stop_pct=0.004,  # 0.4% stop (will fail R:R with min_rr_ratio=5.0)
        tp1_pct=0.003,         # 0.3% TP1 (poor R:R ratio)
        min_rr_ratio=5.0       # Very high R:R requirement
    )
    router.state = state

    monkeypatch.setattr(tier_scheduler, "is_symbol_warm", lambda *_: True)
    intelligence._market_regime = "normal"

    # Force should_enter = True so R:R gate is reached
    stub_score = SimpleNamespace(
        should_enter=True,
        total_score=80,
        reasons=["ok"],
        trend_score=15,
        volume_score=15,
        vwap_score=10,
        range_score=5,
        tier_score=5,
        spread_score=10,
    )
    monkeypatch.setattr(intelligence, "score_entry", lambda *_, **__: stub_score)
    monkeypatch.setattr(intelligence, "check_position_limits", lambda *_, **__: (True, "OK"))
    monkeypatch.setattr(intelligence, "record_trade", lambda: None)
    intelligence._last_trade_time = None

    # Bad R:R geometry - 0.4% stop vs 0.3% TP1 = 0.75 R:R ratio (< 5.0 required)
    signal = make_signal(price=100.0, stop_price=99.6, tp1=100.3, tp2=101.0)
    result = await router.open_position(signal)

    assert result is None
    assert state.rejections_rr == 1
    assert state.rejections_score == 0
    assert state.rejections_regime == 0


def test_stale_ml_neutral_behavior():
    symbol = "ADA-USD"
    intelligence.live_ml.clear()
    intelligence.live_indicators.clear()
    intelligence._market_regime = "normal"

    signal = make_signal(symbol=symbol, price=10.0, stop_price=9.7, tp1=10.5, tp2=11.0)
    burst_metrics = {
        "vol_spike": 3.0,
        "range_spike": 3.0,
        "trend_15m": 2.0,
        "vwap_distance": 1.0,
        "spread_bps": 5.0,
        "tier": "mid",
    }

    base = intelligence.score_entry(signal, burst_metrics, {})
    base_score = base.total_score

    stale_ml = LiveMLResult(
        symbol=symbol,
        raw_score=0.9,
        confidence=0.9,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=200),
    )
    intelligence.live_ml[symbol] = stale_ml

    scored = intelligence.score_entry(signal, burst_metrics, {})
    assert scored.total_score == pytest.approx(base_score)
    assert scored.ml_boost == 0.0
    assert scored.ml_score == 0.0


def test_single_ml_call_path(monkeypatch):
    symbol = "SOL-USD"
    intelligence.live_ml.clear()
    intelligence.live_indicators.clear()
    intelligence._market_regime = "normal"

    call_count = {"count": 0}

    def fake_score(ind):
        call_count["count"] += 1
        return LiveMLResult(symbol=ind.symbol, raw_score=0.4, confidence=0.8)

    monkeypatch.setattr(live_scorer, "score_from_indicators", fake_score)

    indicators = LiveIndicators(symbol=symbol, is_ready=True)
    intelligence.update_live_indicators(symbol, indicators)

    burst_metrics = {
        "vol_spike": 2.0,
        "range_spike": 2.0,
        "trend_15m": 1.5,
        "vwap_distance": 0.2,
        "spread_bps": 5.0,
        "tier": "mid",
    }
    signal = make_signal(symbol=symbol, price=20.0, stop_price=19.5, tp1=21.0, tp2=22.0)

    _ = intelligence.score_entry(signal, burst_metrics, {})

    assert call_count["count"] == 1  # Scored once during update, not during score_entry


@pytest.mark.asyncio
async def test_gate_funnel_distribution(monkeypatch):
    from tests.test_helpers import create_test_router_with_mocks
    from core.mode_configs import TradingMode
    
    state = BotState()
    # Create router with high R:R ratio requirement to test the gate
    router = create_test_router_with_mocks(
        mode=TradingMode.PAPER,
        balance=1000.0,
        min_rr_ratio=5.0,  # High R:R requirement to trigger rejection
        fixed_stop_pct=0.01,  # 1% stop
        tp1_pct=0.005         # 0.5% TP1 (bad R:R)
    )
    router.state = state
    intelligence._market_regime = "risk_off"
    monkeypatch.setattr(intelligence, "check_position_limits", lambda *_, **__: (True, "OK"))
    monkeypatch.setattr(intelligence, "record_trade", lambda: None)
    intelligence._last_trade_time = None

    # Warmth gate fails first, then other gates on subsequent calls
    warm_calls = iter([False, True, True, True])
    monkeypatch.setattr(tier_scheduler, "is_symbol_warm", lambda *_: next(warm_calls))

    score_calls = {"count": 0}

    def score_regime(*_, **__):
        score_calls["count"] += 1
        es = EntryScore(symbol="X")
        es.total_score = 70
        es.volume_score = 15
        es.trend_score = 15
        es.symbol_trend_ok = True
        es.not_overbought = True
        es.btc_trend_ok = False  # regime gate fails
        es.btc_regime = "risk_off"
        return es

    def score_low(*_, **__):
        score_calls["count"] += 1
        es = EntryScore(symbol="X")
        es.total_score = 30
        es.volume_score = 0  # score gate fails
        es.trend_score = 5
        es.symbol_trend_ok = True
        es.not_overbought = True
        es.btc_trend_ok = True
        es.btc_regime = "normal"
        return es

    def score_pass(*_, **__):
        score_calls["count"] += 1
        es = EntryScore(symbol="X")
        es.total_score = 80
        es.volume_score = 15
        es.trend_score = 15
        es.symbol_trend_ok = True
        es.not_overbought = True
        es.btc_trend_ok = True
        es.btc_regime = "normal"
        return es

    # Sequence: warmth fail, regime fail, score fail, R:R fail (stop >= price)
    score_seq = iter([score_regime, score_low, score_pass])

    def score_entry(*args, **kwargs):
        func = next(score_seq)
        return func(*args, **kwargs)

    monkeypatch.setattr(intelligence, "score_entry", score_entry)

    signals = [
        make_signal(),  # warmth fail
        make_signal(),  # regime fail
        make_signal(),  # score fail
        make_signal(stop_price=101.0, tp1=102.0, tp2=103.0),  # R:R fail
    ]

    for sig in signals:
        await router.open_position(sig)

    assert state.rejections_warmth == 1
    assert state.rejections_regime == 1
    assert state.rejections_score == 1
    assert state.rejections_rr == 1
    assert state.rejections_limits == 0
    assert state.rejections_spread == 0
    # Score called only on paths past warmth and spread gates
    assert score_calls["count"] == 3


@pytest.mark.asyncio
async def test_spread_gate_blocks_wide_spreads(monkeypatch):
    """Spread gate rejects signals with spreads wider than spread_max_bps."""
    from core.models import SignalType
    
    state = BotState()
    router = OrderRouter(get_price_func=lambda _: 100.0, state=state)
    router._portfolio_value = 1000.0
    router._usd_balance = 1000.0
    
    # Mark as warm
    monkeypatch.setattr(tier_scheduler, "is_symbol_warm", lambda _: True)
    monkeypatch.setattr(intelligence, "check_position_limits", lambda *_, **__: (True, "OK"))
    intelligence._last_trade_time = None
    
    # Set spread_max_bps to 20
    monkeypatch.setattr(settings, "spread_max_bps", 20.0)
    
    # Signal with wide spread (50 bps > 20 max)
    wide_spread_signal = make_signal(spread_bps=50.0)
    
    result = await router.open_position(wide_spread_signal)
    assert result is None
    assert state.rejections_spread == 1
    
    # Signal with tight spread (10 bps < 20 max) - should pass spread gate
    # (will fail elsewhere, but spread counter shouldn't increment)
    tight_spread_signal = make_signal(spread_bps=10.0)
    
    # Need to stub score to fail (so it doesn't try to trade)
    stub_score = SimpleNamespace(
        should_enter=False, 
        total_score=30, 
        reasons=["test"],
        btc_trend_ok=False,
    )
    monkeypatch.setattr(intelligence, "score_entry", lambda *_, **__: stub_score)
    
    result = await router.open_position(tight_spread_signal)
    assert result is None
    assert state.rejections_spread == 1  # Still 1, not incremented
