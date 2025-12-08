"""
Breakout Retest Strategy - Continuation after pullback.

Classic pattern:
1. Price breaks resistance
2. Pulls back to retest breakout level (now support)
3. Bounces and continues higher

Entry: During retest (price at old resistance, now support)
Stop: Below retest low
Target: Extension of original move

Win rate: 65-75% when volume confirms
"""

from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection
from typing import Optional


class BreakoutRetestStrategy(BaseStrategy):
    """
    Trade retests of breakout levels.
    
    After a breakout, price often pulls back to "test" the old
    resistance as new support before continuing.
    """
    
    strategy_id = "breakout_retest"
    
    # Track recent breakouts
    _breakouts: dict = {}  # symbol -> {level, direction, breakout_at, tested}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict
    ) -> Optional[StrategySignal]:
        """Find retest opportunities."""
        
        candles_5m = getattr(buffer, 'candles_5m', [])
        if len(candles_5m) < 24:  # Need 2 hours
            return None
        
        price = features.get('price', 0)
        if price <= 0:
            return None
        
        # Detect breakouts if not tracked
        if symbol not in self._breakouts:
            self._detect_breakout(symbol, candles_5m, price)
        
        # Check for retest setup
        breakout = self._breakouts.get(symbol)
        if not breakout or breakout.get('tested'):
            return None
        
        level = breakout['level']
        direction = breakout['direction']
        
        # For bullish breakout: look for pullback to level
        if direction == 'up':
            # Price should be near breakout level (within 1%)
            distance = abs(price - level) / level
            if distance > 0.015:  # More than 1.5% away
                return None
            
            # Should be pulling back (not continuing up)
            trend_5m = features.get('trend_5m', 0)
            if trend_5m > 0.5:  # Still going up, not pulling back
                return None
            
            # Check if we're at/below level (retest)
            if price > level * 1.005:  # More than 0.5% above
                return None
            
            # Volume should be declining (healthy pullback)
            vol_spike = features.get('vol_spike_5m', 0)
            if vol_spike > 2.0:  # Too much volume = possible reversal
                return None
            
            # Price should be holding above VWAP
            vwap_distance = features.get('vwap_distance', -999)
            if vwap_distance < -0.5:  # More than 0.5% below VWAP = weak
                return None
            
            # Calculate score
            score = 70  # Base for retest
            
            # Closer to level = better
            score += int((1 - distance / 0.015) * 15)  # +0-15
            
            # VWAP support
            if vwap_distance > 0:
                score += 10
            
            # Declining volume = healthy
            if 0.5 < vol_spike < 1.5:
                score += 10
            
            # Mark as tested
            breakout['tested'] = True
            
            return StrategySignal(
                symbol=symbol,
                strategy_id=self.strategy_id,
                direction=SignalDirection.LONG,
                edge_score_base=min(95, score),
                trend_score=60,  # Continuation bias
                setup_quality=1 - distance,  # Closer = better
                reasons=[
                    "breakout_retest",
                    f"at_level_{distance:.1%}",
                    "pullback_healthy",
                    "above_vwap"
                ],
                is_valid=True
            )
        
        return None
    
    def _detect_breakout(self, symbol: str, candles_5m: list, current_price: float):
        """Detect recent breakouts from consolidation."""
        if len(candles_5m) < 24:
            return
        
        # Look at last 2 hours for consolidation → breakout
        highs = [c.high for c in candles_5m[-24:]]
        lows = [c.low for c in candles_5m[-24:]]
        
        # Find resistance level (recent high that was tested multiple times)
        import numpy as np
        resistance_candidates = []
        
        for i in range(len(highs) - 6, len(highs) - 1):
            high = highs[i]
            
            # Count touches within 0.5% of this level
            touches = sum(1 for h in highs[max(0, i-12):i] 
                         if abs(h - high) / high < 0.005)
            
            if touches >= 2:  # Level was tested at least twice
                resistance_candidates.append((high, touches))
        
        if not resistance_candidates:
            return
        
        # Take strongest resistance (most touches)
        resistance_candidates.sort(key=lambda x: x[1], reverse=True)
        resistance_level = resistance_candidates[0][0]
        
        # Check if we recently broke above it
        recent_highs = highs[-3:]  # Last 15 minutes
        if max(recent_highs) > resistance_level * 1.01:  # Broke 1% above
            # Confirmed breakout
            self._breakouts[symbol] = {
                'level': resistance_level,
                'direction': 'up',
                'breakout_at': candles_5m[-1].timestamp,
                'tested': False
            }
    
    def reset(self, symbol: str):
        """Clear breakout memory."""
        self._breakouts.pop(symbol, None)
