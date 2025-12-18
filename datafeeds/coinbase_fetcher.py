"""Coinbase REST candle fetcher with windowing and rate limiting."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List

from coinbase.rest import RESTClient

from core.logging_utils import get_logger
from core.models import Candle

logger = get_logger(__name__)

# Coinbase returns max 300 candles per request; cap rps to avoid 429s
_MAX_CANDLES = 300
_client: RESTClient | None = None


class _TokenBucket:
    """Simple token bucket for global rate limiting."""

    def __init__(self, rps: float = 4.0, burst: float = 4.0):  # Reduced from 8 to avoid 429s
        self.capacity = burst
        self.tokens = burst
        self.rps = rps
        self.last_refill = time.time()

    def acquire(self, cost: float = 1.0):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rps)
        self.last_refill = now
        wait = max(0.0, cost - self.tokens) / self.rps if self.tokens < cost else 0.0
        if wait > 0:
            time.sleep(wait)
            self.tokens = max(0.0, self.tokens - cost + wait * self.rps)
        else:
            self.tokens -= cost


_bucket = _TokenBucket()

_granularity_map = {
    60: "ONE_MINUTE",
    300: "FIVE_MINUTE",
    900: "FIFTEEN_MINUTE",
    3600: "ONE_HOUR",
    21600: "SIX_HOUR",
    86400: "ONE_DAY",
}


def _get_client() -> RESTClient:
    global _client
    if _client is None:
        _client = RESTClient()  # Public data; no auth required
    return _client


def fetch_history_windowed(
    symbol: str,
    granularity_s: int = 60,
    lookback_minutes: int = 90,
    max_retries: int = 3,
) -> List[Candle]:
    """
    Fetch candles using Coinbase public endpoint, windowed to respect 300-bar limit.
    Returns oldest→newest candles.
    """
    granularity = _granularity_map.get(granularity_s, "ONE_MINUTE")
    total_seconds = max(lookback_minutes, 1) * 60
    chunk_seconds = granularity_s * _MAX_CANDLES  # e.g., 300m for 1m bars

    end_ts = int(time.time())
    start_ts = end_ts - total_seconds
    cursor = start_ts
    candles: List[Candle] = []

    while cursor < end_ts:
        chunk_end = min(cursor + chunk_seconds, end_ts)
        # Coinbase expects start/end as unix seconds strings
        for attempt in range(max_retries):
            try:
                _bucket.acquire()
                resp = _get_client().get_public_candles(
                    product_id=symbol,
                    start=str(cursor),
                    end=str(chunk_end),
                    granularity=granularity,
                )
                raw = getattr(resp, "candles", None) or []
                for c in raw:
                    try:
                        ts = datetime.fromtimestamp(int(getattr(c, "start", 0)), tz=timezone.utc)
                        candle = Candle(
                            timestamp=ts,
                            open=float(getattr(c, "open", 0) or 0),
                            high=float(getattr(c, "high", 0) or 0),
                            low=float(getattr(c, "low", 0) or 0),
                            close=float(getattr(c, "close", 0) or 0),
                            volume=float(getattr(c, "volume", 0) or 0),
                        )
                        candles.append(candle)
                    except ValueError as e:
                        # Skip invalid candles (Candle.__post_init__ validation failed)
                        logger.debug("[CB-FETCH] Skipping invalid candle for %s: %s", symbol, e)
                        continue
                break  # chunk succeeded
            except Exception as e:
                is_rate = "429" in str(e) or "Too Many Requests" in str(e)
                if attempt < max_retries - 1:
                    backoff = 0.5 if not is_rate else min(30, 2 ** attempt)
                    time.sleep(backoff)
                    continue
                logger.warning("[CB-FETCH] %s %ss chunk failed: %s", symbol, granularity_s, e)
        cursor = chunk_end

    candles.sort(key=lambda c: c.timestamp)
    return candles
