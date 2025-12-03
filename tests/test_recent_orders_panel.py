"""Tests for recent orders panel wiring."""

from apps.dashboard.dashboard_v2 import DashboardV2
from core.events import OrderEvent
from core.models import Side


def test_recent_orders_panel_handles_empty():
    dashboard = DashboardV2()
    panel = dashboard.render_recent_orders_panel()
    assert "No orders" in str(panel.renderable)


def test_recent_orders_panel_renders_events():
    dashboard = DashboardV2()
    open_evt = OrderEvent(
        event_type="open",
        symbol="TEST-USD",
        side=Side.BUY,
        mode="paper",
        price=100.0,
        size_usd=50.0,
        size_qty=0.5,
    )
    close_evt = OrderEvent(
        event_type="close",
        symbol="TEST-USD",
        side=Side.BUY,
        mode="paper",
        price=105.0,
        size_usd=50.0,
        size_qty=0.5,
        pnl=5.0,
        pnl_pct=10.0,
    )

    dashboard.state.recent_orders.appendleft(close_evt)
    dashboard.state.recent_orders.appendleft(open_evt)

    panel = dashboard.render_recent_orders_panel()
    text = str(panel.renderable)

    assert "TEST" in text
    assert "$50" in text
    assert "+5.00" in text
