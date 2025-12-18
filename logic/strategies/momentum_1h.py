"""
1H Momentum Strategy - Catches pure momentum movers.

This is THE strategy for catching coins like LRDS +63%, ZEC +23%, MAGIC +14%.

Implements momentum spec 1.1-1.3:
- 1H ROC threshold
- Multi-timeframe acceleration
- MA slope/crossover
- Volume confirmation
- ATR shock detection

Entry: Raw momentum + acceleration + volume
Exit: ATR trailing stop or momentum decay
"""

from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection
from typing import Optional
import numpy as np


class Momentum1HStrategy(BaseStrategy):
    """
    Pure 1H momentum catcher.
    
    Enters on:
    - 1H ROC > 3% (adjustable)
    - Acceleration across timeframes
    - Volume spike confirmation
    - ATR expansion (volatility shock)
    
    This catches the actual market movers, not just patterns.
    """
    
    strategy_id = "momentum_1h"
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict
    ) -> Optional[StrategySignal]:
        """
        Find pure momentum plays.
        
        This is the "just ride the wave" strategy.
        """
        
        # Extract momentum features
        trend_1h = features.get('trend_1h', 0)
        trend_15m = features.get('trend_15m', 0)
        trend_5m = features.get('trend_5m', 0)
        
        price = features.get('price', 0)
        if price <= 0:
            return None
        
        # === GATE 1: 1H MOMENTUM THRESHOLD ===
        # Need strong 1H move (this catches LRDS at +9%, ZEC at +23%)
        MIN_1H_MOMENTUM = 3.0  # 3% minimum (configurable)
        
        if trend_1h < MIN_1H_MOMENTUM:
            return None  # Not enough 1H momentum
        
        # === GATE 2: ACCELERATION (Multi-timeframe stacked) ===
        # 1H surge should show up in shorter timeframes too
        # This filters out "already done" moves
        
        # Check if momentum is accelerating (15m > 5m implies building)
        is_accelerating = trend_15m > trend_5m * 0.5
        
        if trend_15m < 0.5:  # 15m momentum too weak
            return None
        
        if not is_accelerating:
            return None  # Momentum decaying, not building
        
        # === GATE 3: VOLUME CONFIRMATION ===
        vol_spike = features.get('vol_spike_5m', 1.0)
        
        if vol_spike < 1.3:  # Need some volume
            return None
        
        # === GATE 4: ATR SHOCK (Volatility expansion) ===
        # Most 1H gainers are volatility shocks
        candles_5m = getattr(buffer, 'candles_5m', [])
        
        atr_shock = False
        if len(candles_5m) >= 20:
            # Calculate recent ATR vs baseline
            recent_ranges = [c.high - c.low for c in candles_5m[-5:]]
            baseline_ranges = [c.high - c.low for c in candles_5m[-20:-5]]
            
            if baseline_ranges:
                recent_atr = np.mean(recent_ranges)
                baseline_atr = np.mean(baseline_ranges)
                
                if baseline_atr > 0:
                    atr_ratio = recent_atr / baseline_atr
                    atr_shock = atr_ratio > 1.3  # 30% ATR expansion
        
        # === GATE 5: NOT EXHAUSTED ===
        # Don't chase if already at extremes
        # Check if price is >2 std devs from recent mean (overextended)
        if len(candles_5m) >= 20:
            recent_closes = [c.close for c in candles_5m[-20:]]
            mean_price = np.mean(recent_closes)
            std_price = np.std(recent_closes)
            
            if std_price > 0:
                z_score = (price - mean_price) / std_price
                if z_score > 2.5:  # Too extended
                    return None
        
        # === SCORING ===
        score = 50  # Base for valid momentum
        
        # 1H momentum strength (up to 30 points)
        if trend_1h >= 10.0:
            score += 30
        elif trend_1h >= 7.0:
            score += 25
        elif trend_1h >= 5.0:
            score += 20
        else:
            score += int(trend_1h * 3)  # Scale with momentum
        
        # Acceleration bonus (up to 15 points)
        accel_ratio = trend_15m / max(0.1, trend_5m)
        if accel_ratio > 1.5:
            score += 15
        elif accel_ratio > 1.2:
            score += 10
        else:
            score += 5
        
        # Volume confirmation (up to 15 points)
        if vol_spike > 3.0:
            score += 15
        elif vol_spike > 2.0:
            score += 10
        else:
            score += 5
        
        # ATR shock bonus (up to 10 points)
        if atr_shock:
            score += 10
        
        # Market alignment (up to 10 points)
        btc_trend = market_context.get('btc_trend_1h', 0)
        if btc_trend > 0:  # BTC also up
            score += 10
        elif btc_trend < -2.0:  # BTC down, risky
            score -= 10
        
        # === BUILD SIGNAL ===
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=min(100, score),
            trend_score=60 + int(trend_1h),  # Trend-following bias
            entry_price=price,
            stop_price=price * 0.965,  # 3.5% stop
            reasons=[
                f"1h_momentum_{trend_1h:.1f}%",
                f"15m_{trend_15m:.1f}%",
                f"vol_{vol_spike:.1f}x",
                "accelerating" if is_accelerating else "",
                "atr_shock" if atr_shock else ""
            ]
        )
    
    def reset(self, symbol: str):
        """Stateless strategy - no reset needed."""
        pass
