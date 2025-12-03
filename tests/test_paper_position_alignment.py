import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import persistence
from core.config import settings
from core.models import Position, PositionState, Side, Signal, SignalType
from execution.order_manager import order_manager as om_instance
from execution.order_router import OrderRouter
from logic.intelligence import intelligence
from services import alerts
from datafeeds.universe import tier_scheduler


def make_signal(price: float = 100.0, stop: float = 98.0, tp1: float = 102.0, tp2: float = 103.0) -> Signal:
    sig = Signal(
        symbol="TEST-USD",
        type=SignalType.FLAG_BREAKOUT,
        timestamp=datetime.now(timezone.utc),
        price=price,
        stop_price=stop,
        tp1_price=tp1,
        tp2_price=tp2,
        reason="align-test",
    )
    sig.spread_bps = 5.0  # Tight spread to pass gate
    return sig


def configure_common_monkeypatches(monkeypatch):
    monkeypatch.setattr(tier_scheduler, "is_symbol_warm", lambda *_: True)
    score_stub = SimpleNamespace(
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
    monkeypatch.setattr(intelligence, "score_entry", lambda *_, **__: score_stub)
    monkeypatch.setattr(intelligence, "check_position_limits", lambda *_, **__: (True, "OK"))
    monkeypatch.setattr(intelligence, "record_trade", lambda: None)
    monkeypatch.setattr(intelligence, "log_trade_entry", lambda *_, **__: None)
    monkeypatch.setattr(intelligence, "log_trade_exit", lambda *_, **__: None)
    intelligence._last_trade_time = None
    monkeypatch.setattr(persistence, "save_positions", lambda positions: None)
    monkeypatch.setattr(persistence, "load_positions", lambda: {})
    monkeypatch.setattr(alerts, "alert_trade_entry", lambda **kwargs: None)
    monkeypatch.setattr(alerts, "alert_trade_exit", lambda **kwargs: None)
    monkeypatch.setattr(om_instance, "update_stop_price", lambda *_, **__: None)
    monkeypatch.setattr(om_instance, "cancel_stop_order", lambda *_, **__: None)
    monkeypatch.setattr(om_instance, "place_stop_order", lambda *_, **__: None)


async def run_path(mode: str, monkeypatch) -> tuple:
    from tests.test_helpers import create_test_router_with_mocks
    from core.mode_configs import TradingMode
    
    price_holder = {"price": 100.0}
    orig_mode = settings.trading_mode
    orig_key = settings.coinbase_api_key
    orig_secret = settings.coinbase_api_secret
    settings.coinbase_api_key = ""
    settings.coinbase_api_secret = ""
    settings.trading_mode = mode
    configure_common_monkeypatches(monkeypatch)

    # Create router with proper DI mocks and better R:R ratio
    trading_mode = TradingMode.PAPER if mode == "paper" else TradingMode.LIVE
    router = create_test_router_with_mocks(
        mode=trading_mode,
        balance=1000.0,
        fixed_stop_pct=0.025,  # 2.5% stop for better R:R
        tp1_pct=0.05,          # 5% TP1 for better R:R
        min_rr_ratio=1.5       # Lower R:R requirement for tests
    )
    
    # Override price function
    router.get_price = lambda *_: price_holder["price"]
    
    # Mock truth sync to allow trades in test mode
    router._validate_before_trade = lambda *_: True

    signal = make_signal(price=100.0, stop=97.5, tp1=105.0, tp2=107.0)  # Better R:R ratio

    position = await router.open_position(signal)
    assert position is not None

    # Normalize stops/targets for deterministic path
    position.stop_price = 98.0
    position.tp1_price = 102.0
    position.tp2_price = 103.0
    router.positions[position.symbol] = position

    # Hit TP1 (partial)
    price_holder["price"] = 102.1
    await router.check_exits(position.symbol)
    post_tp1 = router.positions[position.symbol]

    # Hit TP2 (full exit)
    price_holder["price"] = 103.5
    trade = await router.check_exits(position.symbol)

    settings.trading_mode = orig_mode
    settings.coinbase_api_key = orig_key
    settings.coinbase_api_secret = orig_secret
    return post_tp1, trade, router.daily_stats.total_pnl


@pytest.mark.asyncio
async def test_paper_vs_live_path_alignment(monkeypatch):
    paper_state, paper_trade, paper_pnl = await run_path("paper", monkeypatch)
    live_state, live_trade, live_pnl = await run_path("live", monkeypatch)

    assert paper_state.partial_closed
    assert live_state.partial_closed
    assert paper_state.stop_price == pytest.approx(live_state.stop_price)
    assert paper_trade.exit_reason == "tp2"
    assert live_trade.exit_reason == "tp2"
    assert paper_pnl == pytest.approx(live_pnl)
