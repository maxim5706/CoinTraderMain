"""
RSI Momentum Strategy - Mean-reversion into momentum.

Catches bounces that turn into trends (spec 5.1):
- RSI exits oversold
- Fast bullish cross
- Momentum continuation

This catches coins that dip then rip (common in crypto).
"""

from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection
from typing import Optional
import numpy as np


class RSIMomentumStrategy(BaseStrategy):
    """
    RSI reset with momentum continuation.
    
    Entry: RSI exits oversold + price above VWAP + momentum building
    Exit: RSI stalls or momentum fades
    """
    
    strategy_id = "rsi_momentum"
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict
    ) -> Optional[StrategySignal]:
        """Find RSI reset plays."""
        
        candles_5m = getattr(buffer, 'candles_5m', [])
        if len(candles_5m) < 30:  # Need 2.5 hours for RSI
            return None
        
        price = features.get('price', 0)
        if price <= 0:
            return None
        
        # Calculate RSI(14) on 5m candles
        closes = np.array([c.close for c in candles_5m[-30:]])
        rsi = self._calculate_rsi(closes, period=14)
        
        if rsi is None:
            return None
        
        # Get previous RSI for cross detection
        closes_prev = np.array([c.close for c in candles_5m[-31:-1]])
        rsi_prev = self._calculate_rsi(closes_prev, period=14)
        
        if rsi_prev is None:
            return None
        
        # === PATTERN: RSI RESET ===
        # Was oversold (<35), now crossing up
        OVERSOLD = 35
        CROSSOVER = 40
        
        was_oversold = rsi_prev < OVERSOLD
        is_crossing_up = rsi > CROSSOVER and rsi > rsi_prev
        
        if not (was_oversold and is_crossing_up):
            return None  # No RSI reset pattern
        
        # === CONFIRMATION: Price above VWAP ===
        vwap_distance = features.get('vwap_distance', -999)
        if vwap_distance < 0:
            return None  # Below VWAP = still weak
        
        # === CONFIRMATION: Momentum building ===
        trend_5m = features.get('trend_5m', 0)
        trend_15m = features.get('trend_15m', 0)
        
        if trend_5m < 0.3:  # Need positive momentum
            return None
        
        # === CONFIRMATION: Volume ===
        vol_spike = features.get('vol_spike_5m', 1.0)
        if vol_spike < 1.2:
            return None
        
        # === SCORING ===
        score = 60  # Base for RSI reset
        
        # RSI momentum (up to 20 points)
        rsi_move = rsi - rsi_prev
        if rsi_move > 15:
            score += 20
        elif rsi_move > 10:
            score += 15
        else:
            score += 10
        
        # VWAP strength (up to 15 points)
        if vwap_distance > 0.5:
            score += 15
        elif vwap_distance > 0.2:
            score += 10
        else:
            score += 5
        
        # Momentum confirmation (up to 10 points)
        if trend_5m > 1.0 and trend_15m > 0.5:
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
            trend_score=50 + int(trend_5m * 10),
            setup_quality=rsi_move / 20,  # Quality based on RSI momentum
            reasons=[
                f"rsi_reset_{rsi_prev:.0f}→{rsi:.0f}",
                f"above_vwap_{vwap_distance:.1%}",
                f"momentum_{trend_5m:.1f}%",
                f"vol_{vol_spike:.1f}x"
            ],
            is_valid=True
        )
    
    def _calculate_rsi(self, closes: np.ndarray, period: int = 14) -> Optional[float]:
        """Calculate RSI."""
        if len(closes) < period + 1:
            return None
        
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def reset(self, symbol: str):
        """Stateless."""
        pass
