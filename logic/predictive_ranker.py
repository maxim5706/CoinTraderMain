"""Predictive Ranker - Pre-scores coins using MTF analysis before signals fire.

Integrates with leaderboard to:
1. Track coin momentum across 1m, 1h, 4h timeframes
2. Build predictive scores ahead of actual signals  
3. Identify coins likely to move in next 1-4 hours
4. Rank by "readiness to trade" score

This allows the bot to:
- Pre-position attention on high-probability setups
- Avoid chasing moves that already happened
- Catch early entries on emerging trends
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)


@dataclass
class MTFScore:
    """Multi-timeframe momentum score for a coin."""
    symbol: str
    
    # Timeframe trends (% change)
    trend_1m: float = 0.0      # Last minute
    trend_5m: float = 0.0      # Last 5 minutes
    trend_1h: float = 0.0      # Last hour
    trend_4h: float = 0.0      # Last 4 hours
    trend_1d: float = 0.0      # Last day
    
    # Volume ratios vs average
    vol_1m: float = 1.0
    vol_1h: float = 1.0
    vol_4h: float = 1.0
    
    # Momentum acceleration (is it building?)
    acceleration: float = 0.0  # Positive = building, negative = fading
    
    # RSI levels
    rsi_1h: float = 50.0
    rsi_4h: float = 50.0
    
    # VWAP position
    vwap_distance: float = 0.0
    
    # Computed scores
    alignment_score: float = 0.0  # How aligned are timeframes?
    readiness_score: float = 0.0  # Ready to trade now?
    prediction_score: float = 0.0 # Likely to move soon?
    
    # Timing
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def is_stale(self, max_age_seconds: float = 120) -> bool:
        age = (datetime.now(timezone.utc) - self.updated_at).total_seconds()
        return age > max_age_seconds


@dataclass
class CoinPrediction:
    """Prediction for a coin's near-term movement."""
    symbol: str
    direction: str  # "bullish", "bearish", "neutral"
    confidence: float  # 0-100
    timeframe: str  # "1h", "4h" - when we expect move
    entry_window: str  # "now", "wait", "missed"
    reasons: List[str] = field(default_factory=list)
    mtf_score: Optional[MTFScore] = None
    
    @property
    def is_actionable(self) -> bool:
        return (
            self.direction == "bullish" and
            self.confidence >= 60 and
            self.entry_window in ("now", "wait")
        )


