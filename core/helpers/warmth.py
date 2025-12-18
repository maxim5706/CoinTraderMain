"""Centralized warmth checks to avoid inconsistent gate decisions."""

from typing import Optional

from datafeeds.universe.tiers import TierScheduler
from core.models import CandleBuffer


def is_warm(symbol: str, buffer: Optional[CandleBuffer], scheduler: TierScheduler) -> bool:
    """Return True if the symbol has enough history for trading.

    Syncs candle counts from the live buffer into the scheduler before checking.
    """

    if buffer:
        try:
            count_1m = len(buffer.candles_1m)
            count_5m = len(buffer.candles_5m)
            scheduler.update_candle_counts(symbol, count_1m, count_5m)
        except Exception:
            pass
    return scheduler.is_symbol_warm(symbol)
