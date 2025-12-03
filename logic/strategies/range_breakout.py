"""
Range Breakout Strategy.

Detects tight consolidation ranges and enters on volume breakout.
"Buy the squeeze before the explosion."
"""

from typing import Optional
from datetime import datetime, timezone

from .base import BaseStrategy, StrategySignal, SignalDirection
from core.config import settings


class RangeBreakoutStrategy(BaseStrategy):
    """
    Range consolidation breakout strategy.
    
    Pattern:
    1. Price consolidates in tight range (< 2% height) for 10+ candles
    2. Volume dries up during consolidation (vol < 0.7x average)
    3. Breakout candle: close above range high with volume > 1.5x
    4. Entry on breakout confirmation
    
    Edge: Tight ranges precede explosive moves. Low risk (stop below range),
    high reward (target = range height × 2).
    """
    
    strategy_id = "range_breakout"
    
    # Track detected ranges per symbol
    _range_cache: dict = {}  # symbol -> {high, low, candle_count, vol_avg, detected_at}
    
    # Configuration
    MIN_RANGE_CANDLES = 8       # Minimum candles in range
    MAX_RANGE_PCT = 0.025       # Max 2.5% range height
    MIN_RANGE_PCT = 0.005       # Min 0.5% (avoid noise)
    VOL_DECAY_THRESHOLD = 0.8   # Volume should drop during consolidation
    BREAKOUT_VOL_MULT = 1.3     # Breakout needs 1.3x volume
    
    def __init__(self):
        self._range_cache = {}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Analyze for range breakout setup.
        """
        if buffer is None or len(buffer.candles_1m) < 30:
            return None
        
        # Use 5m candles for cleaner range detection
        candles_5m = buffer.candles_5m
        if len(candles_5m) < 15:
            return None
        
        candles_1m = buffer.candles_1m
        current = candles_1m[-1]
        price = current.close
        
        # Check for existing range or detect new one
        range_data = self._detect_or_update_range(symbol, candles_5m)
        
        if range_data is None:
            return None
        
        range_high = range_data['high']
        range_low = range_data['low']
        range_pct = (range_high - range_low) / range_low * 100
        candle_count = range_data['candle_count']
        
        # Check for breakout
        breakout_signal = self._check_breakout(
            symbol, price, current, range_data, candles_1m, features
        )
        
        if breakout_signal is None:
            return None
        
        # Calculate levels
        range_height = range_high - range_low
        stop_price = range_low * 0.995  # Just below range low
        tp1_price = price + range_height * 1.5  # 1.5x range
        tp2_price = price + range_height * 2.5  # 2.5x range
        
        risk_pct = abs(price - stop_price) / price * 100
        rr_ratio = (tp1_price - price) / (price - stop_price) if price > stop_price else 0
        
        # Score based on range quality
        score = self._score_setup(range_data, features, breakout_signal)
        
        if score < 60:
            return None
        
        reason = (
            f"Range: {range_pct:.1f}% ({candle_count} candles), "
            f"breakout vol: {breakout_signal['vol_mult']:.1f}x"
        )
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=score,
            trend_score=features.get('trend_5m', 0) * 10 + 50,
            volume_score=min(100, breakout_signal['vol_mult'] * 40),
            pattern_score=min(100, candle_count * 5),
            timing_score=breakout_signal.get('timing_score', 50),
            entry_price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            risk_pct=risk_pct,
            rr_ratio=rr_ratio,
            reason=reason,
            reasons=[
                f"range_{range_pct:.1f}%",
                f"{candle_count}_candles",
                f"vol_{breakout_signal['vol_mult']:.1f}x"
            ],
        )
    
    def _detect_or_update_range(self, symbol: str, candles_5m: list) -> Optional[dict]:
        """Detect consolidation range in 5m candles."""
        if len(candles_5m) < 10:
            return None
        
        # Look at last 20 candles for range detection
        lookback = candles_5m[-20:]
        
        # Find potential range (exclude last 2 candles for breakout detection)
        range_candles = lookback[:-2]
        
        if len(range_candles) < self.MIN_RANGE_CANDLES:
            return None
        
        # Calculate range bounds
        highs = [c.high for c in range_candles]
        lows = [c.low for c in range_candles]
        
        range_high = max(highs)
        range_low = min(lows)
        
        if range_low <= 0:
            return None
        
        range_pct = (range_high - range_low) / range_low
        
        # Check if range is tight enough
        if range_pct > self.MAX_RANGE_PCT or range_pct < self.MIN_RANGE_PCT:
            self._range_cache.pop(symbol, None)
            return None
        
        # Count candles that stayed within range
        candles_in_range = 0
        for c in range_candles:
            if c.low >= range_low * 0.995 and c.high <= range_high * 1.005:
                candles_in_range += 1
        
        if candles_in_range < self.MIN_RANGE_CANDLES:
            self._range_cache.pop(symbol, None)
            return None
        
        # Calculate average volume during consolidation
        vol_avg = sum(c.volume for c in range_candles) / len(range_candles)
        
        # Check for volume decay (sign of consolidation)
        recent_vol = sum(c.volume for c in range_candles[-5:]) / 5
        vol_decay_ratio = recent_vol / vol_avg if vol_avg > 0 else 1.0
        
        # Update cache
        self._range_cache[symbol] = {
            'high': range_high,
            'low': range_low,
            'range_pct': range_pct,
            'candle_count': candles_in_range,
            'vol_avg': vol_avg,
            'vol_decay': vol_decay_ratio,
            'detected_at': datetime.now(timezone.utc),
        }
        
        return self._range_cache[symbol]
    
    def _check_breakout(
        self,
        symbol: str,
        price: float,
        current_candle,
        range_data: dict,
        candles_1m: list,
        features: dict
    ) -> Optional[dict]:
        """Check if current price action is a valid breakout."""
        range_high = range_data['high']
        range_low = range_data['low']
        vol_avg = range_data['vol_avg']
        
        # Must be above range high for long breakout
        if price <= range_high:
            return None
        
        # Breakout amount (how far above range)
        breakout_pct = (price - range_high) / range_high * 100
        
        # Don't chase - breakout should be fresh (< 1%)
        if breakout_pct > 1.5:
            return None
        
        # Check volume on breakout (use last 3 1m candles)
        if len(candles_1m) < 3:
            return None
        
        breakout_vol = sum(c.volume for c in candles_1m[-3:]) / 3
        vol_mult = breakout_vol / vol_avg if vol_avg > 0 else 0
        
        # Need elevated volume on breakout
        if vol_mult < self.BREAKOUT_VOL_MULT:
            return None
        
        # Check trend alignment (prefer breakout with trend)
        trend_5m = features.get('trend_5m', 0)
        if trend_5m < -0.5:  # Against trend
            return None
        
        # Timing score based on how fresh the breakout is
        timing_score = 80 - (breakout_pct * 30)  # Fresher = better
        
        return {
            'vol_mult': vol_mult,
            'breakout_pct': breakout_pct,
            'timing_score': max(50, timing_score),
        }
    
    def _score_setup(self, range_data: dict, features: dict, breakout: dict) -> float:
        """Score the overall setup quality."""
        score = 50  # Base
        
        # Range quality (tighter = better, up to a point)
        range_pct = range_data['range_pct'] * 100
        if 0.8 <= range_pct <= 1.5:
            score += 15  # Sweet spot
        elif 0.5 <= range_pct <= 2.0:
            score += 10
        
        # Candle count (more consolidation = stronger breakout)
        candles = range_data['candle_count']
        if candles >= 15:
            score += 15
        elif candles >= 10:
            score += 10
        elif candles >= 8:
            score += 5
        
        # Volume decay during consolidation (sign of coiling)
        vol_decay = range_data.get('vol_decay', 1.0)
        if vol_decay < 0.7:
            score += 10  # Strong volume decay
        elif vol_decay < 0.9:
            score += 5
        
        # Breakout volume (higher = more conviction)
        vol_mult = breakout['vol_mult']
        if vol_mult >= 2.0:
            score += 15
        elif vol_mult >= 1.5:
            score += 10
        elif vol_mult >= 1.3:
            score += 5
        
        # Trend alignment
        trend_5m = features.get('trend_5m', 0)
        if trend_5m > 0.5:
            score += 10
        elif trend_5m > 0:
            score += 5
        
        return min(100, score)
    
    def reset(self, symbol: str):
        """Reset state for symbol."""
        self._range_cache.pop(symbol, None)
