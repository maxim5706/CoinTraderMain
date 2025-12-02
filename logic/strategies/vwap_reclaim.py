"""
VWAP Reclaim / Pullback Continuation Strategy.

Entry on pullback to VWAP or EMA zone after impulse, with volume reclaim.
"Buy the first good dip in a strong intraday trend."
"""

from typing import Optional
from datetime import datetime, timezone

from .base import BaseStrategy, StrategySignal, SignalDirection
from core.config import settings


class VWAPReclaimStrategy(BaseStrategy):
    """
    VWAP/EMA pullback continuation in established trend.
    
    Pattern:
    1. Prior impulse detected (trend established)
    2. Pullback depth: 0.3-0.8× ATR back toward VWAP/EMA zone
    3. Reclaim trigger: close back above VWAP with volume > 1.3×
    4. Bonus: buy pressure (close in top 30% of candle range)
    
    Edge score based on:
    - Trend strength (5m/15m alignment)
    - Pullback quality (depth, held support)
    - Reclaim quality (volume, candle structure)
    """
    
    strategy_id = "vwap_reclaim"
    
    # Track impulses we've seen for pullback entry
    _impulse_memory: dict = {}  # symbol -> {high, timestamp, trend_score}
    
    def __init__(self):
        self._impulse_memory = {}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Analyze for VWAP reclaim pattern.
        """
        if buffer is None or len(buffer.candles_1m) < 20:
            return None
        
        # Need established trend
        trend_5m = features.get('trend_5m', 0)
        trend_1h = features.get('trend_1h', 0)
        
        if trend_5m <= 0 or trend_1h < -0.3:
            # Clear memory if trend lost
            self._impulse_memory.pop(symbol, None)
            return None
        
        # Get current data
        candles = buffer.candles_1m
        current = candles[-1]
        price = current.close
        vwap = buffer.vwap(30)  # 30-candle VWAP
        atr = buffer.atr(14, "1m")
        
        if vwap <= 0 or atr <= 0:
            return None
        
        # Calculate VWAP distance
        vwap_dist_pct = (price - vwap) / vwap * 100
        vwap_dist_atr = (price - vwap) / atr if atr > 0 else 0
        
        # Track impulse highs for pullback reference
        self._update_impulse_memory(symbol, buffer, features)
        
        impulse_ref = self._impulse_memory.get(symbol)
        if impulse_ref is None:
            return None
        
        # Check for pullback setup
        pullback_depth = (impulse_ref['high'] - price) / atr if atr > 0 else 0
        
        # Pullback criteria: 0.3-0.8 ATR pullback, price near VWAP zone
        valid_pullback = (
            0.3 <= pullback_depth <= 1.2  # Pulled back but not too far
            and abs(vwap_dist_atr) < 0.5  # Near VWAP zone
        )
        
        if not valid_pullback:
            return None
        
        # Reclaim trigger: closing above VWAP with volume
        vol_ratio = features.get('vol_ratio', 1.0)
        above_vwap = price > vwap
        volume_confirm = vol_ratio >= 1.3
        
        # Buy pressure: close in top portion of candle
        candle_range = current.high - current.low
        if candle_range > 0:
            close_position = (current.close - current.low) / candle_range
        else:
            close_position = 0.5
        buy_pressure = close_position >= 0.6  # Close in top 40%
        
        # Need reclaim trigger
        if not (above_vwap and (volume_confirm or buy_pressure)):
            return None
        
        # Calculate edge score
        edge_score = self._calculate_edge_score(
            trend_5m, trend_1h, pullback_depth, vol_ratio, 
            close_position, vwap_dist_pct
        )
        
        # Calculate stops and targets
        stop_price = min(current.low, vwap) - (0.3 * atr)  # Below VWAP/candle low
        risk = price - stop_price
        
        # TP1 at impulse high, TP2 at extension
        tp1_price = impulse_ref['high']
        tp2_price = impulse_ref['high'] + (impulse_ref['high'] - vwap) * 0.5
        
        rr_ratio = (tp1_price - price) / risk if risk > 0 else 0
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=edge_score,
            
            # Score components
            trend_score=min(trend_5m * 20, 25),
            volume_score=min(vol_ratio * 10, 20),
            pattern_score=self._pullback_quality_score(pullback_depth, close_position),
            timing_score=10 if buy_pressure else 0,
            
            # Price levels
            entry_price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            
            # Geometry
            risk_pct=risk / price * 100 if price > 0 else 0,
            rr_ratio=rr_ratio,
            
            # Context
            reason=f"VWAP reclaim: pullback {pullback_depth:.1f}×ATR, vol {vol_ratio:.1f}×",
            reasons=[
                f"Pullback {pullback_depth:.1f}×ATR",
                f"VWAP dist {vwap_dist_pct:+.1f}%",
                f"Vol {vol_ratio:.1f}×",
                f"Close pos {close_position:.0%}",
            ],
            
            # VWAP data
            vwap_distance_pct=vwap_dist_pct,
            pullback_depth_atr=pullback_depth,
        )
    
    def _update_impulse_memory(self, symbol: str, buffer, features: dict):
        """Track impulse highs for pullback reference."""
        candles = buffer.candles_1m[-10:]  # Last 10 candles
        if not candles:
            return
        
        recent_high = max(c.high for c in candles)
        trend_5m = features.get('trend_5m', 0)
        
        # Update memory if we see new high in uptrend
        current = self._impulse_memory.get(symbol)
        if current is None or recent_high > current['high'] * 1.002:  # New high
            self._impulse_memory[symbol] = {
                'high': recent_high,
                'timestamp': datetime.now(timezone.utc),
                'trend_score': trend_5m,
            }
    
    def _calculate_edge_score(
        self, trend_5m, trend_1h, pullback_depth, vol_ratio, 
        close_position, vwap_dist_pct
    ) -> float:
        """Calculate base edge score (0-100)."""
        score = 15  # Base for valid pattern
        
        # Trend alignment (up to 30 points)
        if trend_5m > 0.5 and trend_1h > 0:
            score += 30
        elif trend_5m > 0.3:
            score += 20
        elif trend_5m > 0.1:
            score += 10
        
        # Pullback quality (up to 25 points)
        # Ideal pullback is 0.5-0.8 ATR
        if 0.4 <= pullback_depth <= 0.8:
            score += 25
        elif 0.3 <= pullback_depth <= 1.0:
            score += 15
        else:
            score += 5
        
        # Volume on reclaim (up to 20 points)
        if vol_ratio >= 2.0:
            score += 20
        elif vol_ratio >= 1.5:
            score += 15
        elif vol_ratio >= 1.3:
            score += 10
        
        # Buy pressure (up to 15 points)
        if close_position >= 0.8:
            score += 15
        elif close_position >= 0.6:
            score += 10
        elif close_position >= 0.5:
            score += 5
        
        # VWAP proximity bonus (up to 10 points)
        if abs(vwap_dist_pct) < 0.2:
            score += 10
        elif abs(vwap_dist_pct) < 0.5:
            score += 5
        
        return min(score, 100)
    
    def _pullback_quality_score(self, pullback_depth: float, close_pos: float) -> float:
        """Score pullback quality."""
        score = 0
        if 0.4 <= pullback_depth <= 0.8:
            score += 15
        if close_pos >= 0.7:
            score += 10
        return score
    
    def reset(self, symbol: str):
        """Reset state for symbol."""
        self._impulse_memory.pop(symbol, None)
