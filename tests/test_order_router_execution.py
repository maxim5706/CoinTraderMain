from datetime import datetime, timezone

import pytest

from core.models import Position, PositionState, Side, SignalType
from core.state import BotState
from execution.order_manager import order_manager
from execution.order_utils import STOP_LIMIT_GAP_PCT
from execution.order_router import OrderRouter
from logic.intelligence import intelligence
from datafeeds.universe import tier_scheduler


class FakeBuyClient:
    def __init__(self, order):
        self.order = order
        self.calls = []
        self.cancelled = []

    def market_order_buy(self, *_, **__):
        self.calls.append(("market",))
        return self.order

    def limit_order_gtc_buy(self, *_, **__):
        self.calls.append(("limit",))
        return self.order

    def cancel_orders(self, order_ids=None):
        self.cancelled.extend(order_ids or [])


class FakeStopClient:
    def __init__(self):
        self.stop_requests = []

    def stop_limit_order_gtc_sell(self, **kwargs):
        self.stop_requests.append(kwargs)
        return {"order_id": "stop123", "success": True}


def build_router(state=None, price=100.0, price_func=None):
    router = OrderRouter(get_price_func=price_func or (lambda _: price), state=state)
    router._portfolio_value = 1000.0
    router._usd_balance = 1000.0
    return router


@pytest.mark.asyncio
async def test_partial_fill_uses_filled_qty_for_stop(monkeypatch):
    fill_qty = 0.5
    order_response = {
        "order_id": "abc123",
        "filled_size": fill_qty,
        "average_filled_price": 100.0,
        "status": "FILLED",
    }
    fake_client = FakeBuyClient(order_response)
    router = build_router()
    router._client = fake_client

    stop_args = {}
    monkeypatch.setattr(
        order_manager,
        "place_stop_order",
        lambda **kwargs: stop_args.update(kwargs) or "stop-order-id",
    )

    position = await router._execute_live_buy(
        symbol="TEST-USD",
        qty=1.0,
        price=100.0,
        signal=None,
        stop_price=95.0,
        tp1_price=102.0,
        tp2_price=104.0,
        time_stop_min=30,
    )

    assert position is not None
    assert position.size_qty == pytest.approx(fill_qty)
    assert stop_args["qty"] == pytest.approx(fill_qty)


@pytest.mark.asyncio
async def test_limit_order_no_fill_returns_none(monkeypatch):
    order_response = {
        "order_id": "limit123",
        "filled_size": 0.0,
        "average_filled_price": 0.0,
        "status": "OPEN",
    }
    fake_client = FakeBuyClient(order_response)
    router = build_router()
    router._client = fake_client

    stop_called = {"count": 0}
    monkeypatch.setattr(
        order_manager,
        "place_stop_order",
        lambda **kwargs: stop_called.__setitem__("count", stop_called["count"] + 1),
    )

    position = await router._execute_live_buy(
        symbol="TEST-USD",
        qty=1.0,
        price=100.0,
        signal=None,
        stop_price=95.0,
        tp1_price=102.0,
        tp2_price=104.0,
        time_stop_min=30,
        use_limit=True,
    )

    assert position is None
    assert stop_called["count"] == 0
    assert fake_client.cancelled == ["limit123"]


def test_stop_limit_gap_applied(monkeypatch):
    fake_client = FakeStopClient()
    original_client = order_manager._client
    order_manager._client = fake_client

    try:
        order_manager.place_stop_order(symbol="ABC-USD", qty=1.0, stop_price=50.0)
    finally:
        order_manager._client = original_client

    assert fake_client.stop_requests
    request = fake_client.stop_requests[0]
    expected_limit = 50.0 * STOP_LIMIT_GAP_PCT
    assert float(request["limit_price"]) == pytest.approx(expected_limit)


@pytest.mark.asyncio
async def test_risk_off_trail_tightens_and_stays(monkeypatch):
    price_holder = {"price": 101.0}
    state = BotState()
    router = build_router(state=state, price_func=lambda _: price_holder["price"])

    # Ensure trailing logic can run without warm gate interference
    monkeypatch.setattr(tier_scheduler, "is_symbol_warm", lambda *_: True)

    position = Position(
        symbol="BTC-USD",
        side=Side.BUY,
        entry_price=100.0,
        entry_time=datetime.now(timezone.utc),
        size_usd=10.0,
        size_qty=0.1,
        stop_price=98.0,
        tp1_price=104.0,
        tp2_price=110.0,
        time_stop_min=30,
        state=PositionState.OPEN,
    )
    router.positions[position.symbol] = position

    prev_regime = intelligence._market_regime
    intelligence._market_regime = "risk_off"
    await router.check_exits(position.symbol)
    tightened_stop = position.stop_price

    intelligence._market_regime = "normal"
    price_holder["price"] = 102.0
    await router.check_exits(position.symbol)

    assert tightened_stop > 98.0
    assert position.stop_price >= tightened_stop

    intelligence._market_regime = prev_regime


def test_parse_order_response_market_vs_limit():
    """Verify parse_order_response uses correct partial fill logic for market vs limit."""
    from execution.order_utils import parse_order_response
    
    # Market order: filled $9.50 of expected $10 (5% tolerance)
    market_order = {
        'order_id': 'mkt123',
        'filled_size': 0.095,  # qty doesn't matter for market
        'filled_value': 9.50,  # This is what matters
        'average_filled_price': 100.0,
    }
    result = parse_order_response(market_order, expected_quote=10.0)
    assert result.success
    assert not result.partial_fill  # 9.50 >= 10 * 0.95, so not partial
    
    # Market order: only filled $8 of expected $10 → partial
    partial_market = {
        'order_id': 'mkt456',
        'filled_size': 0.08,
        'filled_value': 8.0,
        'average_filled_price': 100.0,
    }
    result = parse_order_response(partial_market, expected_quote=10.0)
    assert result.success
    assert result.partial_fill  # 8.0 < 10 * 0.95
    
    # Limit order: filled 0.099 of expected 0.1 (1% tolerance)
    limit_order = {
        'order_id': 'lmt123',
        'filled_size': 0.099,
        'filled_value': 9.9,
        'average_filled_price': 100.0,
    }
    result = parse_order_response(limit_order, expected_qty=0.1)
    assert result.success
    assert not result.partial_fill  # 0.099 >= 0.1 * 0.99
    
    # Limit order: only filled 0.05 of expected 0.1 → partial
    partial_limit = {
        'order_id': 'lmt456',
        'filled_size': 0.05,
        'filled_value': 5.0,
        'average_filled_price': 100.0,
    }
    result = parse_order_response(partial_limit, expected_qty=0.1)
    assert result.success
    assert result.partial_fill  # 0.05 < 0.1 * 0.99
