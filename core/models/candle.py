"""Candle primitives and rolling buffer utilities."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from core.logger import log_candle_5m, utc_iso_str


@dataclass
class Candle:
    """OHLCV candle data."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    
    @property
    def range(self) -> float:
        return self.high - self.low
    
    @property
    def body(self) -> float:
        return abs(self.close - self.open)
    
    @property
    def is_green(self) -> bool:
        return self.close >= self.open
    
    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2
    
    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)
    
    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


@dataclass
class CandleBuffer:
    """Rolling buffer of candles with computed indicators."""
    symbol: str
    candles_1m: list[Candle] = field(default_factory=list)
    candles_5m: list[Candle] = field(default_factory=list)
    candles_1h: list[Candle] = field(default_factory=list)  # Higher timeframe
    candles_1d: list[Candle] = field(default_factory=list)  # Daily candles
    max_1m: int = 120  # 2 hours
    max_5m: int = 48   # 4 hours
    max_1h: int = 48   # 48 hours
    max_1d: int = 30   # 30 days
    
    def add_1m(self, candle: Candle):
        # For backfill: avoid duplicates
        if self.candles_1m and candle.timestamp <= self.candles_1m[-1].timestamp:
            # Check if exact duplicate
            if any(c.timestamp == candle.timestamp for c in self.candles_1m[-5:]):
                return
            # Insert in order for historical data
            self.candles_1m.append(candle)
            self.candles_1m.sort(key=lambda c: c.timestamp)
        else:
            self.candles_1m.append(candle)
        
        if len(self.candles_1m) > self.max_1m:
            self.candles_1m = self.candles_1m[-self.max_1m:]
        # Aggregate to 5m when appropriate
        self._maybe_aggregate_5m()
    
    def add_5m_direct(self, candle: Candle):
        """Add a 5m candle directly (for backfill, bypassing aggregation)."""
        # Avoid duplicates by checking timestamp
        if self.candles_5m and candle.timestamp <= self.candles_5m[-1].timestamp:
            return
        self.candles_5m.append(candle)
        if len(self.candles_5m) > self.max_5m:
            self.candles_5m = self.candles_5m[-self.max_5m:]
    
    def _maybe_aggregate_5m(self):
        """Aggregate 1m candles to 5m when we have 5 complete."""
        if len(self.candles_1m) < 5:
            return
        # Check if we should create new 5m candle
        last_1m = self.candles_1m[-1]
        if last_1m.timestamp.minute % 5 == 4:  # End of 5m period
            last_5 = self.candles_1m[-5:]
            candle_5m = Candle(
                timestamp=last_5[0].timestamp,
                open=last_5[0].open,
                high=max(c.high for c in last_5),
                low=min(c.low for c in last_5),
                close=last_5[-1].close,
                volume=sum(c.volume for c in last_5),
            )
            self.add_5m_direct(candle_5m)
            
            # Log 5m candle for analytics
            log_candle_5m({
                "ts": utc_iso_str(last_5[-1].timestamp),
                "symbol": self.symbol,
                "open": candle_5m.open,
                "high": candle_5m.high,
                "low": candle_5m.low,
                "close": candle_5m.close,
                "volume": candle_5m.volume,
            }, last_5[-1].timestamp)
    
    def get_closes(self, timeframe: str = "1m") -> list[float]:
        if timeframe == "1m":
            return [c.close for c in self.candles_1m]
        elif timeframe == "5m":
            return [c.close for c in self.candles_5m]
        elif timeframe == "1h":
            return [c.close for c in self.candles_1h]
        elif timeframe == "1d":
            return [c.close for c in self.candles_1d]
        return []
    
    def get_volumes(self, timeframe: str = "1m") -> list[float]:
        """Get volume values for the specified timeframe."""
        if timeframe == "1m":
            return [c.volume for c in self.candles_1m]
        elif timeframe == "5m":
            return [c.volume for c in self.candles_5m]
        elif timeframe == "1h":
            return [c.volume for c in self.candles_1h]
        elif timeframe == "1d":
            return [c.volume for c in self.candles_1d]
        return []
    
    def get_ranges(self, timeframe: str = "1m") -> list[float]:
        """Get high-low range values for the specified timeframe."""
        if timeframe == "1m":
            return [c.range for c in self.candles_1m]
        elif timeframe == "5m":
            return [c.range for c in self.candles_5m]
        elif timeframe == "1h":
            return [c.range for c in self.candles_1h]
        elif timeframe == "1d":
            return [c.range for c in self.candles_1d]
        return []
    
    def vwap(self, periods: int = 30) -> float:
        """Calculate VWAP over last N 1m candles."""
        if len(self.candles_1m) < periods:
            # Not enough history yet; fall back to last close
            return self.candles_1m[-1].close if self.candles_1m else 0.0
        recent = self.candles_1m[-periods:]
        total_vol = sum(c.volume for c in recent)
        if total_vol == 0:
            return recent[-1].close
        return sum(c.midpoint * c.volume for c in recent) / total_vol
    
    def ema(self, period: int = 20, timeframe: str = "5m") -> float:
        """Calculate EMA."""
        closes = self.get_closes(timeframe)
        if len(closes) < period:
            return closes[-1] if len(closes) > 0 else 0.0
        multiplier = 2 / (period + 1)
        ema = closes[0]
        for price in closes[1:]:
            ema = (price - ema) * multiplier + ema
        return ema
    
    def atr(self, period: int = 14, timeframe: str = "1m") -> float:
        """Calculate ATR."""
        candles = self.candles_1m if timeframe == "1m" else self.candles_5m
        if len(candles) < period + 1:
            return candles[-1].range if candles else 0.0
        
        tr_values = []
        for i in range(1, len(candles)):
            high = candles[i].high
            low = candles[i].low
            prev_close = candles[i-1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            tr_values.append(tr)
        
        return np.mean(tr_values[-period:])
    
    @property
    def last_price(self) -> float:
        if self.candles_1m:
            return self.candles_1m[-1].close
        return 0.0

