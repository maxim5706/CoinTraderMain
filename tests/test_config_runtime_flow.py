"""Integration test for runtime config propagation."""

from core.config import settings
from core.config_manager import get_config_manager
from core.mode_config import RuntimeConfigStore
from core.mode_configs import TradingMode
from execution.order_router import OrderRouter


def test_runtime_config_flow_updates_router():
    manager = get_config_manager()
    store = RuntimeConfigStore(TradingMode.PAPER)
    original = settings.vol_spike_threshold
    new_value = original + 0.1 if original < 9.9 else original - 0.1

    assert store.start_config.vol_spike_threshold == original

    try:
        result = manager.update_param("vol_spike_threshold", new_value, source="test")
        assert result["success"]
        assert settings.vol_spike_threshold == new_value

        running = store.refresh()
        assert running.vol_spike_threshold == new_value

        router = OrderRouter(get_price_func=lambda _: 0.0, mode=TradingMode.PAPER, config=running)
        assert router.config.vol_spike_threshold == new_value

        result = manager.update_param("vol_spike_threshold", original, source="test")
        assert result["success"]
        refreshed = store.refresh()
        router.update_config(refreshed)
        assert router.config.vol_spike_threshold == original
    finally:
        if settings.vol_spike_threshold != original:
            manager.update_param("vol_spike_threshold", original, source="test")
            store.refresh()
