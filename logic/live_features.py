"""
Live Feature Engine

Real-time indicator computation and feature engineering for ML scoring.
Designed for fast computation on streaming candle data.

Indicators:
- RSI (momentum oscillator)
- MACD (trend + momentum)
- Bollinger Bands (volatility)
- Volume Profile (buying pressure)
- Price momentum (rate of change)
- Micro-structure (bid-ask dynamics)

Features are computed incrementally where possible for speed.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from datetime import datetime, timezone


@dataclass
class LiveIndicators:
    """Real-time computed indicators for a symbol."""
    symbol: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_ready: bool = False          # True when enough candles for all indicators
    
    # Price action
    price: float = 0.0
    price_change_1m: float = 0.0    # % change last 1 min
    price_change_5m: float = 0.0    # % change last 5 min
    price_change_15m: float = 0.0   # % change last 15 min
    trend_5m: float = 0.0           # 5m trend for thesis tracking
    trend_15m: float = 0.0          # 15m trend for thesis tracking
    
    # HIGHER TIMEFRAME DATA (1H, 1D, 5D)
    trend_1h: float = 0.0           # 1 hour trend %
    trend_4h: float = 0.0           # 4 hour trend %
    trend_1d: float = 0.0           # 1 day trend %
    trend_7d: float = 0.0           # 7 day trend %
    
    # Daily levels (support/resistance)
    daily_high: float = 0.0         # Today's high
    daily_low: float = 0.0          # Today's low
    daily_open: float = 0.0         # Today's open
    daily_range_pct: float = 0.0    # Daily range as % of price
    
    # Position in daily range
    daily_range_position: float = 0.5  # 0 = at low, 1 = at high
    
    # Weekly context
    weekly_high: float = 0.0        # Week high
    weekly_low: float = 0.0         # Week low
    week_range_position: float = 0.5   # Position in weekly range
    
    # Higher TF momentum
    rsi_1h: float = 50.0            # Hourly RSI
    rsi_1d: float = 50.0            # Daily RSI
    
    # Volume context
    volume_vs_daily_avg: float = 1.0   # Current vs 24h average
    is_high_volume_day: bool = False   # Today's vol > avg
    
    # Momentum
    rsi_14: float = 50.0            # RSI 14 period
    rsi_7: float = 50.0             # RSI 7 period (faster)
    momentum_10: float = 0.0        # Rate of change 10 periods
    
    # Trend
    macd_line: float = 0.0          # MACD line
    macd_signal: float = 0.0        # Signal line
    macd_histogram: float = 0.0     # MACD - Signal
    ema_cross: int = 0              # 1 = bullish cross, -1 = bearish, 0 = none
    ema9: float = 0.0               # EMA 9
    ema21: float = 0.0              # EMA 21
    
    # Volatility
    bb_upper: float = 0.0           # Bollinger upper band
    bb_middle: float = 0.0          # Bollinger middle (SMA20)
    bb_lower: float = 0.0           # Bollinger lower band
    bb_width: float = 0.0           # Band width (volatility)
    bb_position: float = 0.5        # Position within bands (0-1)
    atr_pct: float = 0.0            # ATR as % of price
    atr: float = 0.0                # Raw ATR value
    
    # Volume
    volume_ratio: float = 1.0       # Current vs avg volume
    volume_trend: float = 0.0       # Volume momentum
    obv_slope: float = 0.0          # On-Balance Volume slope
    buy_pressure: float = 0.5       # Estimated buying pressure
    
    # Micro-structure
    spread_bps: float = 0.0         # Bid-ask spread
    vwap_distance: float = 0.0      # Distance from VWAP %
    vwap: float = 0.0               # Current VWAP
    
    # PRO: Order Flow Analysis
    bid_ask_imbalance: float = 0.0  # -1 to +1, positive = more bids (bullish)
    tick_direction: int = 0         # +1 uptick, -1 downtick, 0 neutral
    large_trade_bias: float = 0.0   # Bias from large trades (-1 to +1)
    
    # PRO: Volatility Regime
    volatility_percentile: float = 0.5  # Current vol vs 24h range (0-1)
    is_volatility_expanding: bool = False  # True if ATR growing
    
    # PRO: Momentum Quality
    rsi_divergence: int = 0         # +1 bullish div, -1 bearish div, 0 none
    price_momentum_align: bool = True  # Price and momentum agree
    
    # Chop detection
    is_choppy: bool = False         # True if price action is choppy
    chop_score: float = 0.0         # 0 = clean, 1 = very choppy
    ema_crosses_10: int = 0         # EMA 9/21 crosses in last 10 candles
    directional_ratio: float = 0.5  # % of candles in trend direction
    
    def is_stale(self, max_age_seconds: float = 120) -> bool:
        """Check if indicators are stale (older than max_age_seconds)."""
        age = (datetime.now(timezone.utc) - self.timestamp).total_seconds()
        return age > max_age_seconds


@dataclass
class FeatureState:
    """
    Rolling state for O(1) incremental updates.
    Stores all values needed to update indicators without full recompute.
    """
    symbol: str
    candle_count: int = 0
    min_candles: int = 10           # Minimum for "ready" (10 = ~10 min warmup)
    
    # Rolling price window (last 20 closes)
    closes: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    volumes: List[float] = field(default_factory=list)
    max_window: int = 30            # Keep last 30 for calculations
    
    # EMA values (updated incrementally)
    ema9: float = 0.0
    ema21: float = 0.0
    ema12: float = 0.0              # For MACD
    ema26: float = 0.0              # For MACD
    ema9_signal: float = 0.0        # MACD signal line
    
    # RSI state (Wilder's smoothing)
    avg_gain: float = 0.0
    avg_loss: float = 0.0
    prev_close: float = 0.0
    
    # ATR state (rolling)
    atr: float = 0.0
    prev_atr: float = 0.0
    
    # Volume state
    vol_sum_20: float = 0.0
    obv: float = 0.0
    obv_history: List[float] = field(default_factory=list)
    
    # Chop tracking
    ema_cross_history: List[int] = field(default_factory=list)  # 1=above, -1=below
    direction_history: List[int] = field(default_factory=list)   # 1=green, -1=red
    
    # 5m aggregation
    candles_since_5m: int = 0
    open_5m: float = 0.0
    high_5m: float = 0.0
    low_5m: float = 0.0
    vol_5m: float = 0.0
    closes_5m: List[float] = field(default_factory=list)
    
    # Higher timeframe data (1H, 1D)
    closes_1h: List[float] = field(default_factory=list)
    highs_1h: List[float] = field(default_factory=list)
    lows_1h: List[float] = field(default_factory=list)
    closes_1d: List[float] = field(default_factory=list)
    highs_1d: List[float] = field(default_factory=list)
    lows_1d: List[float] = field(default_factory=list)
    
    @property
    def is_ready(self) -> bool:
        return self.candle_count >= self.min_candles


@dataclass 
class LiveMLResult:
    """ML scoring result."""
    symbol: str
    raw_score: float = 0.0          # -1 to +1
    confidence: float = 0.0         # 0 to 1
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def bullish(self) -> bool:
        return self.raw_score > 0.3 and self.confidence >= 0.5
    
    @property
    def bearish(self) -> bool:
        return self.raw_score < -0.3 and self.confidence >= 0.5
    
    def is_stale(self, max_age_seconds: float = 180) -> bool:
        """Check if ML result is stale (older than max_age_seconds)."""
        age = (datetime.now(timezone.utc) - self.timestamp).total_seconds()
        return age > max_age_seconds
    
    @property
    def age_seconds(self) -> float:
        """Get age of this ML result in seconds."""
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()


class LiveFeatureEngine:
    """
    Computes live indicators and ML features from candle data.
    
    Optimized for real-time with incremental updates.
    """
    
    def __init__(self):
        # Incremental state per symbol
        self.state: Dict[str, FeatureState] = {}
        self.latest: Dict[str, LiveIndicators] = {}
        
        # Legacy caches (for compute() backward compat)
        self._rsi_gains: Dict[str, List[float]] = {}
        self._rsi_losses: Dict[str, List[float]] = {}
        self._ema_cache: Dict[str, Dict[str, float]] = {}
        self._volume_ma: Dict[str, float] = {}
        self._last_price: Dict[str, float] = {}
        self._obv: Dict[str, float] = {}
    
    def update(
        self,
        symbol: str,
        candle_1m,  # Single new candle
        spread_bps: float = 0.0,
        vwap: float = 0.0
    ) -> Optional[LiveIndicators]:
        """
        Incremental O(1) update from a single new candle.
        Returns LiveIndicators if ready, None if still warming up.
        """
        # Get or create state
        if symbol not in self.state:
            self.state[symbol] = FeatureState(symbol=symbol)
        
        s = self.state[symbol]
        s.candle_count += 1
        
        price = candle_1m.close
        high = candle_1m.high
        low = candle_1m.low
        volume = candle_1m.volume
        is_green = candle_1m.close >= candle_1m.open
        
        # Update rolling windows
        s.closes.append(price)
        s.highs.append(high)
        s.lows.append(low)
        s.volumes.append(volume)
        
        if len(s.closes) > s.max_window:
            s.closes = s.closes[-s.max_window:]
            s.highs = s.highs[-s.max_window:]
            s.lows = s.lows[-s.max_window:]
            s.volumes = s.volumes[-s.max_window:]
        
        # Update EMAs incrementally
        if s.candle_count == 1:
            s.ema9 = s.ema21 = s.ema12 = s.ema26 = price
        else:
            s.ema9 = self._update_ema(s.ema9, price, 9)
            s.ema21 = self._update_ema(s.ema21, price, 21)
            s.ema12 = self._update_ema(s.ema12, price, 12)
            s.ema26 = self._update_ema(s.ema26, price, 26)
        
        # Update MACD signal line
        macd = s.ema12 - s.ema26
        if s.candle_count == 1:
            s.ema9_signal = macd
        else:
            s.ema9_signal = self._update_ema(s.ema9_signal, macd, 9)
        
        # Update RSI (Wilder's smoothing)
        if s.prev_close > 0:
            change = price - s.prev_close
            gain = max(0, change)
            loss = max(0, -change)
            
            if s.candle_count <= 14:
                s.avg_gain = (s.avg_gain * (s.candle_count - 1) + gain) / s.candle_count
                s.avg_loss = (s.avg_loss * (s.candle_count - 1) + loss) / s.candle_count
            else:
                s.avg_gain = (s.avg_gain * 13 + gain) / 14
                s.avg_loss = (s.avg_loss * 13 + loss) / 14
        s.prev_close = price
        
        # Update ATR
        if len(s.closes) >= 2:
            prev_close = s.closes[-2]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            if s.atr == 0:
                s.atr = tr
            else:
                s.atr = (s.atr * 13 + tr) / 14
        
        # Update OBV
        if len(s.closes) >= 2:
            if price > s.closes[-2]:
                s.obv += volume
            elif price < s.closes[-2]:
                s.obv -= volume
        s.obv_history.append(s.obv)
        if len(s.obv_history) > 10:
            s.obv_history = s.obv_history[-10:]
        
        # Update chop tracking
        ema_position = 1 if s.ema9 > s.ema21 else -1
        s.ema_cross_history.append(ema_position)
        s.direction_history.append(1 if is_green else -1)
        if len(s.ema_cross_history) > 10:
            s.ema_cross_history = s.ema_cross_history[-10:]
            s.direction_history = s.direction_history[-10:]
        
        # Track 5m aggregation
        s.candles_since_5m += 1
        if s.candles_since_5m == 1:
            s.open_5m = candle_1m.open
            s.high_5m = high
            s.low_5m = low
            s.vol_5m = volume
        else:
            s.high_5m = max(s.high_5m, high)
            s.low_5m = min(s.low_5m, low)
            s.vol_5m += volume
        
        if s.candles_since_5m >= 5:
            s.closes_5m.append(price)
            if len(s.closes_5m) > 12:
                s.closes_5m = s.closes_5m[-12:]
            s.candles_since_5m = 0
        
        # Not ready yet
        if not s.is_ready:
            return None
        
        # Build indicators snapshot
        ind = LiveIndicators(symbol=symbol, is_ready=True)
        ind.price = price
        ind.spread_bps = spread_bps
        ind.vwap = vwap
        ind.vwap_distance = (price / vwap - 1) * 100 if vwap > 0 else 0
        
        # Price changes
        if len(s.closes) >= 2:
            ind.price_change_1m = (price / s.closes[-2] - 1) * 100
        if len(s.closes) >= 6:
            ind.price_change_5m = (price / s.closes[-6] - 1) * 100
        if len(s.closes) >= 16:
            ind.price_change_15m = (price / s.closes[-16] - 1) * 100
        
        # Trends
        if len(s.closes_5m) >= 2:
            ind.trend_5m = (s.closes_5m[-1] / s.closes_5m[-2] - 1) * 100
        if len(s.closes_5m) >= 4:
            ind.trend_15m = (s.closes_5m[-1] / s.closes_5m[-4] - 1) * 100
        
        # EMAs
        ind.ema9 = s.ema9
        ind.ema21 = s.ema21
        
        # MACD
        ind.macd_line = s.ema12 - s.ema26
        ind.macd_signal = s.ema9_signal
        ind.macd_histogram = ind.macd_line - ind.macd_signal
        
        # RSI
        if s.avg_loss > 0:
            rs = s.avg_gain / s.avg_loss
            ind.rsi_14 = 100 - (100 / (1 + rs))
        else:
            ind.rsi_14 = 100 if s.avg_gain > 0 else 50
        
        # ATR
        ind.atr = s.atr
        ind.atr_pct = (s.atr / price) * 100 if price > 0 else 0
        
        # Bollinger Bands
        if len(s.closes) >= 20:
            sma20 = sum(s.closes[-20:]) / 20
            variance = sum((c - sma20) ** 2 for c in s.closes[-20:]) / 20
            std20 = variance ** 0.5
            ind.bb_middle = sma20
            ind.bb_upper = sma20 + 2 * std20
            ind.bb_lower = sma20 - 2 * std20
            ind.bb_width = (ind.bb_upper - ind.bb_lower) / sma20 if sma20 > 0 else 0
            if ind.bb_upper > ind.bb_lower:
                ind.bb_position = (price - ind.bb_lower) / (ind.bb_upper - ind.bb_lower)
                ind.bb_position = max(0, min(1, ind.bb_position))
        
        # Volume
        if len(s.volumes) >= 20:
            avg_vol = sum(s.volumes[-20:]) / 20
            ind.volume_ratio = volume / avg_vol if avg_vol > 0 else 1
        
        # OBV slope
        if len(s.obv_history) >= 5:
            x = list(range(len(s.obv_history)))
            y = s.obv_history
            n = len(x)
            slope = (n * sum(xi*yi for xi, yi in zip(x, y)) - sum(x) * sum(y)) / (n * sum(xi**2 for xi in x) - sum(x)**2 + 0.001)
            avg_vol = sum(s.volumes[-5:]) / 5 if s.volumes else 1
            ind.obv_slope = slope / (avg_vol + 1)
        
        # Chop detection
        if len(s.ema_cross_history) >= 5:
            crosses = 0
            for i in range(1, len(s.ema_cross_history)):
                if s.ema_cross_history[i] != s.ema_cross_history[i-1]:
                    crosses += 1
            ind.ema_crosses_10 = crosses
        
        if len(s.direction_history) >= 10:
            green_count = sum(1 for d in s.direction_history if d > 0)
            ind.directional_ratio = green_count / len(s.direction_history)
        
        ind.chop_score = min(1.0, (ind.ema_crosses_10 / 4) + max(0, 0.5 - ind.directional_ratio))
        ind.is_choppy = ind.chop_score > 0.5 or ind.ema_crosses_10 >= 3
        
        # Buy pressure
        if high > low:
            ind.buy_pressure = (price - low) / (high - low)
        
        # EMA cross signal
        if len(s.ema_cross_history) >= 2:
            if s.ema_cross_history[-1] == 1 and s.ema_cross_history[-2] == -1:
                ind.ema_cross = 1
            elif s.ema_cross_history[-1] == -1 and s.ema_cross_history[-2] == 1:
                ind.ema_cross = -1
        
        # === HIGHER TIMEFRAME INDICATORS ===
        
        # 1H trend (from hourly candles)
        if len(s.closes_1h) >= 2:
            ind.trend_1h = (s.closes_1h[-1] / s.closes_1h[-2] - 1) * 100
        if len(s.closes_1h) >= 4:
            ind.trend_4h = (s.closes_1h[-1] / s.closes_1h[-4] - 1) * 100
        
        # Daily trend (from daily candles)
        if len(s.closes_1d) >= 2:
            ind.trend_1d = (s.closes_1d[-1] / s.closes_1d[-2] - 1) * 100
        if len(s.closes_1d) >= 7:
            ind.trend_7d = (s.closes_1d[-1] / s.closes_1d[-7] - 1) * 100
        
        # Daily high/low/range
        if s.highs_1d and s.lows_1d:
            ind.daily_high = s.highs_1d[-1] if s.highs_1d else 0
            ind.daily_low = s.lows_1d[-1] if s.lows_1d else 0
            ind.daily_open = s.closes_1d[-2] if len(s.closes_1d) >= 2 else s.closes_1d[-1] if s.closes_1d else 0
            
            daily_range = ind.daily_high - ind.daily_low
            if daily_range > 0:
                ind.daily_range_pct = (daily_range / ind.daily_low) * 100
                ind.daily_range_position = (price - ind.daily_low) / daily_range
                ind.daily_range_position = max(0, min(1, ind.daily_range_position))
        
        # Weekly high/low/range (from last 7 daily candles)
        if len(s.highs_1d) >= 7 and len(s.lows_1d) >= 7:
            ind.weekly_high = max(s.highs_1d[-7:])
            ind.weekly_low = min(s.lows_1d[-7:])
            week_range = ind.weekly_high - ind.weekly_low
            if week_range > 0:
                ind.week_range_position = (price - ind.weekly_low) / week_range
                ind.week_range_position = max(0, min(1, ind.week_range_position))
        
        # Hourly RSI (simplified from closes_1h)
        if len(s.closes_1h) >= 15:
            gains = []
            losses = []
            for i in range(1, min(15, len(s.closes_1h))):
                diff = s.closes_1h[-i] - s.closes_1h[-i-1]
                if diff > 0:
                    gains.append(diff)
                else:
                    losses.append(abs(diff))
            avg_gain = sum(gains) / 14 if gains else 0
            avg_loss = sum(losses) / 14 if losses else 0.001
            if avg_loss > 0:
                rs = avg_gain / avg_loss
                ind.rsi_1h = 100 - (100 / (1 + rs))
        
        # Cache and return
        self.latest[symbol] = ind
        return ind
    
    def _update_ema(self, prev_ema: float, price: float, period: int) -> float:
        """Update EMA with new price."""
        mult = 2 / (period + 1)
        return (price - prev_ema) * mult + prev_ema
    
    def get_latest(self, symbol: str) -> Optional[LiveIndicators]:
        """Get cached latest indicators for symbol."""
        return self.latest.get(symbol)
    
    def update_higher_tf(self, symbol: str, candles_1h: List, candles_1d: List):
        """Update higher timeframe data from buffer candles."""
        if symbol not in self.state:
            self.state[symbol] = FeatureState(symbol=symbol)
        
        s = self.state[symbol]
        
        # Extract closes, highs, lows from 1H candles
        if candles_1h:
            s.closes_1h = [c.close for c in candles_1h[-48:]]
            s.highs_1h = [c.high for c in candles_1h[-48:]]
            s.lows_1h = [c.low for c in candles_1h[-48:]]
        
        # Extract closes, highs, lows from 1D candles
        if candles_1d:
            s.closes_1d = [c.close for c in candles_1d[-30:]]
            s.highs_1d = [c.high for c in candles_1d[-30:]]
            s.lows_1d = [c.low for c in candles_1d[-30:]]
    
    def is_ready(self, symbol: str) -> bool:
        """Check if symbol has enough data for indicators."""
        return symbol in self.state and self.state[symbol].is_ready
    
    def compute(
        self, 
        symbol: str,
        candles_1m: List,
        candles_5m: List,
        spread_bps: float = 0.0,
        vwap: float = 0.0
    ) -> LiveIndicators:
        """Compute all indicators from candle data."""
        
        if not candles_1m or len(candles_1m) < 2:
            return LiveIndicators(symbol=symbol)
        
        indicators = LiveIndicators(symbol=symbol)
        closes_1m = np.array([c.close for c in candles_1m])
        volumes_1m = np.array([c.volume for c in candles_1m])
        highs_1m = np.array([c.high for c in candles_1m])
        lows_1m = np.array([c.low for c in candles_1m])
        
        indicators.price = closes_1m[-1]
        
        # ===== PRICE ACTION =====
        if len(closes_1m) >= 2:
            indicators.price_change_1m = (closes_1m[-1] / closes_1m[-2] - 1) * 100
        if len(closes_1m) >= 6:
            indicators.price_change_5m = (closes_1m[-1] / closes_1m[-6] - 1) * 100
        if len(closes_1m) >= 16:
            indicators.price_change_15m = (closes_1m[-1] / closes_1m[-16] - 1) * 100
        
        # ===== RSI =====
        if len(closes_1m) >= 15:
            indicators.rsi_14 = self._compute_rsi(closes_1m, 14)
        if len(closes_1m) >= 8:
            indicators.rsi_7 = self._compute_rsi(closes_1m, 7)
        
        # ===== MOMENTUM =====
        if len(closes_1m) >= 11:
            indicators.momentum_10 = (closes_1m[-1] / closes_1m[-11] - 1) * 100
        
        # ===== MACD =====
        if len(closes_1m) >= 26:
            ema12 = self._compute_ema(closes_1m, 12)
            ema26 = self._compute_ema(closes_1m, 26)
            indicators.macd_line = ema12 - ema26
            
            # Signal line (9-period EMA of MACD)
            macd_values = []
            for i in range(26, len(closes_1m) + 1):
                e12 = self._compute_ema(closes_1m[:i], 12)
                e26 = self._compute_ema(closes_1m[:i], 26)
                macd_values.append(e12 - e26)
            
            if len(macd_values) >= 9:
                indicators.macd_signal = self._compute_ema(np.array(macd_values), 9)
                indicators.macd_histogram = indicators.macd_line - indicators.macd_signal
        
        # ===== EMA CROSS =====
        if len(closes_1m) >= 21:
            ema9 = self._compute_ema(closes_1m, 9)
            ema21 = self._compute_ema(closes_1m, 21)
            ema9_prev = self._compute_ema(closes_1m[:-1], 9)
            ema21_prev = self._compute_ema(closes_1m[:-1], 21)
            
            if ema9 > ema21 and ema9_prev <= ema21_prev:
                indicators.ema_cross = 1  # Bullish cross
            elif ema9 < ema21 and ema9_prev >= ema21_prev:
                indicators.ema_cross = -1  # Bearish cross
        
        # ===== BOLLINGER BANDS =====
        if len(closes_1m) >= 20:
            sma20 = np.mean(closes_1m[-20:])
            std20 = np.std(closes_1m[-20:])
            
            indicators.bb_middle = sma20
            indicators.bb_upper = sma20 + 2 * std20
            indicators.bb_lower = sma20 - 2 * std20
            indicators.bb_width = (indicators.bb_upper - indicators.bb_lower) / sma20 if sma20 > 0 else 0
            
            if indicators.bb_upper > indicators.bb_lower:
                indicators.bb_position = (closes_1m[-1] - indicators.bb_lower) / (indicators.bb_upper - indicators.bb_lower)
                indicators.bb_position = max(0, min(1, indicators.bb_position))
        
        # ===== ATR =====
        if len(candles_1m) >= 14:
            trs = []
            for i in range(1, min(14, len(candles_1m))):
                c = candles_1m[-i]
                prev_c = candles_1m[-i-1]
                tr = max(
                    c.high - c.low,
                    abs(c.high - prev_c.close),
                    abs(c.low - prev_c.close)
                )
                trs.append(tr)
            atr = np.mean(trs)
            indicators.atr_pct = (atr / closes_1m[-1]) * 100 if closes_1m[-1] > 0 else 0
        
        # ===== VOLUME =====
        if len(volumes_1m) >= 20:
            vol_ma = np.mean(volumes_1m[-20:])
            indicators.volume_ratio = volumes_1m[-1] / vol_ma if vol_ma > 0 else 1
            
            # Volume trend
            vol_recent = np.mean(volumes_1m[-5:])
            vol_older = np.mean(volumes_1m[-20:-5])
            indicators.volume_trend = (vol_recent / vol_older - 1) if vol_older > 0 else 0
        
        # ===== OBV SLOPE =====
        if len(closes_1m) >= 10 and len(volumes_1m) >= 10:
            obv = [0]
            for i in range(1, len(closes_1m)):
                if closes_1m[i] > closes_1m[i-1]:
                    obv.append(obv[-1] + volumes_1m[i])
                elif closes_1m[i] < closes_1m[i-1]:
                    obv.append(obv[-1] - volumes_1m[i])
                else:
                    obv.append(obv[-1])
            
            # Slope of last 10 OBV values
            if len(obv) >= 10:
                x = np.arange(10)
                y = np.array(obv[-10:])
                slope = np.polyfit(x, y, 1)[0]
                indicators.obv_slope = slope / (np.mean(volumes_1m[-10:]) + 1)  # Normalize
        
        # ===== BUY PRESSURE =====
        if len(candles_1m) >= 10:
            buy_vol = sum(
                c.volume * ((c.close - c.low) / (c.high - c.low + 0.0001))
                for c in candles_1m[-10:]
            )
            total_vol = sum(c.volume for c in candles_1m[-10:])
            indicators.buy_pressure = buy_vol / total_vol if total_vol > 0 else 0.5
        
        # ===== MICRO-STRUCTURE =====
        indicators.spread_bps = spread_bps
        if vwap > 0 and closes_1m[-1] > 0:
            indicators.vwap_distance = (closes_1m[-1] / vwap - 1) * 100
        
        return indicators
    
    def _compute_rsi(self, closes: np.ndarray, period: int = 14) -> float:
        """Compute RSI."""
        if len(closes) < period + 1:
            return 50.0
        
        deltas = np.diff(closes[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    def _compute_ema(self, data: np.ndarray, period: int) -> float:
        """Compute EMA."""
        if len(data) < period:
            return float(data[-1]) if len(data) > 0 else 0.0
        
        multiplier = 2 / (period + 1)
        ema = float(data[0])
        for price in data[1:]:
            ema = (float(price) - ema) * multiplier + ema
        return ema
    
    def to_feature_vector(self, indicators: LiveIndicators) -> np.ndarray:
        """Convert indicators to ML feature vector."""
        return np.array([
            indicators.price_change_1m,
            indicators.price_change_5m,
            indicators.price_change_15m,
            indicators.rsi_14 / 100,        # Normalize to 0-1
            indicators.rsi_7 / 100,
            indicators.momentum_10,
            indicators.macd_histogram,
            indicators.ema_cross,
            indicators.bb_position,
            indicators.bb_width,
            indicators.atr_pct,
            indicators.volume_ratio,
            indicators.volume_trend,
            indicators.obv_slope,
            indicators.buy_pressure,
            indicators.vwap_distance,
            indicators.spread_bps / 100,    # Normalize
        ])
    
    def get_feature_names(self) -> List[str]:
        """Get feature names for the vector."""
        return [
            "price_change_1m",
            "price_change_5m", 
            "price_change_15m",
            "rsi_14_norm",
            "rsi_7_norm",
            "momentum_10",
            "macd_histogram",
            "ema_cross",
            "bb_position",
            "bb_width",
            "atr_pct",
            "volume_ratio",
            "volume_trend",
            "obv_slope",
            "buy_pressure",
            "vwap_distance",
            "spread_bps_norm",
        ]


@dataclass
class MLScore:
    """ML-based entry score."""
    symbol: str
    raw_score: float = 0.0          # Model output (-1 to 1)
    confidence: float = 0.0         # Model confidence
    signals: Dict[str, float] = field(default_factory=dict)  # Component signals
    
    @property
    def bullish(self) -> bool:
        return self.raw_score > 0.3 and self.confidence > 0.5
    
    @property
    def bearish(self) -> bool:
        return self.raw_score < -0.3 and self.confidence > 0.5


class LiveScorer:
    """
    Simple live scorer using weighted features.
    
    Can be replaced with trained model later.
    Current implementation uses hand-tuned weights.
    """
    
    # Default weights (can be loaded from trained model)
    DEFAULT_WEIGHTS = {
        "price_change_1m": 0.5,      # Recent momentum
        "price_change_5m": 0.3,
        "price_change_15m": 0.2,
        "rsi_14_norm": -0.3,         # Oversold = bullish
        "rsi_7_norm": -0.2,
        "momentum_10": 0.4,
        "macd_histogram": 0.5,       # MACD signal
        "ema_cross": 1.0,            # EMA cross is strong
        "bb_position": -0.2,         # Low in BB = oversold
        "bb_width": 0.3,             # High volatility = opportunity
        "atr_pct": 0.2,
        "volume_ratio": 0.6,         # High volume = conviction
        "volume_trend": 0.4,
        "obv_slope": 0.5,            # OBV rising = accumulation
        "buy_pressure": 0.6,         # Buying pressure
        "vwap_distance": 0.3,        # Above VWAP = bullish
        "spread_bps_norm": -0.2,     # Low spread = better
    }
    
    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS
        self.feature_engine = LiveFeatureEngine()
    
    def score(
        self,
        symbol: str,
        candles_1m: List,
        candles_5m: List,
        spread_bps: float = 0.0,
        vwap: float = 0.0
    ) -> MLScore:
        """Compute ML score for a symbol."""
        
        indicators = self.feature_engine.compute(
            symbol, candles_1m, candles_5m, spread_bps, vwap
        )
        
        features = self.feature_engine.to_feature_vector(indicators)
        feature_names = self.feature_engine.get_feature_names()
        
        # Compute weighted score
        raw_score = 0.0
        signals = {}
        
        for i, name in enumerate(feature_names):
            weight = self.weights.get(name, 0.0)
            contribution = features[i] * weight
            raw_score += contribution
            signals[name] = float(features[i])
        
        # Normalize to -1 to 1
        raw_score = np.tanh(raw_score / 5)  # Scale factor
        
        # Confidence based on signal agreement
        positive_signals = sum(1 for v in signals.values() if v > 0.1)
        negative_signals = sum(1 for v in signals.values() if v < -0.1)
        total_signals = len(signals)
        
        if raw_score > 0:
            confidence = positive_signals / total_signals
        else:
            confidence = negative_signals / total_signals
        
        return MLScore(
            symbol=symbol,
            raw_score=raw_score,
            confidence=confidence,
            signals=signals
        )
    
    def score_from_indicators(self, indicators: LiveIndicators) -> LiveMLResult:
        """
        Score from pre-computed indicators (no candle pass-through).
        This is the production path - indicators computed once, scored once.
        """
        if not indicators or not indicators.is_ready:
            return LiveMLResult(symbol=indicators.symbol if indicators else "")
        
        # Build feature dict from indicators
        features = {
            "price_change_1m": indicators.price_change_1m,
            "price_change_5m": indicators.price_change_5m,
            "price_change_15m": indicators.price_change_15m,
            "rsi_14_norm": indicators.rsi_14 / 100,
            "rsi_7_norm": indicators.rsi_7 / 100,
            "momentum_10": indicators.momentum_10,
            "macd_histogram": indicators.macd_histogram,
            "ema_cross": indicators.ema_cross,
            "bb_position": indicators.bb_position,
            "bb_width": indicators.bb_width,
            "atr_pct": indicators.atr_pct,
            "volume_ratio": indicators.volume_ratio,
            "volume_trend": indicators.volume_trend,
            "obv_slope": indicators.obv_slope,
            "buy_pressure": indicators.buy_pressure,
            "vwap_distance": indicators.vwap_distance,
            "spread_bps_norm": indicators.spread_bps / 100,
        }
        
        # Compute weighted score
        raw_score = 0.0
        for name, value in features.items():
            weight = self.weights.get(name, 0.0)
            raw_score += value * weight
        
        # Normalize to -1 to 1
        raw_score = float(np.tanh(raw_score / 5))
        
        # Confidence based on signal agreement
        positive = sum(1 for v in features.values() if v > 0.1)
        negative = sum(1 for v in features.values() if v < -0.1)
        total = len(features)
        
        if raw_score > 0:
            confidence = positive / total
        else:
            confidence = negative / total
        
        return LiveMLResult(
            symbol=indicators.symbol,
            raw_score=raw_score,
            confidence=confidence
        )
    
    def load_weights(self, path: str):
        """Load trained weights from file."""
        import json
        with open(path, 'r') as f:
            self.weights = json.load(f)
    
    def save_weights(self, path: str):
        """Save current weights to file."""
        import json
        with open(path, 'w') as f:
            json.dump(self.weights, f, indent=2)


# Singleton instance
live_scorer = LiveScorer()
feature_engine = LiveFeatureEngine()
