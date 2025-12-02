import pytest
from unittest.mock import MagicMock

from core.config import settings
from core.profiles import ALLOWED_PROFILE_KEYS, PROFILES, apply_profile, is_test_profile


def test_apply_profile_overrides_and_reverts():
    original = {k: getattr(settings, k) for k in ALLOWED_PROFILE_KEYS}
    apply_profile("aggressive", settings)
    for key, value in PROFILES["aggressive"].items():
        assert getattr(settings, key) == value
    # Revert to original values manually, then prod profile (no-op)
    for key, value in original.items():
        setattr(settings, key, value)
    apply_profile("prod", settings)
    for key, value in original.items():
        assert getattr(settings, key) == value
    assert settings.profile == "prod"


def test_unknown_profile_rejected():
    with pytest.raises(ValueError):
        apply_profile("nonexistent", settings)


def test_live_mode_blocks_test_profile(monkeypatch):
    # Simulate live mode with test profile
    monkeypatch.setattr(settings, "trading_mode", "live")
    with pytest.raises(ValueError):
        apply_profile("test-profile", settings)


def test_is_test_profile():
    """Test detection of test profiles."""
    assert is_test_profile("test") is True
    assert is_test_profile("test-profile") is True
    assert is_test_profile("paper-profile") is False
    assert is_test_profile("live-profile") is False
    assert is_test_profile("prod") is False


def test_test_profile_blocked_in_live_mode():
    """Test profiles cannot be used with TRADING_MODE=live."""
    mock_settings = MagicMock()
    mock_settings.trading_mode = "live"
    
    with pytest.raises(ValueError, match="DANGER"):
        apply_profile("test", mock_settings)
    
    with pytest.raises(ValueError, match="DANGER"):
        apply_profile("test-profile", mock_settings)


def test_named_profiles_exist():
    """Verify all named profiles exist."""
    assert "paper-profile" in PROFILES
    assert "live-profile" in PROFILES
    assert "test-profile" in PROFILES
    assert "prod" in PROFILES


def test_paper_profile_settings():
    """Verify paper-profile has reasonable production settings."""
    paper = PROFILES["paper-profile"]
    assert paper["entry_score_min"] >= 55  # Selective
    assert paper["min_rr_ratio"] >= 1.5    # Good R:R
    assert paper["spread_max_bps"] <= 30   # Tight spreads


def test_live_profile_has_required_settings():
    """Live profile should have all required settings."""
    live = PROFILES["live-profile"]
    
    # Must have these keys
    assert "entry_score_min" in live
    assert "min_rr_ratio" in live
    assert "spread_max_bps" in live
    
    # Reasonable bounds
    assert 30 <= live["entry_score_min"] <= 70
    assert live["min_rr_ratio"] >= 1.5
    assert live["spread_max_bps"] <= 50
