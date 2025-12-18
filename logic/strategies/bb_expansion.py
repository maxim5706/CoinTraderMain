"""
Bollinger Band Expansion Strategy - Volatility breakouts (spec 2.2).

Catches volatility expansion events:
- Price breaks upper BB
- Band width expanding (not squeezing)
- Volume confirmation

This catches explosive moves like ZEC +23%, TROLL +22%.
"""

from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection
from typing import Optional
import numpy as np


class BBExpansionStrategy(BaseStrategy):
    """
    Bollinger Band expansion/ride.
    
    Entry: Close > BB_upper AND BB width expanding
    Exit: Close back inside band or width contraction
    """
    
    strategy_id = "bb_expansion"
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict
    ) -> Optional[StrategySignal]:
        """Find BB expansion plays."""
        
        candles_5m = getattr(buffer, 'candles_5m', [])
        if len(candles_5m) < 40:  # Need 3+ hours
            return None
        
        price = features.get('price', 0)
        if price <= 0:
            return None
        
        # Calculate Bollinger Bands (20, 2)
        closes = np.array([c.close for c in candles_5m[-40:]])
        
        bb_upper, bb_middle, bb_lower, bb_width = self._calculate_bb(closes, period=20, std_dev=2)
        
        if bb_upper is None:
            return None
        
        # Previous BB width (for expansion detection)
        closes_prev = closes[:-1]
        _, _, _, bb_width_prev = self._calculate_bb(closes_prev, period=20, std_dev=2)
        
        if bb_width_prev is None:
            return None
        
        # === PATTERN: BB BREAKOUT + EXPANSION ===
        # Price above upper band
        is_above_upper = price > bb_upper
        
        # Band width expanding (not squeezing)
        is_expanding = bb_width > bb_width_prev
        
        if not (is_above_upper and is_expanding):
            return None
        
        # === CONFIRMATION: Distance above band ===
        # Too far = overextended, too close = weak break
        distance_pct = (price - bb_upper) / bb_upper * 100
        
        if distance_pct > 3.0:  # More than 3% above band = overextended
            return None
        
        if distance_pct < 0.1:  # Barely above = weak
            return None
        
        # === CONFIRMATION: Momentum ===
        trend_5m = features.get('trend_5m', 0)
        trend_15m = features.get('trend_15m', 0)
        
        if trend_5m < 0.3:  # Need upward momentum
            return None
        
        # === CONFIRMATION: Volume ===
        vol_spike = features.get('vol_spike_5m', 1.0)
        if vol_spike < 1.3:
            return None
        
        # === SCORING ===
        score = 65  # Base for BB expansion
        
        # Expansion strength (up to 20 points)
        expansion_ratio = bb_width / max(0.001, bb_width_prev)
        if expansion_ratio > 1.5:
            score += 20
        elif expansion_ratio > 1.3:
            score += 15
        elif expansion_ratio > 1.1:
            score += 10
        else:
            score += 5
        
        # Distance quality (up to 10 points)
        # Optimal: 0.5-1.5% above band
        if 0.5 < distance_pct < 1.5:
            score += 10
        elif distance_pct < 0.5:
            score += 5
        
        # Momentum (up to 10 points)
        if trend_5m > 1.0:
            score += 10
        elif trend_5m > 0.5:
            score += 5
        
        # Volume (up to 5 points)
        if vol_spike > 2.0:
            score += 5
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=min(95, score),
            trend_score=55 + int(trend_5m * 5),
            entry_price=price,
            stop_price=price * 0.965,  # 3.5% stop
            reasons=[
                f"bb_breakout_{distance_pct:.1f}%_above",
                f"expanding_{expansion_ratio:.2f}x",
                f"momentum_{trend_5m:.1f}%",
                f"vol_{vol_spike:.1f}x"
            ]
        )
    
    def _calculate_bb(self, closes: np.ndarray, period: int = 20, std_dev: float = 2.0):
        """Calculate Bollinger Bands."""
        if len(closes) < period:
            return None, None, None, None
        
        # Middle band (SMA)
        middle = np.mean(closes[-period:])
        
        # Standard deviation
        std = np.std(closes[-period:])
        
        # Upper and lower bands
        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)
        
        # Band width (normalized)
        width = (upper - lower) / middle if middle > 0 else 0
        
        return upper, middle, lower, width
    
    def reset(self, symbol: str):
        """Stateless."""
        pass
