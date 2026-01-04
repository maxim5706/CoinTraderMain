"""Tests for runtime config reload and snapshot redaction."""

import json

from core.config import settings
from core.config_manager import RuntimeConfig, get_config_manager
from core.mode_config import ConfigurationManager, sanitize_config_snapshot
from core.mode_configs import TradingMode


def test_reload_from_disk_force_updates_and_notifies(tmp_path):
    manager = get_config_manager()
    original_config = manager.get_config().to_dict()
    original_config_file = manager._config_file
    original_data_dir = manager._data_dir
    original_last_loaded = manager._last_loaded_mtime
    original_callbacks = list(manager._callbacks)
    original_exposure = settings.portfolio_max_exposure_pct

    new_config = dict(original_config)
    new_config["max_exposure_pct"] = 42.0
    new_config["updated_at"] = "2025-12-30T00:00:00Z"
    new_config["updated_by"] = "test"

    try:
        config_file = tmp_path / "runtime_config.json"
        config_file.write_text(json.dumps(new_config))

        manager._data_dir = tmp_path
        manager._config_file = config_file

        called = {"count": 0, "value": None}

        def _callback(cfg):
            called["count"] += 1
            called["value"] = cfg.max_exposure_pct

        manager._callbacks = list(original_callbacks)
        manager.register_callback(_callback)

        changed = manager.reload_from_disk(force=True)
        assert changed is True
        assert called["count"] == 1
        assert called["value"] == 42.0
        assert settings.portfolio_max_exposure_pct == 0.42
    finally:
        manager._callbacks = original_callbacks
        manager._data_dir = original_data_dir
        manager._config_file = original_config_file
        manager._last_loaded_mtime = original_last_loaded
        manager._config = RuntimeConfig.from_dict(original_config)
        manager._apply_all_to_settings()
        settings.portfolio_max_exposure_pct = original_exposure


def test_snapshot_redaction_after_reload(monkeypatch):
    manager = get_config_manager()
    manager.reload_from_disk(force=True)

    monkeypatch.setattr(settings, "coinbase_api_key", "test_key")
    monkeypatch.setattr(settings, "coinbase_api_secret", "test_secret")

    config = ConfigurationManager.get_config_for_mode(TradingMode.LIVE)
    snapshot = sanitize_config_snapshot(config)

    assert snapshot["api_key"] == "REDACTED"
    assert snapshot["api_secret"] == "REDACTED"
