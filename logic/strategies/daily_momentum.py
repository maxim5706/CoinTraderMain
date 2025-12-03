"""
Daily Momentum / Multi-Day Trend Strategy.

Catches coins like SUI that move gradually over days rather than sharp intraday bursts.
"Find the slow grinders before they explode."
"""

from typing import Optional
from datetime import datetime, timezone

from .base import BaseStrategy, StrategySignal, SignalDirection
from core.config import settings


class DailyMomentumStrategy(BaseStrategy):
    """
    Multi-day momentum scanner for catching trending coins.
    
    Pattern:
    1. 3+ days of higher lows (accumulation)
    2. RSI breaking above 50-60 zone
    3. Volume accumulation (recent days > average)
    4. Price near/above daily EMA
    5. Entry on intraday dip within the daily uptrend
    
    This catches moves like SUI that grind higher over days
    without the sharp intraday bursts the other strategies need.
    """
    
    strategy_id = "daily_momentum"
    
    # Track daily analysis to avoid recomputing every tick
    _daily_cache: dict = {}  # symbol -> {score, timestamp, data}
    
    def __init__(self):
        self._daily_cache = {}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Analyze for daily momentum setup.
        """
        if buffer is None:
            return None
        
        # Need daily candles
        daily_candles = getattr(buffer, 'candles_1d', [])
        if len(daily_candles) < 5:
            return None
        
        # Also need 1m candles for intraday entry timing
        if len(buffer.candles_1m) < 20:
            return None
        
        # Check cache - only recompute daily analysis every 5 minutes
        cache_key = symbol
        now = datetime.now(timezone.utc)
        cached = self._daily_cache.get(cache_key)
        
        if cached and (now - cached['timestamp']).total_seconds() < 300:
            # Use cached daily analysis, but still check intraday entry
            if cached['score'] < 60:
                return None
            return self._check_intraday_entry(symbol, buffer, features, cached)
        
        # === DAILY ANALYSIS ===
        
        # Get last 7 days of data
        recent_days = daily_candles[-7:] if len(daily_candles) >= 7 else daily_candles
        
        if len(recent_days) < 3:
            return None
        
        # 1. Check for higher lows (accumulation pattern)
        higher_lows_score = self._score_higher_lows(recent_days)
        
        # 2. Calculate daily RSI
        rsi_score = self._score_rsi(daily_candles)
        
        # 3. Volume accumulation
        volume_score = self._score_volume_trend(recent_days)
        
        # 4. Price vs EMA position
        ema_score = self._score_ema_position(daily_candles)
        
        # 5. Multi-day momentum
        momentum_score = self._score_momentum(recent_days)
        
        # Combine scores
        total_score = (
            higher_lows_score * 0.25 +
            rsi_score * 0.20 +
            volume_score * 0.20 +
            ema_score * 0.15 +
            momentum_score * 0.20
        )
        
        # Build reason string
        reasons = []
        if higher_lows_score >= 70:
            reasons.append(f"HL:{higher_lows_score:.0f}")
        if rsi_score >= 70:
            reasons.append(f"RSI:{rsi_score:.0f}")
        if volume_score >= 70:
            reasons.append(f"Vol:{volume_score:.0f}")
        if momentum_score >= 70:
            reasons.append(f"Mom:{momentum_score:.0f}")
        
        # Cache the result
        self._daily_cache[cache_key] = {
            'score': total_score,
            'timestamp': now,
            'higher_lows': higher_lows_score,
            'rsi': rsi_score,
            'volume': volume_score,
            'ema': ema_score,
            'momentum': momentum_score,
            'reasons': reasons,
        }
        
        # Need minimum score to proceed
        if total_score < 60:
            return None
        
        # Check for intraday entry opportunity
        return self._check_intraday_entry(symbol, buffer, features, self._daily_cache[cache_key])
    
    def _score_higher_lows(self, daily_candles: list) -> float:
        """Score based on pattern of higher lows (accumulation)."""
        if len(daily_candles) < 3:
            return 0
        
        lows = [c.low for c in daily_candles[-5:]]
        
        # Count higher lows
        higher_low_count = 0
        for i in range(1, len(lows)):
            if lows[i] > lows[i-1] * 0.995:  # Allow 0.5% tolerance
                higher_low_count += 1
        
        # Score based on streak
        if higher_low_count >= 4:
            return 100
        elif higher_low_count >= 3:
            return 85
        elif higher_low_count >= 2:
            return 70
        elif higher_low_count >= 1:
            return 50
        return 30
    
    def _score_rsi(self, daily_candles: list) -> float:
        """Score based on RSI position and direction."""
        if len(daily_candles) < 14:
            return 50  # Neutral if not enough data
        
        # Calculate simple RSI
        gains = []
        losses = []
        
        for i in range(1, min(15, len(daily_candles))):
            change = daily_candles[-i].close - daily_candles[-i-1].close
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0.0001
        
        rs = avg_gain / avg_loss if avg_loss > 0 else 100
        rsi = 100 - (100 / (1 + rs))
        
        # Score: bullish zone is 50-70
        if 55 <= rsi <= 70:
            return 100  # Sweet spot
        elif 50 <= rsi < 55:
            return 85  # Breaking out
        elif 45 <= rsi < 50:
            return 70  # Neutral but rising
        elif 70 < rsi <= 80:
            return 60  # Overbought warning
        elif rsi > 80:
            return 30  # Too overbought
        elif rsi < 40:
            return 40  # Oversold, might bounce
        return 50
    
    def _score_volume_trend(self, daily_candles: list) -> float:
        """Score based on volume accumulation over recent days."""
        if len(daily_candles) < 5:
            return 50
        
        # Compare recent 3 days volume to previous period
        recent_vol = sum(c.volume for c in daily_candles[-3:]) / 3
        older_vol = sum(c.volume for c in daily_candles[-7:-3]) / max(1, len(daily_candles[-7:-3]))
        
        if older_vol <= 0:
            return 50
        
        vol_ratio = recent_vol / older_vol
        
        if vol_ratio >= 2.0:
            return 100  # Strong accumulation
        elif vol_ratio >= 1.5:
            return 85
        elif vol_ratio >= 1.2:
            return 70
        elif vol_ratio >= 1.0:
            return 55
        elif vol_ratio >= 0.8:
            return 40  # Volume declining
        return 30
    
    def _score_ema_position(self, daily_candles: list) -> float:
        """Score based on price vs EMA position."""
        if len(daily_candles) < 20:
            return 50
        
        # Simple 10-day EMA approximation
        closes = [c.close for c in daily_candles[-20:]]
        ema10 = sum(closes[-10:]) / 10
        ema20 = sum(closes[-20:]) / 20
        current_price = closes[-1]
        
        # Price above both EMAs is bullish
        above_10 = current_price > ema10
        above_20 = current_price > ema20
        ema10_above_20 = ema10 > ema20  # Golden alignment
        
        score = 50
        if above_10:
            score += 15
        if above_20:
            score += 15
        if ema10_above_20:
            score += 20
        
        return min(100, score)
    
    def _score_momentum(self, daily_candles: list) -> float:
        """Score based on multi-day price momentum."""
        if len(daily_candles) < 3:
            return 50
        
        # 3-day return
        ret_3d = (daily_candles[-1].close - daily_candles[-3].close) / daily_candles[-3].close * 100
        
        # 7-day return if available
        ret_7d = 0
        if len(daily_candles) >= 7:
            ret_7d = (daily_candles[-1].close - daily_candles[-7].close) / daily_candles[-7].close * 100
        
        # Score based on momentum
        if ret_3d >= 10:
            score_3d = 100
        elif ret_3d >= 5:
            score_3d = 85
        elif ret_3d >= 2:
            score_3d = 70
        elif ret_3d >= 0:
            score_3d = 55
        else:
            score_3d = 30
        
        if ret_7d >= 15:
            score_7d = 100
        elif ret_7d >= 8:
            score_7d = 80
        elif ret_7d >= 3:
            score_7d = 65
        else:
            score_7d = 45
        
        return (score_3d + score_7d) / 2
    
    def _check_intraday_entry(
        self,
        symbol: str,
        buffer,
        features: dict,
        daily_data: dict
    ) -> Optional[StrategySignal]:
        """
        Check for good intraday entry point within the daily uptrend.
        We want to buy dips, not chase pumps.
        """
        candles = buffer.candles_1m
        current = candles[-1]
        price = current.close
        
        # Get intraday metrics
        trend_5m = features.get('trend_5m', 0)
        vwap_dist = features.get('vwap_distance', 0)
        
        # Don't chase - we want pullbacks
        # If 5m trend is too hot (>1%), wait for dip
        if trend_5m > 1.5:
            return None
        
        # Best entry: slight pullback (-0.5% to +0.5%) within daily uptrend
        # Or: reclaiming VWAP
        entry_bonus = 0
        entry_reason = ""
        
        if -1.0 <= trend_5m <= 0.5:
            entry_bonus = 20  # Good dip entry
            entry_reason = f"dip entry ({trend_5m:+.1f}%)"
        elif -0.5 <= vwap_dist <= 0.3:
            entry_bonus = 15  # Near VWAP
            entry_reason = f"VWAP zone ({vwap_dist:+.1f}%)"
        else:
            # Not ideal entry, reduce score
            entry_bonus = 0
            entry_reason = "waiting for better entry"
        
        # Final score
        daily_score = daily_data['score']
        final_score = daily_score + entry_bonus
        
        if final_score < 70:
            return None
        
        # Calculate levels
        atr = buffer.atr(14, "1m")
        if atr <= 0:
            atr = price * 0.02  # Fallback: 2% ATR
        
        stop_price = price * (1 - settings.fixed_stop_pct)
        tp1_price = price * (1 + settings.tp1_pct)
        tp2_price = price * (1 + settings.tp2_pct)
        
        risk_pct = abs(price - stop_price) / price * 100
        rr_ratio = (tp1_price - price) / (price - stop_price) if price > stop_price else 0
        
        # Build reason
        daily_reasons = daily_data.get('reasons', [])
        reason = f"Daily trend: {' '.join(daily_reasons)}, {entry_reason}"
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=final_score,
            trend_score=daily_data.get('momentum', 0),
            volume_score=daily_data.get('volume', 0),
            pattern_score=daily_data.get('higher_lows', 0),
            timing_score=entry_bonus,
            entry_price=price,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            risk_pct=risk_pct,
            rr_ratio=rr_ratio,
            reason=reason,
            reasons=daily_reasons + [entry_reason],
        )
