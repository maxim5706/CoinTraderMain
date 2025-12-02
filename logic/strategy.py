"""Burst-flag strategy with trap avoidance."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
import numpy as np

from core.config import settings
from core.models import (
    CandleBuffer, Candle, Signal, SignalType, 
    ImpulseLeg, FlagPattern, Side
)


@dataclass
class SwingHigh:
    """Detected swing high for trap detection."""
    price: float
    timestamp: datetime
    confirmed: bool = False


class BurstFlagStrategy:
    """
    Strategy that:
    1. Detects burst candidates (volume + volatility spike)
    2. Identifies impulse legs
    3. Waits for bull flag formation
    4. Avoids triple tops and head & shoulders
    5. Enters on flag breakout with confirmation
    """
    
    def __init__(self):
        self.impulses: dict[str, Optional[ImpulseLeg]] = {}
        self.flags: dict[str, Optional[FlagPattern]] = {}
        self.swing_highs: dict[str, list[SwingHigh]] = {}
        self.trap_zones: dict[str, bool] = {}  # True if in trap zone
    
    def analyze(self, symbol: str, buffer: CandleBuffer, spread_bps: float = 999.0) -> Signal:
        """Main analysis function - returns a signal.
        
        Args:
            symbol: Trading pair
            buffer: Candle data buffer
            spread_bps: Current bid-ask spread in basis points (for FAST mode)
        """
        self._current_spread_bps = spread_bps  # Store for _generate_entry_signal
        
        # Need minimum data (keep short to react early)
        if len(buffer.candles_1m) < 10 or len(buffer.candles_5m) < 3:
            return Signal(
                symbol=symbol,
                type=SignalType.NONE,
                timestamp=datetime.now(timezone.utc),
                price=buffer.last_price,
                reason="Insufficient data"
            )
        
        # Update swing highs for trap detection
        self._update_swing_highs(symbol, buffer)
        
        # Check for traps first
        trap_signal = self._check_traps(symbol, buffer)
        if trap_signal:
            return trap_signal
        
        # Check for burst
        burst = self._detect_burst(symbol, buffer)
        if not burst:
            return Signal(
                symbol=symbol,
                type=SignalType.NONE,
                timestamp=datetime.now(timezone.utc),
                price=buffer.last_price,
                reason="No burst detected"
            )
        
        # Check for impulse leg
        impulse = self._detect_impulse(symbol, buffer)
        if impulse is None:
            return Signal(
                symbol=symbol,
                type=SignalType.BURST_DETECTED,
                timestamp=datetime.now(timezone.utc),
                price=buffer.last_price,
                reason="Burst detected, waiting for impulse"
            )
        
        self.impulses[symbol] = impulse
        
        # FAST MODE: Impulse can trigger directly without flag
        # If impulse meets minimum and we have tight spread, enter on momentum
        spread_bps = getattr(self, '_current_spread_bps', 999.0)
        momentum_entry = (
            settings.fast_mode_enabled and
            impulse.pct_move >= settings.impulse_min_pct and  # Use config threshold (1.0%)
            spread_bps <= settings.fast_spread_max_bps and
            buffer.last_price >= impulse.high * 0.99  # Within 1% of highs
        )
        
        if momentum_entry:
            # Create synthetic flag at current level for entry signal
            synthetic_flag = FlagPattern(
                start_time=datetime.now(timezone.utc) - timedelta(minutes=1),
                high=impulse.high,
                low=buffer.last_price * 0.99,
                retrace_pct=0.1,
                duration_minutes=1,
                slope=0.0,
                avg_volume=impulse.avg_volume * 0.5
            )
            self.flags[symbol] = synthetic_flag
            return self._generate_entry_signal(symbol, buffer, impulse, synthetic_flag)
        
        # Check for flag formation
        flag = self._detect_flag(symbol, buffer, impulse)
        if flag is None:
            return Signal(
                symbol=symbol,
                type=SignalType.IMPULSE_FOUND,
                timestamp=datetime.now(timezone.utc),
                price=buffer.last_price,
                impulse=impulse,
                reason=f"Impulse +{impulse.pct_move:.1f}%, waiting for flag"
            )
        
        self.flags[symbol] = flag
        
        # Check for breakout
        breakout = self._check_breakout(symbol, buffer, flag)
        if not breakout:
            return Signal(
                symbol=symbol,
                type=SignalType.FLAG_FORMING,
                timestamp=datetime.now(timezone.utc),
                price=buffer.last_price,
                impulse=impulse,
                flag=flag,
                reason="Flag forming, waiting for breakout"
            )
        
        # BREAKOUT SIGNAL!
        return self._generate_entry_signal(symbol, buffer, impulse, flag)
    
    def _detect_burst(self, symbol: str, buffer: CandleBuffer) -> bool:
        """Detect volume + volatility burst."""
        volumes = buffer.get_volumes("5m")
        ranges = buffer.get_ranges("5m")
        
        # Early fallback: if we don't have enough 5m history yet, use 1m burst check
        if len(volumes) < 6:
            vol_1m = buffer.get_volumes("1m")
            rng_1m = buffer.get_ranges("1m")
            if len(vol_1m) < 12:
                return False
            vol_current = vol_1m[-1]
            vol_median = np.median(vol_1m[-30:]) if len(vol_1m) >= 30 else np.median(vol_1m)
            range_current = rng_1m[-1]
            range_median = np.median(rng_1m[-30:]) if len(rng_1m) >= 30 else np.median(rng_1m)
        else:
            # Current vs median (5m)
            vol_current = volumes[-1]
            vol_median = np.median(volumes[-24:]) if len(volumes) >= 24 else np.median(volumes)
            range_current = ranges[-1]
            range_median = np.median(ranges[-24:]) if len(ranges) >= 24 else np.median(ranges)
        
        if vol_median == 0 or range_median == 0:
            return False
        
        vol_spike = vol_current / vol_median
        range_spike = range_current / range_median
        
        # Check VWAP filter
        price = buffer.last_price
        vwap = buffer.vwap(periods=120)  # 2h VWAP
        
        # Gate 1: Both metrics above thresholds
        both_above = (
            vol_spike >= settings.vol_spike_threshold and
            range_spike >= settings.range_spike_threshold
        )
        
        # Gate 2: Very high volume (3x+) can compensate for lower range
        vol_dominant = vol_spike >= 3.0 and range_spike >= 0.8
        
        # Gate 3: Combined score approach - total activity matters
        combined_score = vol_spike + range_spike
        combined_pass = combined_score >= 2.5 and vol_spike >= 1.3
        
        is_burst = both_above or vol_dominant or combined_pass
        
        # Allow price at or above VWAP (>= not >)
        return price >= vwap * 0.998 and is_burst  # Within 0.2% of VWAP is OK
    
    def _detect_impulse(self, symbol: str, buffer: CandleBuffer) -> Optional[ImpulseLeg]:
        """Detect clean impulse leg up."""
        candles_5m = buffer.candles_5m
        if len(candles_5m) < 5:  # Need at least 25 min history
            return None
        
        # Look at last 5-9 candles (25-45 min) depending on availability
        lookback = min(len(candles_5m), 9)
        recent = candles_5m[-lookback:]
        
        # Find start of impulse (local low)
        lows = [c.low for c in recent]
        low_idx = int(np.argmin(lows))
        
        if low_idx >= len(recent) - 2:  # Impulse should have completed somewhat
            return None
        
        impulse_start = recent[low_idx]
        impulse_end = recent[-1]
        
        low_price = impulse_start.low
        high_price = max(c.high for c in recent[low_idx:])
        
        if low_price == 0:
            return None
        
        pct_move = ((high_price - low_price) / low_price) * 100
        
        if pct_move < settings.impulse_min_pct:
            return None
        
        # Check for green candles (relaxed - just need majority green)
        impulse_candles = recent[low_idx:]
        green_count = sum(1 for c in impulse_candles if c.is_green)
        
        if green_count < 2:  # Reduced from 3 - faster detection
            return None
        
        # Skip EMA check for faster reactions (price above VWAP is checked in burst)
        
        return ImpulseLeg(
            start_time=impulse_start.timestamp,
            end_time=impulse_end.timestamp,
            low=low_price,
            high=high_price,
            pct_move=pct_move,
            green_candles=green_count,
            avg_volume=np.mean([c.volume for c in impulse_candles])
        )
    
    def _detect_flag(self, symbol: str, buffer: CandleBuffer, impulse: ImpulseLeg) -> Optional[FlagPattern]:
        """Detect flag formation after impulse."""
        candles_1m = buffer.candles_1m
        
        if len(candles_1m) < 10:
            return None
        
        # Look for consolidation after impulse high
        high_time = impulse.end_time
        
        # Get candles after impulse
        flag_candles = [c for c in candles_1m if c.timestamp > high_time]
        
        if len(flag_candles) < 10:  # Need at least 10 min of flag
            return None
        
        if len(flag_candles) > 40:  # Too long, pattern failed
            return None
        
        flag_high = max(c.high for c in flag_candles)
        flag_low = min(c.low for c in flag_candles)
        
        # Calculate retracement
        impulse_range = impulse.high - impulse.low
        if impulse_range == 0:
            return None
        
        retrace_pct = (impulse.high - flag_low) / impulse_range
        
        # Check retracement bounds
        if not (settings.flag_retrace_min <= retrace_pct <= settings.flag_retrace_max):
            return None
        
        # Check volume decline
        impulse_vol = impulse.avg_volume
        flag_vol = np.mean([c.volume for c in flag_candles])
        
        if impulse_vol > 0 and flag_vol > impulse_vol * settings.flag_vol_decay:
            return None  # Volume not declining enough
        
        # Check slope (should be flat to slightly down)
        closes = [c.close for c in flag_candles]
        if len(closes) >= 2:
            slope = (closes[-1] - closes[0]) / len(closes) / closes[0]
        else:
            slope = 0
        
        # Check price above EMA(50)
        ema50 = buffer.ema(period=50, timeframe="5m")
        if buffer.last_price < ema50:
            return None
        
        return FlagPattern(
            start_time=flag_candles[0].timestamp,
            high=flag_high,
            low=flag_low,
            retrace_pct=retrace_pct,
            duration_minutes=len(flag_candles),
            avg_volume=flag_vol,
            slope=slope
        )
    
    def _check_breakout(self, symbol: str, buffer: CandleBuffer, flag: FlagPattern) -> bool:
        """Check if flag breakout occurred."""
        last_candle = buffer.candles_1m[-1]
        
        # Price breakout with buffer
        breakout_level = flag.high * (1 + settings.breakout_buffer_pct / 100)
        price_breakout = last_candle.close > breakout_level
        
        if not price_breakout:
            return False
        
        # Volume confirmation
        recent_vols = buffer.get_volumes("1m")
        if len(recent_vols) < 10:
            return price_breakout  # Accept without vol confirmation
        
        avg_flag_vol = np.mean(recent_vols[-10:-1])  # Exclude current
        current_vol = recent_vols[-1]
        
        vol_confirmed = current_vol >= avg_flag_vol * settings.breakout_vol_mult
        
        return price_breakout and vol_confirmed
    
    def _update_swing_highs(self, symbol: str, buffer: CandleBuffer):
        """Track swing highs for trap detection."""
        if symbol not in self.swing_highs:
            self.swing_highs[symbol] = []
        
        candles = buffer.candles_5m
        if len(candles) < 5:
            return
        
        # Simple swing high: higher than 2 candles on each side
        # Only check last ~1h of candles for performance
        start_i = max(2, len(candles) - 12)
        for i in range(start_i, len(candles) - 2):
            c = candles[i]
            is_swing = (
                c.high > candles[i-1].high and
                c.high > candles[i-2].high and
                c.high > candles[i+1].high and
                c.high > candles[i+2].high
            )
            
            if is_swing:
                # Check if we already have this swing
                existing = [sh for sh in self.swing_highs[symbol] 
                           if abs(sh.price - c.high) / c.high < 0.005]
                if not existing:
                    self.swing_highs[symbol].append(
                        SwingHigh(price=c.high, timestamp=c.timestamp, confirmed=True)
                    )
        
        # Keep only recent swing highs (last 2 hours)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        self.swing_highs[symbol] = [
            sh for sh in self.swing_highs[symbol] 
            if sh.timestamp > cutoff
        ]
    
    def _check_traps(self, symbol: str, buffer: CandleBuffer) -> Optional[Signal]:
        """Check for triple top or head & shoulders patterns."""
        
        # Triple top check
        triple_top = self._detect_triple_top(symbol, buffer)
        if triple_top:
            self.trap_zones[symbol] = True
            return Signal(
                symbol=symbol,
                type=SignalType.TRAP_TRIPLE_TOP,
                timestamp=datetime.now(timezone.utc),
                price=buffer.last_price,
                reason="Triple top detected - avoiding longs"
            )
        
        # Head & shoulders check
        hs = self._detect_head_shoulders(symbol, buffer)
        if hs:
            self.trap_zones[symbol] = True
            return Signal(
                symbol=symbol,
                type=SignalType.TRAP_HEAD_SHOULDERS,
                timestamp=datetime.now(timezone.utc),
                price=buffer.last_price,
                reason="Head & shoulders detected - avoiding longs"
            )
        
        self.trap_zones[symbol] = False
        return None
    
    def _detect_triple_top(self, symbol: str, buffer: CandleBuffer) -> bool:
        """Detect triple top pattern."""
        swings = self.swing_highs.get(symbol, [])
        
        if len(swings) < 3:
            return False
        
        # Get last 3 swing highs
        recent = sorted(swings, key=lambda x: x.timestamp)[-3:]
        prices = [sh.price for sh in recent]
        
        # Check if they're within tolerance
        avg_price = np.mean(prices)
        tolerance = avg_price * (settings.triple_top_tolerance_pct / 100)
        
        all_close = all(abs(p - avg_price) <= tolerance for p in prices)
        
        if not all_close:
            return False
        
        # Current price should be near or below these levels
        current = buffer.last_price
        return current <= avg_price * 1.005  # Within 0.5% of triple top zone
    
    def _detect_head_shoulders(self, symbol: str, buffer: CandleBuffer) -> bool:
        """Detect head & shoulders pattern."""
        swings = self.swing_highs.get(symbol, [])
        
        if len(swings) < 3:
            return False
        
        # Need 3 peaks with middle highest
        recent = sorted(swings, key=lambda x: x.timestamp)[-3:]
        
        if len(recent) != 3:
            return False
        
        left, head, right = recent
        
        # Head must be highest
        if not (head.price > left.price and head.price > right.price):
            return False
        
        # Shoulders should be similar
        shoulder_diff = abs(left.price - right.price) / left.price
        if shoulder_diff > settings.hs_shoulder_tolerance_pct / 100:
            return False
        
        # Current price should be below head and near/below shoulders
        current = buffer.last_price
        shoulder_avg = (left.price + right.price) / 2
        
        return current < head.price and current <= shoulder_avg * 1.02
    
    def _generate_entry_signal(
        self, 
        symbol: str, 
        buffer: CandleBuffer, 
        impulse: ImpulseLeg, 
        flag: FlagPattern
    ) -> Signal:
        """Generate entry signal with stops and targets."""
        
        price = buffer.last_price
        atr = buffer.atr(period=14, timeframe="1m")
        
        # V2.1 Stop geometry: use tighter of ATR-based or fixed %
        # This ensures stop is reasonable but never too wide
        stop_from_atr = price - (atr * settings.stop_atr_mult)  # 1.5× ATR
        stop_from_fixed = price * (1 - settings.fixed_stop_pct)  # 1.5% fixed floor
        stop_from_flag = flag.low * 0.998  # Below flag low
        
        # Use the tightest stop that's still below flag low
        stop_price = max(stop_from_fixed, stop_from_atr)  # At least 1.5% or 1.5×ATR
        stop_price = min(stop_price, stop_from_flag)      # But not above flag low
        
        # Ensure stop isn't too wide (max 2.5% for small account safety)
        max_stop = price * 0.975  # 2.5% max
        stop_price = max(stop_price, max_stop)
        
        # TP1: target impulse high OR fixed %, whichever gives better R:R
        tp1_from_impulse = impulse.high
        tp1_from_fixed = price * (1 + settings.tp1_pct)  # 2.5% fixed
        
        # Calculate R:R for each option
        risk = price - stop_price
        if risk > 0:
            rr_impulse = (tp1_from_impulse - price) / risk
            rr_fixed = (tp1_from_fixed - price) / risk
            # Use whichever passes min_rr_ratio, prefer impulse if both pass
            if rr_impulse >= settings.min_rr_ratio:
                tp1_price = tp1_from_impulse
            elif rr_fixed >= settings.min_rr_ratio:
                tp1_price = tp1_from_fixed
            else:
                # Neither passes, use fixed (will be caught by order_router R:R gate)
                tp1_price = tp1_from_fixed
        else:
            tp1_price = tp1_from_fixed
        
        # TP2 at impulse high + 0.5× impulse range, or 5% fixed
        impulse_range = impulse.high - impulse.low
        tp2_from_impulse = impulse.high + (impulse_range * settings.tp2_impulse_mult)
        tp2_from_fixed = price * (1 + settings.tp2_pct)  # 5% fixed
        tp2_price = max(tp2_from_impulse, tp2_from_fixed)
        
        # Calculate confidence based on pattern quality
        confidence = 0.5
        
        # Better retracement = higher confidence
        if 0.3 <= flag.retrace_pct <= 0.4:
            confidence += 0.1
        
        # Volume confirmation
        if flag.avg_volume < impulse.avg_volume * 0.5:
            confidence += 0.1
        
        # Strong impulse
        if impulse.pct_move >= 5.0:
            confidence += 0.1
        
        # Many green candles
        if impulse.green_candles >= 5:
            confidence += 0.1
        
        confidence = min(confidence, 1.0)
        
        # Check FAST breakout conditions
        is_fast = False
        if settings.fast_mode_enabled:
            spread_bps = getattr(self, '_current_spread_bps', 999.0)
            
            # FAST gate conditions
            fast_confidence = confidence >= settings.fast_confidence_min
            fast_spread = spread_bps <= settings.fast_spread_max_bps
            
            # Breakout candle quality check
            last_candle = buffer.candles_1m[-1] if buffer.candles_1m else None
            fast_candle = False
            if last_candle:
                candle_range = last_candle.high - last_candle.low
                if candle_range > 0:
                    body = abs(last_candle.close - last_candle.open)
                    body_ratio = body / candle_range
                    close_position = (last_candle.close - last_candle.low) / candle_range
                    # Clean candle: body >= 60% of range, close in top 20%
                    fast_candle = body_ratio >= 0.6 and close_position >= 0.8
            
            is_fast = fast_confidence and fast_spread and fast_candle
        
        signal_type = SignalType.FAST_BREAKOUT if is_fast else SignalType.FLAG_BREAKOUT
        reason_prefix = "FAST " if is_fast else ""
        
        return Signal(
            symbol=symbol,
            type=signal_type,
            timestamp=datetime.now(timezone.utc),
            price=price,
            confidence=confidence,
            impulse=impulse,
            flag=flag,
            stop_price=stop_price,
            tp1_price=tp1_price,
            tp2_price=tp2_price,
            reason=f"{reason_prefix}Bull flag breakout: impulse +{impulse.pct_move:.1f}%, retrace {flag.retrace_pct*100:.0f}%"
        )
    
    def reset(self, symbol: str):
        """Reset state for a symbol after trade."""
        self.impulses[symbol] = None
        self.flags[symbol] = None
