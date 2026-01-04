"""Entry scoring system for trade decisions.

Calculates entry confidence scores using rules-based scoring with ML boost.
Integrates predictive ranker for MTF-aware entry timing.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, timezone

from core.config import settings
from core.models import Signal
from core.logging_utils import get_logger

logger = get_logger(__name__)

try:
    from logic.predictive_ranker import predictive_ranker
except ImportError:
    predictive_ranker = None

CANONICAL_GATE_ORDER = [
    "warmth",
    "limits",
    "spread",
    "score_regime",
    "risk_reward",
    "budget",
    "ml_boost",
]


@dataclass
class EntryScore:
    """Detailed entry confidence breakdown."""
    symbol: str
    total_score: float = 0.0
    trend_score: float = 0.0
    volume_score: float = 0.0
    vwap_score: float = 0.0
    range_score: float = 0.0
    tier_score: float = 0.0
    spread_score: float = 0.0
    ml_score: float = 0.0
    ml_confidence: float = 0.0
    ml_boost: float = 0.0
    rsi: float = 50.0
    macd_signal: float = 0.0
    bb_position: float = 0.5
    btc_trend_ok: bool = False
    symbol_trend_ok: bool = False
    not_overbought: bool = True
    btc_regime: str = "normal"
    reasons: List[str] = field(default_factory=list)
    
    @property
    def should_enter(self) -> bool:
        """Check if score meets entry threshold."""
        base_min = settings.entry_score_min  # Default 40
        
        # Simple score-based entry - if score is good enough, trade
        # BTC regime adjustment: require higher score in caution/bearish
        if self.btc_regime == "caution":
            return self.total_score >= base_min + 5
        elif self.btc_regime == "bearish":
            return self.total_score >= base_min + 10
        else:
            return self.total_score >= base_min


class EntryScorer:
    """Calculates entry scores for signals."""
    
    def __init__(self, regime_detector, indicator_cache, limit_checker):
        self.regime = regime_detector
        self.cache = indicator_cache
        self.limits = limit_checker
    
    def score(self, signal: Signal, burst_metrics: dict, positions: dict) -> EntryScore:
        """Calculate entry score with canonical gate order."""
        score = EntryScore(symbol=signal.symbol)
        
        strategy_confidence = getattr(signal, "confidence", 0.0)
        strategy_id = getattr(signal, "strategy_id", "")
        if strategy_confidence > 0 and strategy_id:
            return self._score_strategy_signal(score, signal, strategy_confidence, strategy_id)
        
        score = self._calculate_base_score(score, signal, burst_metrics)
        score = self._apply_quality_filters(score, signal, burst_metrics)
        score = self._apply_regime_filter(score, burst_metrics)
        score = self._apply_ml_gate(score, signal)
        
        return score
    
    def _score_strategy_signal(self, score: EntryScore, signal: Signal, 
                                confidence: float, strategy_id: str) -> EntryScore:
        """Score a signal from strategy with pre-computed confidence."""
        score.total_score = confidence * 100
        score.reasons.append(f"{strategy_id}: {confidence:.0%}")
        score.btc_regime = self.regime.regime
        score.btc_trend_ok = (self.regime.regime == "normal")
        score.symbol_trend_ok = True
        score.not_overbought = True
        score.volume_score = 10
        
        # Apply predictive ranker MTF analysis
        score = self._apply_predictive_boost(score, signal.symbol)
        
        ml = self.cache.get_ml(signal.symbol)
        if ml and not ml.is_stale():
            score.ml_score = ml.raw_score
            if ml.raw_score > 0.6:
                score.ml_boost = (ml.raw_score - 0.5) * 20
                score.total_score += score.ml_boost
                score.reasons.append(f"ML +{score.ml_boost:.0f}")
        
        return score
    
    def _apply_predictive_boost(self, score: EntryScore, symbol: str) -> EntryScore:
        """Apply predictive ranker MTF analysis to score."""
        if predictive_ranker is None:
            return score
        
        try:
            # Get prediction
            pred = predictive_ranker.predict(symbol)
            mtf = pred.mtf_score
            
            if mtf is None or mtf.is_stale():
                return score
            
            # Check if we should wait
            should_wait, wait_reason = predictive_ranker.should_wait_for_entry(symbol)
            if should_wait:
                score.total_score -= 15
                score.reasons.append(f"wait:{wait_reason}")
                return score
            
            # Boost for aligned timeframes
            if mtf.alignment_score >= 60:
                score.total_score += 15
                score.reasons.append(f"MTF aligned +{mtf.alignment_score:.0f}%")
            elif mtf.alignment_score >= 40:
                score.total_score += 8
            elif mtf.alignment_score <= -30:
                score.total_score -= 10
                score.reasons.append("MTF divergent")
            
            # Boost for high readiness
            if mtf.readiness_score >= 70:
                score.total_score += 10
                score.reasons.append("ready_now")
            elif mtf.readiness_score < 40:
                score.total_score -= 8
            
            # Boost for prediction confidence
            if pred.confidence >= 70 and pred.direction == "bullish":
                score.total_score += 12
                score.reasons.append(f"predict:{pred.confidence:.0f}%")
            elif pred.confidence >= 60 and pred.direction == "bullish":
                score.total_score += 6
            
            # Penalty for missed entry window
            if pred.entry_window == "missed":
                score.total_score -= 20
                score.reasons.append("move_missed")
            
        except Exception as e:
            logger.debug("[SCORE] Predictive boost error for %s: %s", symbol, e)
        
        return score
    
    def _calculate_base_score(self, score: EntryScore, signal: Signal, 
                               burst_metrics: dict) -> EntryScore:
        """Calculate base score from burst metrics."""
        vol_spike = burst_metrics.get("vol_spike", 1.0)
        range_spike = burst_metrics.get("range_spike", 1.0)
        trend_15m = burst_metrics.get("trend_15m", 0.0)
        vwap_dist = burst_metrics.get("vwap_distance", 0.0)
        spread_bps = burst_metrics.get("spread_bps", 50.0)
        tier = burst_metrics.get("tier", "unknown")
        price = getattr(signal, 'price', 0) or burst_metrics.get('price', 0)
        
        # Trend score (0-20)
        if trend_15m >= 2.0:
            score.trend_score = 20
            score.reasons.append(f"Strong trend +{trend_15m:.1f}%")
        elif trend_15m >= 1.0:
            score.trend_score = 15
        elif trend_15m >= 0.5:
            score.trend_score = 10
        elif trend_15m > 0:
            score.trend_score = 5
        
        # Volume score (0-20)
        if vol_spike >= 5.0:
            score.volume_score = 20
            score.reasons.append(f"Massive volume {vol_spike:.1f}x")
        elif vol_spike >= 3.0:
            score.volume_score = 15
        elif vol_spike >= 2.0:
            score.volume_score = 10
        elif vol_spike >= 1.5:
            score.volume_score = 5
        
        # VWAP score (0-20)
        if vwap_dist > 0.5:
            score.vwap_score = 20
        elif vwap_dist > 0:
            score.vwap_score = 15
        elif vwap_dist > -0.3:
            score.vwap_score = 10
        
        # Range score (0-15)
        if range_spike >= 3.0:
            score.range_score = 15
        elif range_spike >= 2.0:
            score.range_score = 10
        elif range_spike >= 1.5:
            score.range_score = 5
        
        # Tier score (0-20)
        tier_scores = {"micro": 20, "small": 15, "mid": 8, "large": 3}
        score.tier_score = tier_scores.get(tier, 0)
        
        # Spread score (0-15)
        if spread_bps < 5:
            score.spread_score = 15
        elif spread_bps < 10:
            score.spread_score = 10
        elif spread_bps < 15:
            score.spread_score = 5
        
        # Price volatility bonus
        price_bonus = 0
        if 0 < price < 0.10:
            price_bonus = 15
        elif price < 1.0:
            price_bonus = 10
        elif price < 10.0:
            price_bonus = 5
        elif price > 1000:
            price_bonus = -5
        
        score.total_score = (
            score.trend_score + score.volume_score + score.vwap_score +
            score.range_score + score.tier_score + score.spread_score + price_bonus
        )
        
        return score
    
    def _apply_quality_filters(self, score: EntryScore, signal: Signal,
                                burst_metrics: dict) -> EntryScore:
        """Apply quality filters from live indicators."""
        ind = self.cache.get_indicators(signal.symbol)
        if not (ind and ind.is_ready):
            return score
        
        quality_adjust = 0
        trend_15m = burst_metrics.get("trend_15m", 0.0)
        
        # RSI filters
        if ind.rsi_14 > 75:
            quality_adjust -= 15
            score.not_overbought = False
        elif ind.rsi_14 > 70:
            quality_adjust -= 8
        elif 50 <= ind.rsi_14 <= 65:
            quality_adjust += 5
        
        # MACD
        if ind.macd_histogram > 0:
            quality_adjust += 5
        elif ind.macd_histogram < -0.001:
            quality_adjust -= 5
        
        # EMA stack
        if ind.price > ind.ema9 > ind.ema21:
            quality_adjust += 5
        elif ind.price < ind.ema9 < ind.ema21:
            quality_adjust -= 10
        
        # Bollinger position
        if ind.bb_position > 0.9:
            quality_adjust -= 10
        elif 0.4 <= ind.bb_position <= 0.7:
            quality_adjust += 3
        
        # Chop detection
        if ind.is_choppy or ind.chop_score > 0.6:
            quality_adjust -= 15
        elif ind.chop_score > 0.4:
            quality_adjust -= 5
        
        # Buy pressure
        if ind.buy_pressure > 0.65:
            quality_adjust += 5
        elif ind.buy_pressure < 0.4:
            quality_adjust -= 5
        
        # OBV divergence
        if ind.obv_slope < 0 and trend_15m > 0:
            quality_adjust -= 5
        
        # EDGE: Acceleration Score (volume building = catch move early)
        accel = getattr(ind, 'acceleration_score', 0)
        if accel >= 0.6:
            quality_adjust += 10
            score.reasons.append(f"Accelerating +{accel:.0%}")
        elif accel >= 0.3:
            quality_adjust += 5
        
        # EDGE: Whale Detection (follow the big money)
        whale_bias = getattr(ind, 'whale_bias', 0)
        whale_activity = getattr(ind, 'whale_activity', 0)
        if whale_bias == 1 and whale_activity >= 0.3:
            quality_adjust += 8
            score.reasons.append("Whales buying")
        elif whale_bias == -1 and whale_activity >= 0.3:
            quality_adjust -= 10
            score.reasons.append("Whales selling")
        
        # MTF alignment
        trend_5m = getattr(ind, 'trend_5m', 0)
        if trend_5m > 0 and trend_15m > 0:
            quality_adjust += 5
        elif (trend_5m > 0) != (trend_15m > 0):
            quality_adjust -= 5
        
        # Time of day
        hour_utc = datetime.now(timezone.utc).hour
        if 2 <= hour_utc <= 6:
            quality_adjust -= 5
        elif 13 <= hour_utc <= 21:
            quality_adjust += 3
        
        # Daily/weekly context
        trend_1d = getattr(ind, 'trend_1d', 0)
        if trend_1d > 2.0:
            quality_adjust += 8
        elif trend_1d < -2.0:
            quality_adjust -= 8
        
        daily_range_pos = getattr(ind, 'daily_range_position', 0.5)
        if daily_range_pos < 0.2:
            quality_adjust += 5
        elif daily_range_pos > 0.8:
            quality_adjust -= 5
        
        score.total_score += quality_adjust
        score.rsi = ind.rsi_14
        score.macd_signal = ind.macd_histogram
        score.bb_position = ind.bb_position
        
        return score
    
    def _apply_regime_filter(self, score: EntryScore, burst_metrics: dict) -> EntryScore:
        """Apply BTC regime filter."""
        trend_15m = burst_metrics.get("trend_15m", 0.0)
        score.btc_regime = self.regime.regime
        
        if self.regime.regime == "normal":
            score.btc_trend_ok = True
        elif self.regime.regime == "caution":
            score.btc_trend_ok = False
            score.reasons.append(f"BTC caution {self.regime.btc_trend_1h:+.1f}%")
        else:
            score.btc_trend_ok = False
            if trend_15m >= 2.0:
                score.reasons.append(f"BTC dump but ALT diverging +{trend_15m:.1f}%")
        
        score.symbol_trend_ok = trend_15m >= 0
        if trend_15m > 5.0:
            score.not_overbought = False
            score.reasons.append("Too extended")
        
        return score
    
    def _apply_ml_gate(self, score: EntryScore, signal: Signal) -> EntryScore:
        """Apply ML gate/boost as final step."""
        ml = self.cache.get_ml(signal.symbol)
        
        if ml and not ml.is_stale():
            score.ml_score = ml.raw_score
            score.ml_confidence = ml.confidence
            
            if ml.confidence >= settings.ml_min_confidence:
                if ml.bearish and score.total_score < settings.base_score_strict_cutoff:
                    score.ml_boost = -10
                    score.total_score += score.ml_boost
                    score.reasons.append(f"ML bearish blocks ({ml.raw_score:+.2f})")
                elif ml.bullish:
                    raw_boost = ml.raw_score * settings.ml_boost_scale
                    score.ml_boost = max(settings.ml_boost_min, min(settings.ml_boost_max, raw_boost))
                    score.total_score += score.ml_boost
                    score.reasons.append(f"ML boost +{score.ml_boost:.1f}")
                elif ml.bearish:
                    score.ml_boost = settings.ml_boost_min
                    score.total_score += score.ml_boost
        else:
            score.total_score -= 3
            score.reasons.append("ML stale (-3)")
        
        return score
    
    def get_position_size(self, base_size: float, score: EntryScore) -> float:
        """Adjust position size based on score and regime."""
        if score.total_score >= 85:
            multiplier = 1.5
        elif score.total_score >= 80:
            multiplier = 1.3
        elif score.total_score >= 70:
            multiplier = 1.1
        elif score.total_score >= 60:
            multiplier = 0.9
        elif score.total_score >= 50:
            multiplier = 0.7
        else:
            multiplier = 0.5
        
        if self.regime.regime == "caution":
            multiplier *= 0.85
        elif self.regime.regime == "risk_off":
            multiplier *= 0.65
        elif self.regime.regime == "bullish":
            multiplier *= 1.1
        
        return base_size * multiplier
