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
    price_holder = {"price": 100.0}
    orig_mode = settings.trading_mode
    orig_key = settings.coinbase_api_key
    orig_secret = settings.coinbase_api_secret
    settings.coinbase_api_key = ""
    settings.coinbase_api_secret = ""
    settings.trading_mode = mode
    configure_common_monkeypatches(monkeypatch)
    monkeypatch.setattr(OrderRouter, "_refresh_balance", lambda self: None)

    # Build router with deterministic price getter
    router = OrderRouter(get_price_func=lambda *_: price_holder["price"], state=None)
    router._portfolio_value = 1000.0
    router._usd_balance = 1000.0

    signal = make_signal()

    if mode == "live":
        async def fake_live_buy(self, **kwargs):
            return Position(
                symbol=kwargs["symbol"],
                side=Side.BUY,
                entry_price=kwargs["price"],
                entry_time=datetime.now(timezone.utc),
                size_usd=kwargs["price"] * kwargs["qty"],
                size_qty=kwargs["qty"],
                stop_price=kwargs["stop_price"],
                tp1_price=kwargs["tp1_price"],
                tp2_price=kwargs["tp2_price"],
                time_stop_min=kwargs["time_stop_min"],
                state=PositionState.OPEN,
            )

        async def fake_live_sell(self, symbol: str, qty: float):
            return None

        monkeypatch.setattr(OrderRouter, "_execute_live_buy", fake_live_buy)
        monkeypatch.setattr(OrderRouter, "_execute_live_sell", fake_live_sell)
        monkeypatch.setattr(OrderRouter, "_init_live_client", lambda self: setattr(self, "_client", object()))
        router._client = object()
    else:
        router._client = None

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
