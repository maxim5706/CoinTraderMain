from datetime import datetime, timezone

from types import SimpleNamespace

from run_v2 import TradingBotV2


class FakeScanner:
    def __init__(self, eligible_count: int, spicy_count: int, tier_counts: dict):
        self.universe = {f"S{i}": None for i in range(eligible_count * 2)}
        self._eligible = [f"E{i}" for i in range(eligible_count)]
        self._spicy = [f"P{i}" for i in range(spicy_count)]
        self._tiers = tier_counts
        self._last_universe_refresh = datetime.now(timezone.utc)

    def get_eligible_symbols(self):
        return list(self._eligible)

    def get_spicy_smallcaps(self):
        return list(self._spicy)

    def get_tier_symbols(self, tier: str):
        return [f"T{tier}{i}" for i in range(self._tiers.get(tier, 0))]


def test_universe_and_stream_counts_update():
    bot = TradingBotV2()
    bot.collector = SimpleNamespace(symbols=["A", "B"])
    bot.scanner = FakeScanner(eligible_count=3, spicy_count=1, tier_counts={"large": 1, "mid": 1, "small": 1, "micro": 0})

    bot._update_universe_state()

    state = bot.state.universe
    assert state.eligible_symbols == 3
    assert state.symbols_streaming == 2
    assert bot.state.live_log[0][2].startswith("Universe refreshed")

    # Update scanner/collector and ensure state/log refresh
    bot.collector.symbols = ["X"]
    bot.scanner = FakeScanner(eligible_count=5, spicy_count=2, tier_counts={"large": 0, "mid": 2, "small": 2, "micro": 1})
    bot._update_universe_state()

    state = bot.state.universe
    assert state.eligible_symbols == 5
    assert state.symbols_streaming == 1
    assert bot.state.live_log[0][2].startswith("Universe refreshed")
