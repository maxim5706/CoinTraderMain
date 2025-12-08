"""
Gap Fill Strategy - High probability mean reversion.

When price gaps up/down (e.g. after news, listings, large orders),
it tends to "fill the gap" 70-90% of the time within hours/days.

Entry: Price moves toward gap
Exit: Gap filled or stop hit

This is one of the highest win-rate patterns in crypto.
"""

from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection
from core.models import Candle
from typing import Optional
import numpy as np


class GapFillStrategy(BaseStrategy):
    """
    Detect and trade gap fills.
    
    Gap = price jump of 2%+ between candles with no trading in between.
    """
    
    strategy_id = "gap_fill"
    
    # Track detected gaps
    _gaps: dict = {}  # symbol -> {gap_high, gap_low, detected_at, direction}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict
    ) -> Optional[StrategySignal]:
        """Find gaps and trade toward them."""
        
        # Need 5m candles to detect gaps
        candles_5m = getattr(buffer, 'candles_5m', [])
        if len(candles_5m) < 12:  # Need 1 hour
            return None
        
        price = features.get('price', 0)
        if price <= 0:
            return None
        
        # Detect new gaps (2%+ price jump between 5m candles)
        if symbol not in self._gaps:
            self._detect_gaps(symbol, candles_5m)
        
        # Check if we have an active gap
        gap = self._gaps.get(symbol)
        if not gap:
            return None
        
        # Check if price is moving toward gap (setup)
        gap_high = gap['gap_high']
        gap_low = gap['gap_low']
        gap_direction = gap['direction']  # 'up' or 'down'
        
        # For gap up: trade when price pulls back into gap
        if gap_direction == 'up' and gap_low < price < gap_high:
            # Price in gap zone - expect fill
            distance_into_gap = (price - gap_low) / (gap_high - gap_low)
            
            # Better entry = deeper into gap
            if distance_into_gap < 0.3:  # Only entered 30% into gap
                return None
            
            # Check momentum toward gap fill
            trend_5m = features.get('trend_5m', 0)
            if trend_5m > 0:  # Moving away from gap, not toward
                return None
            
            # Calculate score
            score = 60  # Base for gap fill
            score += int(distance_into_gap * 20)  # +0-20 for depth
            score += min(15, abs(trend_5m) * 3)  # +0-15 for momentum
            
            # Volume confirmation
            vol_spike = features.get('vol_spike_5m', 0)
            if vol_spike > 1.5:
                score += 10
            
            return StrategySignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                direction=SignalDirection.LONG,
                edge_score_base=min(95, score),
                trend_score=50 - abs(trend_5m) * 10,
                setup_quality=distance_into_gap,
                reasons=[
                    f"gap_fill_up",
                    f"in_gap_{distance_into_gap:.1%}",
                    f"momentum_down"
                ],
                is_valid=True
            )
        
        # For gap down: trade when price bounces back up
        elif gap_direction == 'down' and gap_low < price < gap_high:
            # Price in gap zone - expect fill upward
            distance_into_gap = (gap_high - price) / (gap_high - gap_low)
            
            if distance_into_gap < 0.3:
                return None
            
            # Check momentum toward gap fill
            trend_5m = features.get('trend_5m', 0)
            if trend_5m < 0:  # Moving away from gap
                return None
            
            score = 60
            score += int(distance_into_gap * 20)
            score += min(15, abs(trend_5m) * 3)
            
            vol_spike = features.get('vol_spike_5m', 0)
            if vol_spike > 1.5:
                score += 10
            
            return StrategySignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                direction=SignalDirection.LONG,
                edge_score_base=min(95, score),
                trend_score=50 + trend_5m * 10,
                setup_quality=distance_into_gap,
                reasons=[
                    f"gap_fill_down",
                    f"in_gap_{distance_into_gap:.1%}",
                    f"momentum_up"
                ],
                is_valid=True
            )
        
        return None
    
    def _detect_gaps(self, symbol: str, candles_5m: list):
        """Detect price gaps in recent candles."""
        if len(candles_5m) < 3:
            return
        
        # Look at last 12 candles (1 hour) for gaps
        for i in range(len(candles_5m) - 2, max(0, len(candles_5m) - 13), -1):
            c1 = candles_5m[i]
            c2 = candles_5m[i + 1]
            
            # Gap up: c2.low > c1.high (no overlap)
            if c2.low > c1.high:
                gap_pct = (c2.low / c1.high - 1) * 100
                if gap_pct >= 2.0:  # 2%+ gap
                    self._gaps[symbol] = {
                        'gap_high': c2.low,
                        'gap_low': c1.high,
                        'direction': 'up',
                        'size_pct': gap_pct,
                        'detected_at': c2.timestamp
                    }
                    return
            
            # Gap down: c2.high < c1.low
            elif c2.high < c1.low:
                gap_pct = (c1.low / c2.high - 1) * 100
                if gap_pct >= 2.0:
                    self._gaps[symbol] = {
                        'gap_high': c1.low,
                        'gap_low': c2.high,
                        'direction': 'down',
                        'size_pct': gap_pct,
                        'detected_at': c2.timestamp
                    }
                    return
    
    def reset(self, symbol: str):
        """Clear gap memory."""
        self._gaps.pop(symbol, None)
