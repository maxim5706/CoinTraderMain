"""
Mean Reversion Strategy - Fade extremes in quiet/range-bound conditions.

Only fires when:
- Vol regime is quiet/normal (NOT hot/crashy)
- Symbol is range-bound (ChopFilter says choppy)
- Price at Bollinger Band extremes with RSI confirmation
"""

from typing import Optional
from datetime import datetime, timezone

from .base import BaseStrategy, StrategySignal, SignalDirection
from core.config import settings


class MeanReversionStrategy(BaseStrategy):
    """
    Mean reversion at range extremes.
    
    Pattern:
    1. Vol regime = quiet/normal (ATR not elevated)
    2. Symbol is choppy/range-bound (not trending)
    3. Price near lower BB (< 0.15) with RSI < 30
    4. Target = VWAP or mid-band
    5. Tight stop outside range
    
    Hard gates inside strategy:
    - If vol_regime is hot/crashy → no signal
    - If strong trend detected → no signal
    """
    
    strategy_id = "mean_reversion"
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Analyze for mean reversion setup.
        
        Hard-gated to only fire in appropriate conditions.
        """
        if buffer is None or len(buffer.candles_1m) < 30:
            return None
        
        # === HARD GATES (strategy-specific) ===
        
        # Gate 1: Vol regime must be quiet/normal
        vol_regime = market_context.get('vol_regime', 'normal')
        if vol_regime in ('hot', 'crashy', 'extreme'):
            return None
        
        # Gate 2: BTC regime must be OK (not risk-off)
        btc_regime = market_context.get('btc_regime', 'normal')
        if btc_regime == 'risk_off':
            return None
        
        # Gate 3: Symbol must NOT be strongly trending
        trend_5m = features.get('trend_5m', 0)
        trend_1h = features.get('trend_1h', 0)
        
        # If aligned bullish trend, don't fade
        if trend_5m > 0.4 and trend_1h > 0.2:
            return None
        # If aligned bearish trend, don't try to catch falling knife
        if trend_5m < -0.4 and trend_1h < -0.2:
            return None
        
        # === PATTERN DETECTION ===
        
        candles = buffer.candles_1m
        current = candles[-1]
        price = current.close
        
        # Calculate Bollinger Bands
        bb_data = self._calculate_bb(candles, period=20, std_mult=2.0)
        if bb_data is None:
            return None
        
        bb_upper, bb_mid, bb_lower = bb_data
        bb_width = bb_upper - bb_lower
        
        if bb_width <= 0:
            return None
        
        # BB position (0 = lower band, 1 = upper band)
        bb_position = (price - bb_lower) / bb_width
        
        # Calculate RSI
        rsi = self._calculate_rsi(candles, period=7)
        
        # Get VWAP
        vwap = buffer.vwap(30)
        atr = buffer.atr(14, "1m")
        
        if vwap <= 0 or atr <= 0:
            return None
        
        # === ENTRY CONDITIONS ===
        
        # Long fade: near lower band + oversold RSI
        long_setup = bb_position < 0.2 and rsi < 35
        
        # Short fade would be: bb_position > 0.8 and rsi > 65
        # (Not implementing shorts for now)
        
        if not long_setup:
            return None
        
        # Additional confirmation: price holding above recent lows
        recent_low = min(c.low for c in candles[-5:])
        if price < recent_low:  # Breaking down, don't fade
            return None
        
        # Calculate edge score
        edge_score = self._calculate_edge_score(
            bb_position, rsi, trend_5m, vol_regime, buffer
        )
        
        # Calculate stops and targets
        stop_price = recent_low - (0.5 * atr)  # Below recent low
        risk = price - stop_price
        
        # MINIMUM RISK CHECK: Stop must be at least 2% below entry
        # (Otherwise fees eat all profit)
        min_risk = price * settings.fixed_stop_pct  # Use config stop %
        if risk < min_risk:
            # Enforce minimum stop distance
            stop_price = price * (1 - settings.fixed_stop_pct)
            risk = price - stop_price
        
        # Target = VWAP or mid-band, whichever is closer
        # But ensure TP1 is at least as far as our config TP1
        tp1_price = min(vwap, bb_mid)
        min_tp1 = price * (1 + settings.tp1_pct)  # Use config TP %
        if tp1_price < min_tp1:
            tp1_price = min_tp1
        
        tp2_price = max(vwap, bb_mid)
        min_tp2 = price * (1 + settings.tp2_pct)
        if tp2_price < min_tp2:
            tp2_price = min_tp2
        
        rr_ratio = (tp1_price - price) / risk if risk > 0 else 0
        
        # Need minimum R:R for mean reversion
        if rr_ratio < 1.5:
            return None
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=edge_score,
            
            # Score components
            trend_score=0,  # Not trend-based
            volume_score=0,
            pattern_score=self._pattern_quality_score(bb_position, rsi),
            timing_score=self._timing_score(candles),
            
            # Price levels
            entry_price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            
            # Geometry
            risk_pct=risk / price * 100 if price > 0 else 0,
            rr_ratio=rr_ratio,
            
            # Context
            reason=f"Mean reversion: BB {bb_position:.0%}, RSI {rsi:.0f}",
            reasons=[
                f"BB position {bb_position:.0%}",
                f"RSI7 {rsi:.0f}",
                f"Vol regime {vol_regime}",
                f"R:R {rr_ratio:.1f}",
            ],
            
            # Mean reversion data
            bb_position=bb_position,
            rsi=rsi,
        )
    
    def _calculate_bb(self, candles, period: int = 20, std_mult: float = 2.0):
        """Calculate Bollinger Bands."""
        if len(candles) < period:
            return None
        
        closes = [c.close for c in candles[-period:]]
        sma = sum(closes) / period
        
        variance = sum((c - sma) ** 2 for c in closes) / period
        std = variance ** 0.5
        
        upper = sma + (std_mult * std)
        lower = sma - (std_mult * std)
        
        return upper, sma, lower
    
    def _calculate_rsi(self, candles, period: int = 7) -> float:
        """Calculate RSI."""
        if len(candles) < period + 1:
            return 50.0
        
        changes = []
        for i in range(-period, 0):
            changes.append(candles[i].close - candles[i-1].close)
        
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.0001
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def _calculate_edge_score(
        self, bb_position, rsi, trend_5m, vol_regime, buffer
    ) -> float:
        """Calculate base edge score (0-100)."""
        score = 20  # Base for valid setup
        
        # BB extremity (up to 30 points)
        if bb_position < 0.1:
            score += 30
        elif bb_position < 0.15:
            score += 25
        elif bb_position < 0.2:
            score += 15
        
        # RSI oversold (up to 25 points)
        if rsi < 20:
            score += 25
        elif rsi < 25:
            score += 20
        elif rsi < 30:
            score += 15
        elif rsi < 35:
            score += 10
        
        # Quiet vol regime bonus (up to 15 points)
        if vol_regime == 'quiet':
            score += 15
        elif vol_regime == 'normal':
            score += 10
        
        # Neutral trend bonus (not fighting momentum)
        if abs(trend_5m) < 0.1:
            score += 10
        elif abs(trend_5m) < 0.2:
            score += 5
        
        return min(score, 100)
    
    def _pattern_quality_score(self, bb_position: float, rsi: float) -> float:
        """Score pattern quality."""
        score = 0
        if bb_position < 0.15:
            score += 20
        if rsi < 25:
            score += 15
        return score
    
    def _timing_score(self, candles) -> float:
        """Score timing based on recent price action."""
        if len(candles) < 3:
            return 0
        
        # Bullish reversal candle?
        last = candles[-1]
        prev = candles[-2]
        
        # Higher low
        if last.low > prev.low:
            return 10
        return 0
    
    def reset(self, symbol: str):
        """Reset state for symbol."""
        pass
