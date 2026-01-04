"""Tests for live feature computation."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from core.models import Candle
from logic.live_features import LiveFeatureEngine


def _make_candles(count: int, start_price: float = 100.0) -> list[Candle]:
    base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = []
    price = start_price
    for i in range(count):
        open_price = price
        close_price = price + 1.0
        high = close_price + 0.5
        low = open_price - 0.5
        volume = 100.0 + i
        candles.append(
            Candle(
                timestamp=base_time + timedelta(minutes=i),
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=volume,
            )
        )
        price = close_price
    return candles


def test_ema_series_matches_ema():
    engine = LiveFeatureEngine()
    data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    ema_series = engine._compute_ema_series(data, 3)
    assert ema_series[-1] == pytest.approx(engine._compute_ema(data, 3))


def test_compute_macd_signal_and_obv_slope():
    engine = LiveFeatureEngine()
    candles = _make_candles(40)
    indicators = engine.compute("BTC-USD", candles, [])
    assert indicators.macd_line != 0
    assert indicators.macd_signal != 0
    assert indicators.macd_histogram == pytest.approx(indicators.macd_line - indicators.macd_signal)
    assert indicators.obv_slope > 0