class PredictiveRanker:
    """
    Pre-scores coins using MTF analysis to predict movements.
    
    Strategy:
    1. Track momentum across timeframes
    2. Identify "coiling" patterns (low 1m volatility, building volume)
    3. Score alignment (all TFs pointing same direction)
    4. Predict breakout timing
    """
    
    def __init__(self):
        self.mtf_scores: Dict[str, MTFScore] = {}
        self.predictions: Dict[str, CoinPrediction] = {}
        self.history: Dict[str, List[Tuple[datetime, float]]] = defaultdict(list)
        
        # Config
        self.min_alignment_for_trade = 0.6  # 60% alignment needed
        self.min_volume_for_signal = 1.5    # 1.5x avg volume
        self.max_rsi_for_entry = 70         # Avoid overbought
        self.min_rsi_for_entry = 35         # Avoid oversold
    
    def update_from_buffer(self, symbol: str, buffer) -> Optional[MTFScore]:
        """Update MTF score from candle buffer."""
        if buffer is None:
            return None
        
        try:
            score = MTFScore(symbol=symbol)
            
            # Calculate trends from candles
            if hasattr(buffer, 'candles_1m') and len(buffer.candles_1m) >= 5:
                candles_1m = list(buffer.candles_1m)
                score.trend_1m = self._calc_trend(candles_1m, 1)
                score.trend_5m = self._calc_trend(candles_1m, 5)
                score.vol_1m = self._calc_vol_ratio(candles_1m, 5)
            
            if hasattr(buffer, 'candles_1h') and len(buffer.candles_1h) >= 4:
                candles_1h = list(buffer.candles_1h)
                score.trend_1h = self._calc_trend(candles_1h, 1)
                score.trend_4h = self._calc_trend(candles_1h, 4)
                score.vol_1h = self._calc_vol_ratio(candles_1h, 4)
            
            if hasattr(buffer, 'candles_1d') and len(buffer.candles_1d) >= 1:
                candles_1d = list(buffer.candles_1d)
                score.trend_1d = self._calc_trend(candles_1d, 1)
            
            # Get RSI from indicators if available
            from logic.intelligence import intelligence
            ind = intelligence.get_live_indicators(symbol)
            if ind and ind.is_ready:
                score.rsi_1h = ind.rsi_14
                score.vwap_distance = ind.vwap_distance
                score.acceleration = getattr(ind, 'acceleration_score', 0) * 100
            
            # Calculate composite scores
            score.alignment_score = self._calc_alignment(score)
            score.readiness_score = self._calc_readiness(score)
            score.prediction_score = self._calc_prediction(score)
            score.updated_at = datetime.now(timezone.utc)
            
            # Store
            self.mtf_scores[symbol] = score
            
            # Track history for pattern detection
            self.history[symbol].append((score.updated_at, score.prediction_score))
            # Keep last 60 data points (~1 hour at 1min intervals)
            if len(self.history[symbol]) > 60:
                self.history[symbol] = self.history[symbol][-60:]
            
            return score
            
        except Exception as e:
            logger.debug("[PREDICT] Error updating %s: %s", symbol, e)
            return None
    
    def _calc_trend(self, candles: list, periods: int) -> float:
        """Calculate % trend over N periods."""
        if len(candles) < periods + 1:
            return 0.0
        
        recent = candles[-1]
        past = candles[-(periods + 1)]
        
        close_recent = getattr(recent, 'close', 0) or recent.get('close', 0) if isinstance(recent, dict) else recent.close
        close_past = getattr(past, 'close', 0) or past.get('close', 0) if isinstance(past, dict) else past.close
        
        if close_past <= 0:
            return 0.0
        
        return ((close_recent / close_past) - 1) * 100
    
    def _calc_vol_ratio(self, candles: list, periods: int) -> float:
        """Calculate recent volume vs average."""
        if len(candles) < periods + 5:
            return 1.0
        
        recent_vol = sum(
            getattr(c, 'volume', 0) or c.get('volume', 0) if isinstance(c, dict) else c.volume
            for c in candles[-periods:]
        )
        
        avg_vol = sum(
            getattr(c, 'volume', 0) or c.get('volume', 0) if isinstance(c, dict) else c.volume
            for c in candles[-(periods + 5):-periods]
        ) / 5
        
        if avg_vol <= 0:
            return 1.0
        
        return recent_vol / (avg_vol * periods)
    
    def _calc_alignment(self, score: MTFScore) -> float:
        """Calculate timeframe alignment (0-100)."""
        signals = []
        
        # Each TF contributes a direction signal
        for trend, weight in [
            (score.trend_1m, 0.1),   # 1m: low weight, noisy
            (score.trend_5m, 0.15),  # 5m: some weight
            (score.trend_1h, 0.35),  # 1h: main signal
            (score.trend_4h, 0.25),  # 4h: important context
            (score.trend_1d, 0.15),  # 1d: big picture
        ]:
            if trend > 0.3:
                signals.append(weight)
            elif trend < -0.3:
                signals.append(-weight)
            else:
                signals.append(0)
        
        # Alignment = how much signals agree
        total_bullish = sum(s for s in signals if s > 0)
        total_bearish = abs(sum(s for s in signals if s < 0))
        
        if total_bullish > total_bearish:
            return total_bullish * 100
        else:
            return -total_bearish * 100
    
    def _calc_readiness(self, score: MTFScore) -> float:
        """Calculate readiness to trade NOW (0-100)."""
        readiness = 50  # Base
        
        # Alignment bonus
        if abs(score.alignment_score) >= 60:
            readiness += 15
        elif abs(score.alignment_score) >= 40:
            readiness += 8
        
        # Volume building = ready to move
        if score.vol_1m >= 2.0:
            readiness += 15
        elif score.vol_1m >= 1.5:
            readiness += 8
        
        # RSI in tradeable range
        if self.min_rsi_for_entry < score.rsi_1h < self.max_rsi_for_entry:
            readiness += 10
        else:
            readiness -= 15
        
        # Above VWAP = bullish context
        if score.vwap_distance > 0.5:
            readiness += 8
        elif score.vwap_distance < -1.0:
            readiness -= 10
        
        # Acceleration = momentum building
        if score.acceleration > 50:
            readiness += 12
        elif score.acceleration > 25:
            readiness += 5
        elif score.acceleration < -25:
            readiness -= 10
        
        return max(0, min(100, readiness))
    
    def _calc_prediction(self, score: MTFScore) -> float:
        """Calculate prediction score - likelihood of significant move."""
        prediction = 40  # Base
        
        # Coiling pattern: low 1m vol but high 1h vol = tension building
        if score.vol_1m < 1.2 and score.vol_1h > 1.5:
            prediction += 20
            
        # Strong 4h trend + pullback on 1h = continuation likely
        if abs(score.trend_4h) > 2.0 and abs(score.trend_1h) < 1.0:
            prediction += 15
        
        # All timeframes aligned strongly
        if abs(score.alignment_score) >= 70:
            prediction += 18
        elif abs(score.alignment_score) >= 50:
            prediction += 10
        
        # Volume surge across timeframes
        avg_vol = (score.vol_1m + score.vol_1h) / 2
        if avg_vol > 2.0:
            prediction += 12
        elif avg_vol > 1.5:
            prediction += 6
        
        # Acceleration indicates building momentum
        if score.acceleration > 40:
            prediction += 10
        
        return max(0, min(100, prediction))
    
    def predict(self, symbol: str) -> CoinPrediction:
        """Generate prediction for a coin."""
        mtf = self.mtf_scores.get(symbol)
        
        if mtf is None or mtf.is_stale():
            return CoinPrediction(
                symbol=symbol,
                direction="neutral",
                confidence=0,
                timeframe="unknown",
                entry_window="wait",
                reasons=["No data"]
            )
        
        # Recalculate scores if not set (for manually created MTFScores)
        if mtf.alignment_score == 0 and (mtf.trend_1h != 0 or mtf.trend_4h != 0):
            mtf.alignment_score = self._calc_alignment(mtf)
            mtf.readiness_score = self._calc_readiness(mtf)
            mtf.prediction_score = self._calc_prediction(mtf)
        
        # Determine direction from alignment
        if mtf.alignment_score >= 40:
            direction = "bullish"
        elif mtf.alignment_score <= -40:
            direction = "bearish"
        else:
            direction = "neutral"
        
        # Confidence from multiple factors
        confidence = (mtf.readiness_score + mtf.prediction_score) / 2
        
        # Adjust for RSI extremes
        if mtf.rsi_1h > 75:
            confidence -= 20
            direction = "neutral" if direction == "bullish" else direction
        elif mtf.rsi_1h < 25:
            confidence -= 20
            direction = "neutral" if direction == "bearish" else direction
        
        # Determine timeframe
        if abs(mtf.trend_1h) > 1.5:
            timeframe = "1h"
        elif abs(mtf.trend_4h) > 2.0:
            timeframe = "4h"
        else:
            timeframe = "1h"
        
        # Entry window
        if mtf.readiness_score >= 70 and confidence >= 60:
            entry_window = "now"
        elif mtf.prediction_score >= 60 and confidence >= 50:
            entry_window = "wait"
        elif abs(mtf.trend_1m) > 3.0:
            entry_window = "missed"  # Already moved
        else:
            entry_window = "wait"
        
        # Build reasons
        reasons = []
        if abs(mtf.alignment_score) >= 60:
            reasons.append(f"TF aligned {mtf.alignment_score:+.0f}%")
        if mtf.vol_1h > 1.5:
            reasons.append(f"Vol {mtf.vol_1h:.1f}x")
        if mtf.acceleration > 30:
            reasons.append(f"Accel {mtf.acceleration:.0f}%")
        if abs(mtf.trend_4h) > 2.0:
            reasons.append(f"4h trend {mtf.trend_4h:+.1f}%")
        
        prediction = CoinPrediction(
            symbol=symbol,
            direction=direction,
            confidence=max(0, min(100, confidence)),
            timeframe=timeframe,
            entry_window=entry_window,
            reasons=reasons,
            mtf_score=mtf
        )
        
        self.predictions[symbol] = prediction
        return prediction
    
    def get_top_predictions(self, n: int = 10, direction: str = "bullish") -> List[CoinPrediction]:
        """Get top N predictions by confidence."""
        predictions = []
        
        for symbol, mtf in self.mtf_scores.items():
            if mtf.is_stale():
                continue
            pred = self.predict(symbol)
            if pred.direction == direction and pred.confidence >= 50:
                predictions.append(pred)
        
        predictions.sort(key=lambda p: p.confidence, reverse=True)
        return predictions[:n]
    
    def get_actionable_plays(self) -> List[CoinPrediction]:
        """Get all actionable plays (ready to trade now)."""
        return [
            p for p in self.predictions.values()
            if p.is_actionable and not (p.mtf_score and p.mtf_score.is_stale())
        ]
    
    def rank_for_entry(self, symbols: List[str]) -> List[Tuple[str, float, str]]:
        """
        Rank symbols by entry attractiveness.
        
        Returns: List of (symbol, score, reason) sorted by score desc
        """
        ranked = []
        
        for symbol in symbols:
            mtf = self.mtf_scores.get(symbol)
            if mtf is None or mtf.is_stale():
                continue
            
            # Composite score for entry
            score = 0
            reasons = []
            
            # Alignment (40% weight)
            if mtf.alignment_score >= 60:
                score += 40
                reasons.append("aligned")
            elif mtf.alignment_score >= 40:
                score += 25
            elif mtf.alignment_score <= -40:
                score -= 20
            
            # Readiness (30% weight) 
            score += mtf.readiness_score * 0.30
            if mtf.readiness_score >= 70:
                reasons.append("ready")
            
            # Prediction (30% weight)
            score += mtf.prediction_score * 0.30
            if mtf.prediction_score >= 60:
                reasons.append("likely_move")
            
            # RSI filter
            if not (self.min_rsi_for_entry < mtf.rsi_1h < self.max_rsi_for_entry):
                score -= 25
                reasons.append("rsi_extreme")
            
            ranked.append((symbol, score, "+".join(reasons) if reasons else "neutral"))
        
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked
    
    def should_wait_for_entry(self, symbol: str) -> Tuple[bool, str]:
        """
        Check if we should wait before entering this coin.
        
        Returns: (should_wait, reason)
        """
        mtf = self.mtf_scores.get(symbol)
        if mtf is None:
            return False, "no_data"
        
        # Already extended - wait for pullback
        if mtf.trend_1m > 2.0 and mtf.rsi_1h > 65:
            return True, "extended_wait_pullback"
        
        # Volume dying - wait for confirmation
        if mtf.vol_1m < 0.8 and mtf.acceleration < 0:
            return True, "momentum_fading"
        
        # Timeframes misaligned - wait for alignment
        if abs(mtf.alignment_score) < 30:
            return True, "wait_for_alignment"
        
        # Counter-trend on shorter TF
        if (mtf.trend_4h > 1.0 and mtf.trend_1h < -0.5):
            return True, "wait_pullback_end"
        
        return False, "ready"
    
    def get_status(self) -> dict:
        """Get ranker status for dashboard."""
        actionable = len(self.get_actionable_plays())
        total = len(self.mtf_scores)
        stale = sum(1 for m in self.mtf_scores.values() if m.is_stale())
        
        top = self.get_top_predictions(3)
        
        return {
            "total_tracked": total,
            "actionable": actionable,
            "stale": stale,
            "top_3": [
                {"symbol": p.symbol, "confidence": p.confidence, "direction": p.direction}
                for p in top
            ]
        }


# Global instance
predictive_ranker = PredictiveRanker()
