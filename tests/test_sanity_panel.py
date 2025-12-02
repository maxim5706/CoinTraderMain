"""Tests for startup sanity panel and health check features."""

import pytest
from datetime import datetime, timezone, timedelta

from core.state import BotState
from execution.order_router import OrderRouter


def test_state_has_sanity_fields():
    """Verify BotState has all fields needed for sanity panel."""
    state = BotState()
    
    # Startup fields
    assert hasattr(state, 'startup_time')
    assert hasattr(state, 'profile')
    assert hasattr(state, 'mode')
    
    # WS fields
    assert hasattr(state, 'ws_ok')
    assert hasattr(state, 'ws_last_age')
    assert hasattr(state, 'ws_reconnect_count')
    
    # ML fields
    assert hasattr(state, 'ml_fresh_pct')
    assert hasattr(state, 'ml_total_cached')
    assert hasattr(state, 'ml_score')
    assert hasattr(state, 'ml_confidence')
    
    # Rejection counters
    assert hasattr(state, 'rejections_warmth')
    assert hasattr(state, 'rejections_regime')
    assert hasattr(state, 'rejections_score')
    assert hasattr(state, 'rejections_rr')
    assert hasattr(state, 'rejections_spread')
    assert hasattr(state, 'rejections_limits')


def test_rejection_counter_wiring():
    """Verify OrderRouter._record_rejection updates state correctly."""
    state = BotState()
    router = OrderRouter(get_price_func=lambda *_: 100.0, state=state)
    
    # Record each type
    router._record_rejection('warmth')
    router._record_rejection('regime')
    router._record_rejection('score')
    router._record_rejection('rr')
    router._record_rejection('spread')
    router._record_rejection('limits')
    
    assert state.rejections_warmth == 1
    assert state.rejections_regime == 1
    assert state.rejections_score == 1
    assert state.rejections_rr == 1
    assert state.rejections_spread == 1
    assert state.rejections_limits == 1


def test_rejection_counter_no_state():
    """Verify _record_rejection doesn't crash without state."""
    router = OrderRouter(get_price_func=lambda *_: 100.0, state=None)
    
    # Should not raise
    router._record_rejection('warmth')
    router._record_rejection('invalid_type')


def test_ml_staleness():
    """Verify ML staleness detection works."""
    from features.live import LiveMLResult
    
    # Fresh ML
    fresh = LiveMLResult(symbol='TEST', raw_score=0.5, confidence=0.7)
    assert not fresh.is_stale()
    assert fresh.age_seconds < 1
    
    # Stale ML (5 min old)
    stale = LiveMLResult(symbol='TEST', raw_score=0.5, confidence=0.7)
    stale.timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert stale.is_stale()
    assert stale.age_seconds > 180


def test_intelligence_rejects_stale_ml():
    """Verify intelligence.get_live_ml returns None for stale ML."""
    from logic.intelligence import intelligence
    from features.live import LiveMLResult
    
    # Set stale ML
    stale = LiveMLResult(symbol='STALE-USD', raw_score=0.5, confidence=0.7)
    stale.timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)
    intelligence.live_ml['STALE-USD'] = stale
    
    # Should return None for stale
    result = intelligence.get_live_ml('STALE-USD')
    assert result is None
    
    # Set fresh ML
    fresh = LiveMLResult(symbol='FRESH-USD', raw_score=0.5, confidence=0.7)
    intelligence.live_ml['FRESH-USD'] = fresh
    
    # Should return the fresh one
    result = intelligence.get_live_ml('FRESH-USD')
    assert result is not None
    assert result.raw_score == 0.5


def test_dashboard_sanity_panel_renders():
    """Verify sanity panel renders without error."""
    from apps.dashboard.dashboard_v2 import DashboardV2
    
    dashboard = DashboardV2()
    dashboard.state.startup_time = datetime.now(timezone.utc)
    dashboard.state.profile = 'prod'
    dashboard.state.ws_ok = True
    dashboard.state.ws_last_age = 1.5
    dashboard.state.ml_fresh_pct = 85.0
    dashboard.state.ml_total_cached = 30
    
    # Should not raise
    panel = dashboard.render_sanity_panel()
    assert panel is not None


def test_health_check_runs():
    """Verify health_check.py can be imported and run."""
    from tools.health_check import run_health_check, HealthStatus
    
    status = run_health_check()
    assert isinstance(status, HealthStatus)
    assert hasattr(status, 'is_healthy')
    assert hasattr(status, 'summary')
    
    # Summary should be a string
    summary = status.summary()
    assert isinstance(summary, str)
    assert 'HEALTH CHECK' in summary


def test_indicator_staleness():
    """Verify LiveIndicators.is_stale works correctly."""
    from features.live import LiveIndicators
    
    # Fresh indicators
    fresh = LiveIndicators(symbol='TEST-USD')
    assert not fresh.is_stale()
    
    # Stale indicators (3 min old)
    stale = LiveIndicators(symbol='TEST-USD')
    stale.timestamp = datetime.now(timezone.utc) - timedelta(minutes=3)
    assert stale.is_stale()  # Default max_age is 120s


def test_intelligence_rejects_stale_indicators():
    """Verify intelligence.get_live_indicators returns None for stale indicators."""
    from logic.intelligence import intelligence
    from features.live import LiveIndicators
    
    # Set stale indicators
    stale = LiveIndicators(symbol='STALE-IND-USD')
    stale.timestamp = datetime.now(timezone.utc) - timedelta(minutes=5)
    intelligence.live_indicators['STALE-IND-USD'] = stale
    
    # Should return None for stale
    result = intelligence.get_live_indicators('STALE-IND-USD')
    assert result is None
    
    # Set fresh indicators
    fresh = LiveIndicators(symbol='FRESH-IND-USD')
    intelligence.live_indicators['FRESH-IND-USD'] = fresh
    
    # Should return the fresh one
    result = intelligence.get_live_indicators('FRESH-IND-USD')
    assert result is not None
