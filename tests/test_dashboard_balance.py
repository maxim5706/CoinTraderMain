"""Tests for dashboard balance display by mode."""

import pytest
from datetime import datetime, timezone

from apps.dashboard.dashboard_v2 import DashboardV2
from core.state import BotState


def test_paper_mode_shows_paper_balance():
    """In PAPER mode, top bar shows simulated paper balance, not real."""
    dashboard = DashboardV2()
    dashboard.state.mode = "paper"
    dashboard.state.paper_balance = 1000.0
    dashboard.state.paper_positions_value = 50.0
    dashboard.state.portfolio_value = 5000.0  # Real balance (should NOT show)
    dashboard.state.cash_balance = 4000.0
    dashboard.state.holdings_value = 1000.0
    
    top_bar = dashboard.render_top_bar()
    text = str(top_bar)
    
    # Should show paper total ($1050)
    assert "$1050.00" in text or "$1,050.00" in text
    assert "(paper)" in text
    
    # Should NOT show real balance
    assert "$5000" not in text and "$5,000" not in text
    assert "$4000" not in text and "$4,000" not in text


def test_live_mode_shows_real_balance():
    """In LIVE mode, top bar shows real portfolio balance."""
    dashboard = DashboardV2()
    dashboard.state.mode = "live"
    dashboard.state.paper_balance = 1000.0  # Paper (should NOT show)
    dashboard.state.paper_positions_value = 0.0
    dashboard.state.portfolio_value = 5000.0
    dashboard.state.cash_balance = 4000.0
    dashboard.state.holdings_value = 1000.0
    
    top_bar = dashboard.render_top_bar()
    text = str(top_bar)
    
    # Should show real balance
    assert "$5000.00" in text or "$5,000.00" in text
    
    # Should NOT show paper label
    assert "(paper)" not in text


def test_paper_mode_api_shows_paper():
    """In PAPER mode, API status shows PAPER."""
    dashboard = DashboardV2()
    dashboard.state.mode = "paper"
    dashboard.state.api_ok = True
    
    top_bar = dashboard.render_top_bar()
    text = str(top_bar)
    
    assert "PAPER" in text
    # Should not show READONLY or OK
    assert "READONLY" not in text


def test_live_mode_api_shows_live_or_fail():
    """In LIVE mode, API status shows LIVE or FAIL."""
    dashboard = DashboardV2()
    dashboard.state.mode = "live"
    
    # API OK
    dashboard.state.api_ok = True
    top_bar = dashboard.render_top_bar()
    assert "LIVE" in str(top_bar)
    
    # API FAIL
    dashboard.state.api_ok = False
    top_bar = dashboard.render_top_bar()
    assert "FAIL" in str(top_bar)


def test_live_panel_shows_paper_in_paper_mode():
    """LIVE positions panel shows PAPER label in PAPER mode."""
    dashboard = DashboardV2()
    dashboard.state.mode = "paper"
    
    panel = dashboard.render_live_positions_panel()
    # Should show PAPER title in paper mode
    assert "PAPER" in str(panel.title)


def test_paper_panel_shows_info_in_live_mode():
    """In LIVE mode, paper panel shows info instead."""
    dashboard = DashboardV2()
    dashboard.state.mode = "live"
    dashboard.state.profile = "live-profile"
    
    panel = dashboard.render_paper_positions_panel()
    # Should show Info panel in LIVE mode (not PAPER)
    assert "Info" in str(panel.title) or "LIVE" in str(panel.title)


def test_paper_start_balance_default():
    """Paper balance defaults to $1000."""
    state = BotState()
    assert state.paper_balance == 1000.0


def test_paper_total_includes_positions():
    """Paper total = paper_balance + paper_positions_value."""
    dashboard = DashboardV2()
    dashboard.state.mode = "paper"
    dashboard.state.paper_balance = 950.0
    dashboard.state.paper_positions_value = 50.0
    
    top_bar = dashboard.render_top_bar()
    text = str(top_bar)
    
    # Total should be $1000
    assert "$1000.00" in text or "$1,000.00" in text


def test_paper_profile_sets_balance_1000():
    """Paper-profile should set paper_start_balance_usd to 1000."""
    from core.profiles import PROFILES
    assert PROFILES["paper-profile"]["paper_start_balance_usd"] == 1000.0


def test_paper_balance_uses_config():
    """OrderRouter should use settings.paper_start_balance_usd, not hardcoded."""
    from core.config import settings
    from execution.order_router import OrderRouter
    from core.state import BotState
    
    # Temporarily set paper mode
    import os
    old_mode = os.environ.get("TRADING_MODE", "")
    os.environ["TRADING_MODE"] = "paper"
    
    try:
        # Reload settings to pick up env var
        from importlib import reload
        import core.config
        reload(core.config)
        from core.config import settings as fresh_settings
        
        state = BotState()
        # Create router in paper mode
        router = OrderRouter(get_price_func=lambda s: 100.0, state=state)
        
        # Should use paper_start_balance_usd from config (default 1000)
        assert router._usd_balance == fresh_settings.paper_start_balance_usd
        assert router._portfolio_value == fresh_settings.paper_start_balance_usd
    finally:
        if old_mode:
            os.environ["TRADING_MODE"] = old_mode
        else:
            os.environ.pop("TRADING_MODE", None)
