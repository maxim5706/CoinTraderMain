from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import persistence
from core.config import settings
from core.models import Position, PositionState, Side, Signal, SignalType
from execution.order_router import OrderRouter
from logic.intelligence import intelligence
from services import alerts
from datafeeds.universe import tier_scheduler


def make_signal(price: float = 100.0) -> Signal:
    sig = Signal(
        symbol="TEST-USD",
        type=SignalType.FLAG_BREAKOUT,
        timestamp=datetime.now(timezone.utc),
        price=price,
        stop_price=price * 0.98,
        tp1_price=price * 1.045,
        tp2_price=price * 1.08,
        reason="mode-test",
    )
    sig.spread_bps = 5.0  # Tight spread to pass gate
    return sig


@pytest.mark.asyncio
async def test_mode_matrix_same_decision(monkeypatch):
    # Ensure warmth and scoring pass
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
    monkeypatch.setattr(intelligence, "record_trade", lambda: None)
    intelligence._last_trade_time = None
    monkeypatch.setattr(persistence, "save_positions", lambda positions: None)
    monkeypatch.setattr(persistence, "load_positions", lambda: {})
    monkeypatch.setattr("core.persistence.sync_with_exchange", lambda client, positions: positions)
    monkeypatch.setattr(alerts, "alert_trade_entry", lambda **kwargs: None)

    signal = make_signal()

    # PAPER path
    orig_mode = settings.trading_mode
    orig_key = settings.coinbase_api_key
    orig_secret = settings.coinbase_api_secret
    settings.trading_mode = "paper"
    router_paper = OrderRouter(get_price_func=lambda *_: signal.price, state=None)
    router_paper._portfolio_value = 1000.0
    router_paper._usd_balance = 1000.0
    paper_position = await router_paper.open_position(signal)

    # LIVE path (use fake client + live buy)
    settings.trading_mode = "live"
    settings.coinbase_api_key = ""
    settings.coinbase_api_secret = ""
    monkeypatch.setattr(OrderRouter, "_init_live_client", lambda self: None)

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

    monkeypatch.setattr(OrderRouter, "_execute_live_buy", fake_live_buy)

    router_live = OrderRouter(get_price_func=lambda *_: signal.price, state=None)
    router_live._portfolio_value = 1000.0
    router_live._usd_balance = 1000.0
    live_position = await router_live.open_position(signal)

    # Restore mode
    settings.trading_mode = orig_mode
    settings.coinbase_api_key = orig_key
    settings.coinbase_api_secret = orig_secret

    assert paper_position is not None
    assert live_position is not None
    assert paper_position.stop_price == pytest.approx(live_position.stop_price)
    assert paper_position.tp1_price == pytest.approx(live_position.tp1_price)
    assert paper_position.tp2_price == pytest.approx(live_position.tp2_price)


def test_no_live_client_in_paper(monkeypatch):
    # Make sure live init is never invoked in paper mode even with keys present
    orig_mode = settings.trading_mode
    settings.trading_mode = "paper"
    settings.coinbase_api_key = "fake"
    settings.coinbase_api_secret = "fake"
    called = {"flag": False}

    def _init_live_client(self):
        called["flag"] = True

    monkeypatch.setattr(OrderRouter, "_init_live_client", _init_live_client)
    router = OrderRouter(get_price_func=lambda *_: 100.0, state=None)
    assert not called["flag"]
    assert router._client is None
    settings.trading_mode = orig_mode


@pytest.mark.asyncio
async def test_live_path_uses_live_execution(monkeypatch):
    orig_mode = settings.trading_mode
    orig_key = settings.coinbase_api_key
    orig_secret = settings.coinbase_api_secret
    settings.trading_mode = "live"
    settings.coinbase_api_key = ""
    settings.coinbase_api_secret = ""
    monkeypatch.setattr(persistence, "load_positions", lambda: {})
    monkeypatch.setattr(persistence, "save_positions", lambda positions: None)
    monkeypatch.setattr("core.persistence.sync_with_exchange", lambda client, positions: positions)

    # Ensure warmth passes and scoring passes
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
    intelligence._last_trade_time = None
    intelligence._last_trade_time = None

    live_called = {"flag": False}

    async def fake_live_buy(self, **kwargs):
        live_called["flag"] = True
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

    monkeypatch.setattr(OrderRouter, "_execute_live_buy", fake_live_buy)
    monkeypatch.setattr(OrderRouter, "_init_live_client", lambda self: setattr(self, "_client", object()))

    signal = make_signal()
    router = OrderRouter(get_price_func=lambda *_: signal.price, state=None)
    router._portfolio_value = 1000.0
    router._usd_balance = 1000.0
    await router.open_position(signal)

    assert live_called["flag"]
    settings.trading_mode = orig_mode
    settings.coinbase_api_key = orig_key
    settings.coinbase_api_secret = orig_secret
