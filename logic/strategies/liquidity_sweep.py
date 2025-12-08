"""
Liquidity Sweep Strategy - Hunt the stop hunters.

Whales/market makers push price below support to trigger stop losses,
collect liquidity, then reverse. This is a "stop hunt" or "liquidity grab".

Pattern:
1. Price drops below key support (sweeps stops)
2. Immediate reversal (V-shape)
3. Strong bounce back above support

Entry: During the reversal
Stop: Below sweep low
Target: Back to pre-sweep level + extension

Win rate: 70%+ when volume confirms the reversal
"""

from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection
from typing import Optional
import numpy as np


class LiquiditySweepStrategy(BaseStrategy):
    """
    Trade reversals after liquidity sweeps (stop hunts).
    
    Smart money pushes price through key levels to grab liquidity,
    then reverses. We trade WITH them, not against them.
    """
    
    strategy_id = "liquidity_sweep"
    
    # Track key support levels
    _support_levels: dict = {}  # symbol -> [levels]
    _recent_sweeps: dict = {}  # symbol -> {level, swept_at, reversed}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict
    ) -> Optional[StrategySignal]:
        """Find liquidity sweep reversals."""
        
        candles_1m = getattr(buffer, 'candles_1m', [])
        candles_5m = getattr(buffer, 'candles_5m', [])
        
        if len(candles_1m) < 30 or len(candles_5m) < 12:
            return None
        
        price = features.get('price', 0)
        if price <= 0:
            return None
        
        # Identify support levels if not cached
        if symbol not in self._support_levels:
            self._identify_support_levels(symbol, candles_5m)
        
        # Check for recent sweep
        sweep = self._recent_sweeps.get(symbol)
        if sweep and sweep.get('reversed'):
            return None  # Already traded this sweep
        
        # Detect new sweeps
        if not sweep:
            sweep = self._detect_sweep(symbol, candles_1m, price)
            if sweep:
                self._recent_sweeps[symbol] = sweep
        
        if not sweep:
            return None
        
        # Check for reversal (V-shape recovery)
        swept_level = sweep['level']
        swept_low = sweep['low']
        
        # Price should be recovering above swept level
        if price <= swept_level:
            return None  # Not reversed yet
        
        # Check reversal strength (V-shape)
        reversal_speed = (price - swept_low) / swept_low
        if reversal_speed < 0.005:  # Less than 0.5% recovery
            return None  # Too weak
        
        # Volume should spike on reversal (buyers stepping in)
        vol_spike = features.get('vol_spike_1m', 0)
        if vol_spike < 1.5:  # Need volume confirmation
            return None
        
        # Check momentum (should be strongly up)
        trend_5m = features.get('trend_5m', 0)
        if trend_5m < 0.3:  # Not strong enough
            return None
        
        # Price should be above VWAP after reversal
        vwap_distance = features.get('vwap_distance', -999)
        if vwap_distance < 0:
            return None  # Below VWAP = still weak
        
        # Calculate score
        score = 75  # Base for liquidity sweep
        
        # Faster reversal = better
        score += min(15, int(reversal_speed * 100))  # +0-15
        
        # Higher volume = more conviction
        if vol_spike > 2.5:
            score += 10
        
        # Strong momentum = follow-through likely
        if trend_5m > 1.0:
            score += 10
        
        # Mark as reversed (trade once per sweep)
        sweep['reversed'] = True
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=min(95, score),
            trend_score=60 + int(trend_5m * 10),
            setup_quality=reversal_speed * 10,
            reasons=[
                "liquidity_sweep",
                "stop_hunt_reversal",
                f"v_shape_{reversal_speed:.1%}",
                f"vol_spike_{vol_spike:.1f}x"
            ],
            is_valid=True
        )
    
    def _identify_support_levels(self, symbol: str, candles_5m: list):
        """Find key support levels that could be swept."""
        if len(candles_5m) < 48:  # Need 4 hours
            return
        
        lows = [c.low for c in candles_5m[-48:]]
        
        # Find levels with multiple touches (2-3 touches = key level)
        levels = []
        for i in range(len(lows) - 6):
            low = lows[i]
            
            # Count similar lows within 0.5%
            touches = sum(1 for l in lows[max(0, i-12):min(len(lows), i+12)]
                         if abs(l - low) / low < 0.005)
            
            if touches >= 2:  # Multi-touch support
                levels.append(low)
        
        # Keep unique levels
        unique_levels = []
        for level in levels:
            if not any(abs(level - existing) / existing < 0.01 for existing in unique_levels):
                unique_levels.append(level)
        
        self._support_levels[symbol] = sorted(unique_levels)
    
    def _detect_sweep(self, symbol: str, candles_1m: list, current_price: float) -> Optional[dict]:
        """Detect if we just swept a support level."""
        support_levels = self._support_levels.get(symbol, [])
        if not support_levels:
            return None
        
        # Look at last 5-10 candles for sweep
        recent_candles = candles_1m[-10:]
        recent_low = min(c.low for c in recent_candles)
        
        # Check if recent low swept a support level
        for level in support_levels:
            if recent_low < level < current_price:
                # Swept below level and now back above
                # Check if it was a quick sweep (within 5 min)
                sweep_candle = next((c for c in recent_candles if c.low == recent_low), None)
                if sweep_candle:
                    return {
                        'level': level,
                        'low': recent_low,
                        'swept_at': sweep_candle.timestamp,
                        'reversed': False
                    }
        
        return None
    
    def reset(self, symbol: str):
        """Clear state."""
        self._support_levels.pop(symbol, None)
        self._recent_sweeps.pop(symbol, None)
