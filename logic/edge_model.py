"""
Edge Model - Statistical edge detection for burst momentum trading.

Based on the optimal policy for small account compounding:
1. Quick invalidation (cut losers fast when thesis breaks)
2. Partial profits (TP1 to boost win rate + reduce variance)
3. Let runner work (preserve avg_win with trailing stop)

Three buckets:
1. Edge Detection - when a trade is worth taking
2. Risk Control - when to skip or cut
3. Execution Reality - what actually happens on exchange

The goal: compound from small size with strict edge discipline.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime, timezone
import numpy as np


@dataclass
class TrendAlignment:
    """Multi-timeframe trend alignment check."""
    tf_5m: float = 0.0      # 5m EMA slope or trend
    tf_15m: float = 0.0     # 15m EMA slope  
    tf_1h: float = 0.0      # 1h EMA slope
    
    @property
    def aligned_bullish(self) -> bool:
        """All timeframes pointing up."""
        return self.tf_5m > 0 and self.tf_15m > 0 and self.tf_1h >= 0
    
    @property
    def aligned_bearish(self) -> bool:
        """All timeframes pointing down."""
        return self.tf_5m < 0 and self.tf_15m < 0 and self.tf_1h <= 0
    
    @property
    def alignment_score(self) -> float:
        """Score from -3 (all bearish) to +3 (all bullish)."""
        score = 0
        if self.tf_5m > 0: score += 1
        elif self.tf_5m < 0: score -= 1
        if self.tf_15m > 0: score += 1
        elif self.tf_15m < 0: score -= 1
        if self.tf_1h > 0: score += 1
        elif self.tf_1h < 0: score -= 1
        return score


@dataclass
class VolatilityRegime:
    """Volatility regime classification."""
    atr_current: float = 0.0        # Current ATR
    atr_20_avg: float = 0.0         # 20-period avg ATR
    atr_percentile: float = 50.0    # Where current ATR sits in history
    
    @property
    def regime(self) -> str:
        """
        Classify volatility regime:
        - quiet: ATR < 30th percentile (avoid)
        - normal: 30-70th percentile (ideal)
        - hot: 70-90th percentile (good for momentum)
        - crashy: >90th percentile (avoid)
        """
        if self.atr_percentile < 30:
            return "quiet"
        elif self.atr_percentile < 70:
            return "normal"
        elif self.atr_percentile < 90:
            return "hot"
        else:
            return "crashy"
    
    @property
    def tradeable(self) -> bool:
        """Is this a good volatility regime for momentum?"""
        return self.regime in ["normal", "hot"]


@dataclass
class ChopFilter:
    """Micro-chop detection to avoid noisy price action."""
    ema_cross_count: int = 0       # EMA 9/21 crosses in last N candles
    vwap_crosses: int = 0          # VWAP crosses in last N candles
    atr_vs_range: float = 1.0      # ATR / avg candle range (< 1 = choppy)
    directional_ratio: float = 0.5  # % of candles in trend direction
    
    @property
    def is_choppy(self) -> bool:
        """Detect micro-chop (noisy, indecisive price action)."""
        # Many EMA crosses = choppy
        if self.ema_cross_count >= 3:
            return True
        # Many VWAP crosses = range-bound
        if self.vwap_crosses >= 4:
            return True
        # Low directional ratio = no clear trend
        if self.directional_ratio < 0.4:
            return True
        return False
    
    @property
    def chop_score(self) -> float:
        """0 = clean trend, 1 = extremely choppy."""
        score = 0.0
        score += min(1.0, self.ema_cross_count / 5)
        score += min(1.0, self.vwap_crosses / 6)
        score += max(0, 0.5 - self.directional_ratio)
        return min(1.0, score / 2.5)


@dataclass
class ImpulseQuality:
    """Quality assessment of the impulse/burst."""
    impulse_vs_atr: float = 0.0     # Impulse size / ATR
    close_position: float = 0.0     # Where close is in candle range (0-1)
    volume_expansion: float = 1.0   # Volume vs average
    consecutive_green: int = 0      # Streak of directional candles
    
    @property
    def quality_score(self) -> float:
        """0 = weak impulse, 1 = strong impulse."""
        score = 0.0
        
        # Impulse should be > 1.5x ATR for quality move
        if self.impulse_vs_atr >= 2.0:
            score += 0.3
        elif self.impulse_vs_atr >= 1.5:
            score += 0.2
        elif self.impulse_vs_atr >= 1.0:
            score += 0.1
        
        # Close near high = strong buying pressure
        if self.close_position >= 0.8:
            score += 0.25
        elif self.close_position >= 0.6:
            score += 0.15
        
        # Volume expansion confirms move
        if self.volume_expansion >= 3.0:
            score += 0.25
        elif self.volume_expansion >= 2.0:
            score += 0.15
        elif self.volume_expansion >= 1.5:
            score += 0.1
        
        # Consecutive directional candles = conviction
        if self.consecutive_green >= 4:
            score += 0.2
        elif self.consecutive_green >= 3:
            score += 0.1
        
        return min(1.0, score)
    
    @property
    def is_quality(self) -> bool:
        """Is this a quality impulse worth trading?"""
        return self.quality_score >= 0.4


@dataclass
class ThesisState:
    """Track whether original trade thesis is still valid."""
    entry_price: float = 0.0
    entry_trend_5m: float = 0.0
    entry_trend_15m: float = 0.0
    entry_vwap: float = 0.0
    
    current_price: float = 0.0
    current_trend_5m: float = 0.0
    current_trend_15m: float = 0.0
    current_vwap: float = 0.0
    
    def is_thesis_valid(self) -> bool:
        """
        Check if the original bullish thesis is still valid.
        
        Thesis breaks when:
        - 5m trend flips negative (short-term reversal)
        - 15m trend flips negative (medium-term reversal)
        - Price drops significantly below VWAP
        - Price action suggests distribution, not accumulation
        """
        # 5m trend flip = immediate warning
        if self.entry_trend_5m > 0 and self.current_trend_5m < -0.5:
            return False
        
        # 15m trend flip = thesis broken
        if self.entry_trend_15m > 0 and self.current_trend_15m < -0.3:
            return False
        
        # Drop significantly below VWAP = weakness
        if self.current_vwap > 0:
            vwap_distance = (self.current_price / self.current_vwap - 1) * 100
            if vwap_distance < -1.0:  # >1% below VWAP
                return False
        
        return True
    
    def invalidation_reason(self) -> Optional[str]:
        """Get reason for thesis invalidation."""
        if self.entry_trend_5m > 0 and self.current_trend_5m < -0.5:
            return "5m trend flipped bearish"
        if self.entry_trend_15m > 0 and self.current_trend_15m < -0.3:
            return "15m trend flipped bearish"
        if self.current_vwap > 0:
            vwap_distance = (self.current_price / self.current_vwap - 1) * 100
            if vwap_distance < -1.0:
                return f"Price {vwap_distance:.1f}% below VWAP"
        return None


@dataclass
class EdgeAssessment:
    """Complete edge assessment for a potential trade."""
    symbol: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Components
    trend: TrendAlignment = field(default_factory=TrendAlignment)
    volatility: VolatilityRegime = field(default_factory=VolatilityRegime)
    chop: ChopFilter = field(default_factory=ChopFilter)
    impulse: ImpulseQuality = field(default_factory=ImpulseQuality)
    
    # Universe position
    universe_rank: int = 999        # Rank in hot list (1 = best)
    top_n_threshold: int = 15       # Only trade top N
    
    # Final assessment
    edge_score: float = 0.0         # Composite edge score (0-100)
    has_edge: bool = False
    skip_reasons: List[str] = field(default_factory=list)
    
    def compute_edge(self) -> bool:
        """
        Compute whether this setup has statistical edge.
        
        Edge requires:
        1. Multi-TF trend alignment
        2. Tradeable volatility regime
        3. No micro-chop
        4. Quality impulse
        5. Top universe rank
        """
        self.skip_reasons = []
        self.edge_score = 0.0
        
        # 1. Trend alignment (25 points)
        if self.trend.aligned_bullish:
            self.edge_score += 25
        elif self.trend.alignment_score >= 2:
            self.edge_score += 15
        elif self.trend.alignment_score >= 1:
            self.edge_score += 5
        else:
            self.skip_reasons.append(f"Trend misaligned ({self.trend.alignment_score})")
        
        # 2. Volatility regime (20 points)
        if self.volatility.tradeable:
            self.edge_score += 20
            if self.volatility.regime == "hot":
                self.edge_score += 5  # Bonus for hot regime
        else:
            self.skip_reasons.append(f"Vol regime: {self.volatility.regime}")
        
        # 3. Chop filter (20 points)
        if not self.chop.is_choppy:
            self.edge_score += 20
        else:
            self.skip_reasons.append(f"Choppy (score {self.chop.chop_score:.2f})")
        
        # 4. Impulse quality (20 points)
        if self.impulse.is_quality:
            self.edge_score += 20
        elif self.impulse.quality_score >= 0.25:
            self.edge_score += 10
        else:
            self.skip_reasons.append(f"Weak impulse ({self.impulse.quality_score:.2f})")
        
        # 5. Universe rank (15 points)
        if self.universe_rank <= self.top_n_threshold:
            self.edge_score += 15
        elif self.universe_rank <= 25:
            self.edge_score += 5
        else:
            self.skip_reasons.append(f"Rank {self.universe_rank} > top {self.top_n_threshold}")
        
        # Final edge determination
        # Need at least 60 points AND no critical failures
        self.has_edge = (
            self.edge_score >= 60 and
            self.trend.alignment_score >= 1 and
            self.volatility.tradeable and
            not self.chop.is_choppy and
            self.universe_rank <= 25
        )
        
        return self.has_edge


class EdgeModel:
    """
    Edge detection engine for burst momentum trading.
    
    Implements the statistical edge model:
    - Only trade when multiple factors align
    - Quick invalidation when thesis breaks
    - Strict position management
    """
    
    def __init__(self):
        self._trend_cache: Dict[str, TrendAlignment] = {}
        self._volatility_cache: Dict[str, VolatilityRegime] = {}
        self._thesis_states: Dict[str, ThesisState] = {}
    
    def compute_trend_alignment(
        self, 
        candles_1m: List,
        candles_5m: List,
        ema_period: int = 20
    ) -> TrendAlignment:
        """Compute multi-TF trend alignment from candle data."""
        
        trend = TrendAlignment()
        
        # 5m trend from last 3 5m candles
        if len(candles_5m) >= 4:
            closes_5m = [c.close for c in candles_5m[-4:]]
            trend.tf_5m = (closes_5m[-1] / closes_5m[0] - 1) * 100
        
        # 15m trend from last 3 periods (= 9 5m candles or 15 1m candles)
        if len(candles_5m) >= 10:
            closes_15m = [c.close for c in candles_5m[-10:]]
            mid = len(closes_15m) // 2
            trend.tf_15m = (closes_15m[-1] / closes_15m[0] - 1) * 100
        elif len(candles_1m) >= 16:
            closes = [c.close for c in candles_1m[-16:]]
            trend.tf_15m = (closes[-1] / closes[0] - 1) * 100
        
        # 1h trend from 12 5m candles or 60 1m candles
        if len(candles_5m) >= 13:
            closes_1h = [c.close for c in candles_5m[-13:]]
            trend.tf_1h = (closes_1h[-1] / closes_1h[0] - 1) * 100
        elif len(candles_1m) >= 61:
            closes = [c.close for c in candles_1m[-61:]]
            trend.tf_1h = (closes[-1] / closes[0] - 1) * 100
        
        return trend
    
    def compute_volatility_regime(
        self,
        candles_1m: List,
        lookback: int = 60
    ) -> VolatilityRegime:
        """Compute volatility regime from ATR history."""
        
        vol = VolatilityRegime()
        
        if len(candles_1m) < 20:
            return vol
        
        # Compute ATR for each period
        atrs = []
        for i in range(1, min(lookback, len(candles_1m))):
            c = candles_1m[-i]
            prev = candles_1m[-i-1]
            tr = max(
                c.high - c.low,
                abs(c.high - prev.close),
                abs(c.low - prev.close)
            )
            atrs.append(tr)
        
        if not atrs:
            return vol
        
        vol.atr_current = atrs[0]
        vol.atr_20_avg = np.mean(atrs[:20]) if len(atrs) >= 20 else np.mean(atrs)
        
        # Compute percentile
        sorted_atrs = sorted(atrs)
        rank = sum(1 for a in sorted_atrs if a <= vol.atr_current)
        vol.atr_percentile = (rank / len(sorted_atrs)) * 100
        
        return vol
    
    def compute_chop_filter(
        self,
        candles_1m: List,
        vwap: float = 0.0,
        lookback: int = 20
    ) -> ChopFilter:
        """Detect micro-chop in recent price action."""
        
        chop = ChopFilter()
        
        if len(candles_1m) < lookback:
            return chop
        
        recent = candles_1m[-lookback:]
        closes = [c.close for c in recent]
        
        # EMA 9/21 cross count
        if len(closes) >= 21:
            ema9 = self._compute_ema(closes, 9)
            ema21 = self._compute_ema(closes, 21)
            
            # Count crosses in last 20 periods
            crosses = 0
            for i in range(1, min(20, len(closes))):
                e9_now = self._compute_ema(closes[:len(closes)-i+1], 9)
                e21_now = self._compute_ema(closes[:len(closes)-i+1], 21)
                e9_prev = self._compute_ema(closes[:len(closes)-i], 9)
                e21_prev = self._compute_ema(closes[:len(closes)-i], 21)
                if (e9_now > e21_now) != (e9_prev > e21_prev):
                    crosses += 1
            chop.ema_cross_count = crosses
        
        # VWAP cross count
        if vwap > 0:
            crosses = 0
            for i in range(1, len(recent)):
                if (recent[i].close > vwap) != (recent[i-1].close > vwap):
                    crosses += 1
            chop.vwap_crosses = crosses
        
        # Directional ratio (% of green candles)
        green = sum(1 for c in recent if c.close >= c.open)
        chop.directional_ratio = green / len(recent)
        
        return chop
    
    def compute_impulse_quality(
        self,
        candles_1m: List,
        impulse_pct: float,
        atr: float
    ) -> ImpulseQuality:
        """Assess quality of the impulse move."""
        
        quality = ImpulseQuality()
        
        if not candles_1m or atr <= 0:
            return quality
        
        # Impulse vs ATR
        impulse_abs = abs(impulse_pct / 100 * candles_1m[-1].close)
        quality.impulse_vs_atr = impulse_abs / atr
        
        # Close position in last candle
        last = candles_1m[-1]
        if last.high > last.low:
            quality.close_position = (last.close - last.low) / (last.high - last.low)
        
        # Volume expansion
        if len(candles_1m) >= 20:
            avg_vol = np.mean([c.volume for c in candles_1m[-20:-1]])
            quality.volume_expansion = last.volume / avg_vol if avg_vol > 0 else 1.0
        
        # Consecutive green candles
        consecutive = 0
        for c in reversed(candles_1m[-10:]):
            if c.close >= c.open:
                consecutive += 1
            else:
                break
        quality.consecutive_green = consecutive
        
        return quality
    
    def assess_edge(
        self,
        symbol: str,
        candles_1m: List,
        candles_5m: List,
        universe_rank: int,
        impulse_pct: float = 0.0,
        vwap: float = 0.0,
        atr: float = 0.0
    ) -> EdgeAssessment:
        """
        Complete edge assessment for a potential trade.
        
        Returns EdgeAssessment with has_edge = True/False and reasons.
        """
        
        assessment = EdgeAssessment(symbol=symbol)
        assessment.universe_rank = universe_rank
        
        # Compute all components
        assessment.trend = self.compute_trend_alignment(candles_1m, candles_5m)
        assessment.volatility = self.compute_volatility_regime(candles_1m)
        assessment.chop = self.compute_chop_filter(candles_1m, vwap)
        assessment.impulse = self.compute_impulse_quality(candles_1m, impulse_pct, atr)
        
        # Compute final edge
        assessment.compute_edge()
        
        return assessment
    
    def create_thesis_state(
        self,
        symbol: str,
        entry_price: float,
        candles_1m: List,
        candles_5m: List,
        vwap: float
    ) -> ThesisState:
        """Create thesis state at entry for later invalidation check."""
        
        trend = self.compute_trend_alignment(candles_1m, candles_5m)
        
        thesis = ThesisState(
            entry_price=entry_price,
            entry_trend_5m=trend.tf_5m,
            entry_trend_15m=trend.tf_15m,
            entry_vwap=vwap,
            current_price=entry_price,
            current_trend_5m=trend.tf_5m,
            current_trend_15m=trend.tf_15m,
            current_vwap=vwap
        )
        
        self._thesis_states[symbol] = thesis
        return thesis
    
    def update_thesis(
        self,
        symbol: str,
        current_price: float,
        candles_1m: List,
        candles_5m: List,
        vwap: float
    ) -> Optional[str]:
        """
        Update thesis state and check for invalidation.
        
        Returns invalidation reason if thesis broken, None if still valid.
        """
        
        if symbol not in self._thesis_states:
            return None
        
        thesis = self._thesis_states[symbol]
        trend = self.compute_trend_alignment(candles_1m, candles_5m)
        
        thesis.current_price = current_price
        thesis.current_trend_5m = trend.tf_5m
        thesis.current_trend_15m = trend.tf_15m
        thesis.current_vwap = vwap
        
        if not thesis.is_thesis_valid():
            reason = thesis.invalidation_reason()
            return reason
        
        return None
    
    def clear_thesis(self, symbol: str):
        """Clear thesis state when position closed."""
        if symbol in self._thesis_states:
            del self._thesis_states[symbol]
    
    def _compute_ema(self, data: List[float], period: int) -> float:
        """Compute EMA."""
        if len(data) < period:
            return data[-1] if data else 0.0
        
        multiplier = 2 / (period + 1)
        ema = data[0]
        for price in data[1:]:
            ema = (price - ema) * multiplier + ema
        return ema


# Singleton instance
edge_model = EdgeModel()
