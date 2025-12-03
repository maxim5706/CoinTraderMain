"""
Support Bounce Strategy.

Identifies key support levels and enters on confirmed bounces.
"Buy at levels where buyers stepped in before."
"""

from typing import Optional, List, Tuple
from datetime import datetime, timezone
from collections import defaultdict

from .base import BaseStrategy, StrategySignal, SignalDirection
from core.config import settings


class SupportBounceStrategy(BaseStrategy):
    """
    Support level bounce strategy.
    
    Pattern:
    1. Identify key support levels from recent price action
    2. Price approaches support (within 0.5%)
    3. Bounce confirmation: green candle with volume
    4. Entry with tight stop below support
    
    Edge: Support levels represent prior buying interest.
    Multiple touches = stronger level. Tight stop = great R:R.
    """
    
    strategy_id = "support_bounce"
    
    # Track identified levels per symbol
    _level_cache: dict = {}  # symbol -> {supports: [], resistances: [], updated_at}
    
    # Configuration
    LEVEL_TOLERANCE_PCT = 0.005   # 0.5% tolerance for level touches
    MIN_TOUCHES = 2              # Minimum touches to confirm level
    APPROACH_PCT = 0.008         # Within 0.8% of level to trigger
    BOUNCE_CONFIRM_PCT = 0.003   # 0.3% bounce to confirm
    
    def __init__(self):
        self._level_cache = {}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Analyze for support bounce setup.
        """
        if buffer is None or len(buffer.candles_1m) < 60:
            return None
        
        # Use 5m candles for level detection
        candles_5m = buffer.candles_5m
        candles_1m = buffer.candles_1m
        
        if len(candles_5m) < 20:
            return None
        
        current = candles_1m[-1]
        price = current.close
        
        # Identify or update support/resistance levels
        levels = self._identify_levels(symbol, candles_5m, candles_1m)
        
        if not levels['supports']:
            return None
        
        # Check if price is near a support level
        nearest_support = self._find_nearest_support(price, levels['supports'])
        
        if nearest_support is None:
            return None
        
        support_price, touches, strength = nearest_support
        
        # Check for bounce confirmation
        bounce = self._check_bounce(price, support_price, candles_1m, features)
        
        if bounce is None:
            return None
        
        # Calculate levels
        stop_price = support_price * (1 - 0.01)  # 1% below support
        risk = price - stop_price
        tp1_price = price + (risk * 2)  # 2:1 R:R
        tp2_price = price + (risk * 3)  # 3:1 R:R
        
        risk_pct = abs(price - stop_price) / price * 100
        rr_ratio = 2.0  # By design
        
        # Score the setup
        score = self._score_setup(touches, strength, bounce, features)
        
        if score < 65:
            return None
        
        reason = (
            f"Support ${support_price:.4f} ({touches} touches), "
            f"bounce: {bounce['bounce_pct']:.2f}%"
        )
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=score,
            trend_score=50 + features.get('trend_5m', 0) * 10,
            volume_score=min(100, bounce['vol_ratio'] * 40),
            pattern_score=min(100, touches * 20 + strength * 10),
            timing_score=bounce.get('timing_score', 50),
            entry_price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            risk_pct=risk_pct,
            rr_ratio=rr_ratio,
            reason=reason,
            reasons=[
                f"{touches}_touches",
                f"strength_{strength:.0f}",
                f"bounce_{bounce['bounce_pct']:.1f}%"
            ],
        )
    
    def _identify_levels(self, symbol: str, candles_5m: list, candles_1m: list) -> dict:
        """Identify support and resistance levels from price action."""
        # Check cache (update every 5 minutes)
        cached = self._level_cache.get(symbol)
        if cached:
            age = (datetime.now(timezone.utc) - cached['updated_at']).total_seconds()
            if age < 300:  # 5 minute cache
                return cached
        
        supports = []
        resistances = []
        
        # Combine 5m and recent 1m for level detection
        all_candles = candles_5m[-50:]  # Last 50 5m candles
        
        if len(all_candles) < 20:
            return {'supports': [], 'resistances': []}
        
        # Find swing lows (support) and swing highs (resistance)
        for i in range(2, len(all_candles) - 2):
            candle = all_candles[i]
            
            # Swing low (support)
            if (candle.low < all_candles[i-1].low and 
                candle.low < all_candles[i-2].low and
                candle.low < all_candles[i+1].low and 
                candle.low < all_candles[i+2].low):
                supports.append(candle.low)
            
            # Swing high (resistance)
            if (candle.high > all_candles[i-1].high and 
                candle.high > all_candles[i-2].high and
                candle.high > all_candles[i+1].high and 
                candle.high > all_candles[i+2].high):
                resistances.append(candle.high)
        
        # Cluster nearby levels
        supports = self._cluster_levels(supports)
        resistances = self._cluster_levels(resistances)
        
        # Count touches for each level
        support_data = []
        for level in supports:
            touches, strength = self._count_touches(level, all_candles)
            if touches >= self.MIN_TOUCHES:
                support_data.append((level, touches, strength))
        
        resistance_data = []
        for level in resistances:
            touches, strength = self._count_touches(level, all_candles)
            if touches >= self.MIN_TOUCHES:
                resistance_data.append((level, touches, strength))
        
        # Sort by strength
        support_data.sort(key=lambda x: x[2], reverse=True)
        resistance_data.sort(key=lambda x: x[2], reverse=True)
        
        result = {
            'supports': support_data[:5],  # Top 5 supports
            'resistances': resistance_data[:5],  # Top 5 resistances
            'updated_at': datetime.now(timezone.utc)
        }
        
        self._level_cache[symbol] = result
        return result
    
    def _cluster_levels(self, levels: List[float], tolerance: float = 0.005) -> List[float]:
        """Cluster nearby levels into single representative levels."""
        if not levels:
            return []
        
        levels = sorted(levels)
        clustered = []
        current_cluster = [levels[0]]
        
        for level in levels[1:]:
            if current_cluster and abs(level - current_cluster[-1]) / current_cluster[-1] < tolerance:
                current_cluster.append(level)
            else:
                # Average the cluster
                clustered.append(sum(current_cluster) / len(current_cluster))
                current_cluster = [level]
        
        if current_cluster:
            clustered.append(sum(current_cluster) / len(current_cluster))
        
        return clustered
    
    def _count_touches(self, level: float, candles: list) -> Tuple[int, float]:
        """Count how many times price touched a level and calculate strength."""
        touches = 0
        bounces = 0
        
        tolerance = level * self.LEVEL_TOLERANCE_PCT
        
        for i, candle in enumerate(candles):
            # Check if candle touched the level
            if candle.low <= level + tolerance and candle.low >= level - tolerance:
                touches += 1
                
                # Check if it bounced (closed above)
                if candle.close > level + tolerance:
                    bounces += 1
        
        # Strength = touches + bonus for bounces
        strength = touches + (bounces * 0.5)
        
        return touches, strength
    
    def _find_nearest_support(self, price: float, supports: list) -> Optional[Tuple[float, int, float]]:
        """Find nearest support level below current price."""
        for support_price, touches, strength in supports:
            # Support must be below current price
            if support_price >= price:
                continue
            
            # Check if price is approaching (within APPROACH_PCT)
            distance_pct = (price - support_price) / support_price
            if distance_pct <= self.APPROACH_PCT:
                return (support_price, touches, strength)
        
        return None
    
    def _check_bounce(self, price: float, support: float, candles_1m: list, features: dict) -> Optional[dict]:
        """Check if there's a confirmed bounce off support."""
        if len(candles_1m) < 5:
            return None
        
        recent = candles_1m[-5:]
        current = recent[-1]
        
        # Check if recent candles touched support
        touched_support = False
        lowest_price = min(c.low for c in recent)
        
        tolerance = support * self.LEVEL_TOLERANCE_PCT
        if lowest_price <= support + tolerance:
            touched_support = True
        
        if not touched_support:
            return None
        
        # Check for bounce (price moved up from low)
        bounce_pct = (price - lowest_price) / lowest_price * 100
        
        if bounce_pct < self.BOUNCE_CONFIRM_PCT * 100:
            return None
        
        # Check candle color (should be green for confirmation)
        if current.close <= current.open:
            # Red candle - not confirmed yet
            # Unless previous was green and strong
            if recent[-2].close <= recent[-2].open:
                return None
        
        # Volume check
        avg_vol = sum(c.volume for c in candles_1m[-20:]) / 20
        bounce_vol = sum(c.volume for c in recent[-3:]) / 3
        vol_ratio = bounce_vol / avg_vol if avg_vol > 0 else 1.0
        
        # Need some volume on bounce
        if vol_ratio < 0.8:
            return None
        
        # Timing score - fresher bounce is better
        timing_score = min(80, 50 + bounce_pct * 10)
        
        return {
            'bounce_pct': bounce_pct,
            'vol_ratio': vol_ratio,
            'timing_score': timing_score,
        }
    
    def _score_setup(self, touches: int, strength: float, bounce: dict, features: dict) -> float:
        """Score the support bounce setup."""
        score = 50  # Base
        
        # Level strength (more touches = stronger)
        if touches >= 4:
            score += 20
        elif touches >= 3:
            score += 15
        elif touches >= 2:
            score += 10
        
        # Overall strength score
        if strength >= 5:
            score += 15
        elif strength >= 3:
            score += 10
        elif strength >= 2:
            score += 5
        
        # Bounce quality
        bounce_pct = bounce['bounce_pct']
        if 0.3 <= bounce_pct <= 1.0:
            score += 15  # Perfect bounce size
        elif 0.2 <= bounce_pct <= 1.5:
            score += 10
        elif bounce_pct > 1.5:
            score += 5  # Might be chasing
        
        # Volume on bounce
        vol_ratio = bounce['vol_ratio']
        if vol_ratio >= 1.5:
            score += 10
        elif vol_ratio >= 1.2:
            score += 5
        
        # Trend alignment (prefer uptrend)
        trend_5m = features.get('trend_5m', 0)
        if trend_5m > 0.3:
            score += 5
        elif trend_5m < -0.5:
            score -= 10  # Penalty for downtrend
        
        return min(100, max(0, score))
    
    def reset(self, symbol: str):
        """Reset state for symbol."""
        self._level_cache.pop(symbol, None)
