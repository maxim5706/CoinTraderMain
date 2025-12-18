"""Helpers to validate REST candle responses before buffering/persisting."""

from datetime import datetime
from typing import List

from core.models import Candle


def validate_candles(candles: List[Candle]) -> List[Candle]:
    """Return a cleaned, time-ordered list or an empty list if invalid."""
    if not candles:
        return []
    try:
        sorted_candles = sorted(candles, key=lambda c: c.timestamp)
        cleaned = [c for c in sorted_candles if isinstance(c.timestamp, datetime)]
        return cleaned
    except Exception:
        return []
