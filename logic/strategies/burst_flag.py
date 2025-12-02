"""
Burst Flag Strategy - Original momentum breakout pattern.

Detects: Burst → Impulse → Flag → Breakout

This wraps the existing strategy.py logic into the new architecture.
"""

from typing import Optional
from datetime import datetime, timezone

from .base import BaseStrategy, StrategySignal, SignalDirection
from core.config import settings


class BurstFlagStrategy(BaseStrategy):
    """
    Bull flag breakout after impulse move.
    
    Pattern:
    1. Burst: Volume + range spike detected
    2. Impulse: Strong directional move (3%+ in crypto)
    3. Flag: Consolidation/pullback (30-60% retrace)
    4. Breakout: Price breaks above flag high with volume
    
    Edge score based on:
    - Impulse strength (size, green candles)
    - Flag quality (retrace depth, duration, volume decay)
    - Breakout quality (volume confirmation)
    """
    
    strategy_id = "burst_flag"
    
    def __init__(self):
        # Delegate to existing strategy for now
        from logic.strategy import BurstFlagStrategy as LegacyStrategy
        self._strategy = LegacyStrategy()
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Analyze for burst-flag pattern.
        
        Wraps existing strategy and converts to StrategySignal.
        """
        if buffer is None:
            return None
        
        # Use existing strategy analysis
        from core.models import Signal, SignalType
        signal = self._strategy.analyze(symbol, buffer)
        
        if signal is None:
            return None
        
        # Only emit for breakout signals
        if signal.type not in [SignalType.FLAG_BREAKOUT, SignalType.FAST_BREAKOUT]:
            return None
        
        # Convert to StrategySignal
        impulse = signal.impulse
        flag = signal.flag
        
        # Calculate base edge score
        edge_score = self._calculate_edge_score(impulse, flag, features, buffer)
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=edge_score,
            
            # Score components
            trend_score=self._trend_score(features),
            volume_score=self._volume_score(impulse, flag),
            pattern_score=self._pattern_score(impulse, flag),
            timing_score=0.0,  # Not used for burst_flag
            
            # Price levels
            entry_price=signal.price,
            stop_price=signal.stop_price,
            tp1_price=signal.tp1_price,
            tp2_price=signal.tp2_price,
            
            # Geometry
            risk_pct=abs(signal.price - signal.stop_price) / signal.price * 100 if signal.price > 0 else 0,
            rr_ratio=getattr(signal, 'rr_ratio', 0.0),
            
            # Context
            reason=signal.reason,
            reasons=[signal.reason] if signal.reason else [],
            
            # Pattern data
            impulse_pct=impulse.pct_move if impulse else 0.0,
            flag_retrace_pct=flag.retrace_pct if flag else 0.0,
        )
    
    def _calculate_edge_score(self, impulse, flag, features: dict, buffer) -> float:
        """Calculate base edge score (0-100)."""
        score = 10  # Base for having a valid pattern
        
        # Impulse strength (up to 30 points)
        if impulse:
            if impulse.pct_move >= 5.0:
                score += 15
            elif impulse.pct_move >= 3.0:
                score += 10
            elif impulse.pct_move >= 2.0:
                score += 5
            
            if impulse.green_candles >= 5:
                score += 10
            elif impulse.green_candles >= 3:
                score += 5
            
            if impulse.avg_volume > 2.0:
                score += 5
        
        # Flag quality (up to 25 points)
        if flag:
            # Ideal retrace is 30-50%
            if 0.3 <= flag.retrace_pct <= 0.5:
                score += 15
            elif 0.2 <= flag.retrace_pct <= 0.6:
                score += 10
            elif flag.retrace_pct < 0.7:
                score += 5
            
            # Volume decay during flag
            if hasattr(flag, 'avg_volume') and impulse:
                if flag.avg_volume < impulse.avg_volume * 0.5:
                    score += 10
                elif flag.avg_volume < impulse.avg_volume * 0.7:
                    score += 5
        
        # Trend alignment (up to 20 points)
        trend_5m = features.get('trend_5m', 0)
        if trend_5m > 0.5:
            score += 15
        elif trend_5m > 0.2:
            score += 10
        elif trend_5m > 0:
            score += 5
        
        # Volume confirmation (up to 15 points)
        vol_ratio = features.get('vol_ratio', 1.0)
        if vol_ratio >= 3.0:
            score += 15
        elif vol_ratio >= 2.0:
            score += 10
        elif vol_ratio >= 1.5:
            score += 5
        
        # VWAP position (up to 10 points)
        vwap_pct = features.get('vwap_pct', 0)
        if 0 < vwap_pct < 0.5:  # Above VWAP but not extended
            score += 10
        elif vwap_pct < 1.0:
            score += 5
        
        return min(score, 100)
    
    def _trend_score(self, features: dict) -> float:
        """Extract trend score component."""
        trend = features.get('trend_5m', 0)
        if trend > 0.5:
            return 20
        elif trend > 0.2:
            return 15
        elif trend > 0:
            return 10
        return 0
    
    def _volume_score(self, impulse, flag) -> float:
        """Extract volume score component."""
        score = 0
        if impulse and impulse.avg_volume > 2.0:
            score += 10
        if flag and impulse:
            if hasattr(flag, 'avg_volume') and flag.avg_volume < impulse.avg_volume * 0.5:
                score += 10
        return score
    
    def _pattern_score(self, impulse, flag) -> float:
        """Extract pattern quality score component."""
        score = 0
        if impulse:
            if impulse.pct_move >= 3.0:
                score += 15
            if impulse.green_candles >= 4:
                score += 10
        if flag and 0.3 <= flag.retrace_pct <= 0.5:
            score += 15
        return score
    
    def reset(self, symbol: str):
        """Reset state for symbol."""
        self._strategy.reset(symbol)
