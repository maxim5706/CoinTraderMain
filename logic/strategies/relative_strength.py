"""
Relative Strength Strategy.

Finds altcoins outperforming BTC and enters on pullback.
"Buy the strongest horses when they dip."
"""

from typing import Optional
from datetime import datetime, timezone

from .base import BaseStrategy, StrategySignal, SignalDirection
from core.config import settings


class RelativeStrengthStrategy(BaseStrategy):
    """
    Relative strength rotation strategy.
    
    Pattern:
    1. Altcoin outperforming BTC over 1h/4h timeframe
    2. RS ratio > 1.02 (alt gaining 2%+ vs BTC)
    3. Pullback opportunity (5m trend slightly negative or flat)
    4. Enter with trend, stop on RS breakdown
    
    Edge: Strong coins stay strong. Buying dips in outperformers
    gives better R:R than chasing or buying laggards.
    """
    
    strategy_id = "relative_strength"
    
    # Track BTC price for RS calculation
    _btc_prices: dict = {}  # timestamp -> price
    _rs_cache: dict = {}    # symbol -> {rs_1h, rs_4h, last_update}
    
    # Configuration
    MIN_RS_1H = 1.015       # Alt must be +1.5% vs BTC over 1h
    MIN_RS_4H = 1.02        # Alt must be +2% vs BTC over 4h
    MAX_CHASE_PCT = 0.5     # Don't enter if 5m trend > 0.5% (chasing)
    
    def __init__(self):
        self._btc_prices = {}
        self._rs_cache = {}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Analyze for relative strength setup.
        """
        if buffer is None or len(buffer.candles_1m) < 30:
            return None
        
        # Skip BTC itself
        if symbol.startswith("BTC"):
            return None
        
        # Get BTC data from market context
        btc_regime = market_context.get('btc_regime', 'normal')
        
        # Don't trade RS in bearish BTC regime - everything drops
        if btc_regime == 'bearish':
            return None
        
        # Calculate relative strength
        rs_data = self._calculate_rs(symbol, buffer, market_context)
        if rs_data is None:
            return None
        
        rs_1h = rs_data['rs_1h']
        rs_4h = rs_data['rs_4h']
        
        # Must show relative strength
        if rs_1h < self.MIN_RS_1H and rs_4h < self.MIN_RS_4H:
            return None
        
        # Check for entry opportunity (pullback, not chase)
        trend_5m = features.get('trend_5m', 0)
        
        # Don't chase pumps
        if trend_5m > self.MAX_CHASE_PCT:
            return None
        
        # Best entry: slight pullback (-0.5% to +0.3%)
        if trend_5m < -1.5:
            # Falling too fast - might be RS breakdown
            return None
        
        candles = buffer.candles_1m
        current = candles[-1]
        price = current.close
        
        # Score the setup
        score = self._score_setup(rs_data, features)
        
        if score < 65:
            return None
        
        # Calculate levels based on ATR
        atr = buffer.atr(14, "1m")
        if atr <= 0:
            atr = price * 0.015  # Fallback 1.5%
        
        stop_price = price - (atr * 2)  # 2 ATR stop
        tp1_price = price + (atr * 3)   # 3 ATR target (1.5 R:R)
        tp2_price = price + (atr * 5)   # 5 ATR extended
        
        risk_pct = abs(price - stop_price) / price * 100
        rr_ratio = (tp1_price - price) / (price - stop_price) if price > stop_price else 0
        
        # Entry bonus for better dip
        entry_quality = "dip" if trend_5m < 0 else "flat"
        
        reason = (
            f"RS 1h: {rs_1h:.2f}, RS 4h: {rs_4h:.2f}, "
            f"entry: {entry_quality} ({trend_5m:+.1f}%)"
        )
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=score,
            trend_score=min(100, rs_1h * 50),  # RS as trend proxy
            volume_score=features.get('vol_ratio', 1.0) * 50,
            pattern_score=min(100, (rs_4h - 1) * 500),  # Reward stronger RS
            timing_score=70 if trend_5m < 0 else 50,
            entry_price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            risk_pct=risk_pct,
            rr_ratio=rr_ratio,
            reason=reason,
            reasons=[
                f"rs_1h_{rs_1h:.2f}",
                f"rs_4h_{rs_4h:.2f}",
                entry_quality
            ],
        )
    
    def _calculate_rs(self, symbol: str, buffer, market_context: dict) -> Optional[dict]:
        """Calculate relative strength vs BTC."""
        # Get altcoin price changes
        candles_1h = getattr(buffer, 'candles_1h', [])
        candles_1m = buffer.candles_1m
        
        if len(candles_1m) < 60:
            return None
        
        current_price = candles_1m[-1].close
        
        # 1h price change (use last 60 1m candles or 1h candles)
        if len(candles_1h) >= 2:
            price_1h_ago = candles_1h[-2].close if len(candles_1h) >= 2 else candles_1h[-1].open
        else:
            price_1h_ago = candles_1m[-60].close if len(candles_1m) >= 60 else candles_1m[0].close
        
        # 4h price change
        if len(candles_1h) >= 5:
            price_4h_ago = candles_1h[-5].close
        elif len(candles_1m) >= 240:
            price_4h_ago = candles_1m[-240].close
        else:
            price_4h_ago = candles_1m[0].close
        
        if price_1h_ago <= 0 or price_4h_ago <= 0:
            return None
        
        alt_change_1h = current_price / price_1h_ago
        alt_change_4h = current_price / price_4h_ago
        
        # Get BTC change from intelligence layer
        from logic.intelligence import intelligence
        btc_ind = intelligence.get_live_indicators("BTC-USD")
        
        # Estimate BTC changes from trend data
        btc_change_1h = 1.0
        btc_change_4h = 1.0
        
        if btc_ind and getattr(btc_ind, 'is_ready', False):
            # Use 5m and 15m trends as proxy for 1h/4h
            btc_trend_5m = getattr(btc_ind, 'trend_5m', 0) / 100
            btc_trend_15m = getattr(btc_ind, 'trend_15m', 0) / 100
            
            # Rough estimate: 5m trend × 12 ≈ 1h, 15m × 16 ≈ 4h
            btc_change_1h = 1 + (btc_trend_5m * 2)  # More conservative
            btc_change_4h = 1 + (btc_trend_15m * 4)
        
        # Calculate RS (alt / btc)
        rs_1h = alt_change_1h / btc_change_1h if btc_change_1h != 0 else 1.0
        rs_4h = alt_change_4h / btc_change_4h if btc_change_4h != 0 else 1.0
        
        # Cache result
        self._rs_cache[symbol] = {
            'rs_1h': rs_1h,
            'rs_4h': rs_4h,
            'alt_1h': alt_change_1h,
            'alt_4h': alt_change_4h,
            'btc_1h': btc_change_1h,
            'btc_4h': btc_change_4h,
            'updated': datetime.now(timezone.utc)
        }
        
        return self._rs_cache[symbol]
    
    def _score_setup(self, rs_data: dict, features: dict) -> float:
        """Score the relative strength setup."""
        score = 50  # Base
        
        rs_1h = rs_data['rs_1h']
        rs_4h = rs_data['rs_4h']
        
        # 1h RS score
        if rs_1h >= 1.03:
            score += 20  # Strong RS
        elif rs_1h >= 1.02:
            score += 15
        elif rs_1h >= 1.015:
            score += 10
        
        # 4h RS score (more weight to longer term)
        if rs_4h >= 1.05:
            score += 25  # Very strong
        elif rs_4h >= 1.03:
            score += 20
        elif rs_4h >= 1.02:
            score += 15
        elif rs_4h >= 1.01:
            score += 10
        
        # Entry timing
        trend_5m = features.get('trend_5m', 0)
        if -0.5 <= trend_5m <= 0:
            score += 10  # Perfect dip entry
        elif -1.0 <= trend_5m < -0.5:
            score += 5   # Decent dip
        elif 0 < trend_5m <= 0.3:
            score += 5   # Flat, acceptable
        
        # Volume confirmation
        vol_ratio = features.get('vol_ratio', 1.0)
        if vol_ratio >= 1.5:
            score += 10
        elif vol_ratio >= 1.2:
            score += 5
        
        return min(100, score)
    
    def reset(self, symbol: str):
        """Reset state for symbol."""
        self._rs_cache.pop(symbol, None)
