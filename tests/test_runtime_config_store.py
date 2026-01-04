"""Tests for runtime config snapshots."""

from core.config import settings
from core.mode_config import RuntimeConfigStore
from core.mode_configs import TradingMode


def test_runtime_config_store_refresh_updates_running_config():
    store = RuntimeConfigStore(TradingMode.PAPER)
    start_config = store.start_config
    running_config = store.running_config
    assert start_config.max_trade_usd == running_config.max_trade_usd

    original_max_trade = settings.max_trade_usd
    try:
        settings.max_trade_usd = original_max_trade + 1
        refreshed = store.refresh()
        assert refreshed.max_trade_usd == settings.max_trade_usd
        assert start_config.max_trade_usd == original_max_trade
    finally:
        settings.max_trade_usd = original_max_trade
        store.refresh()
