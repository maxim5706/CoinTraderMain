"""Signal and pattern definitions."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalType(Enum):
    NONE = "none"
    BURST_DETECTED = "burst_detected"
    IMPULSE_FOUND = "impulse_found"
    FLAG_FORMING = "flag_forming"
    FLAG_BREAKOUT = "flag_breakout"
    FAST_BREAKOUT = "fast_breakout"  # High-conviction scalp
    TRAP_TRIPLE_TOP = "trap_triple_top"
    TRAP_HEAD_SHOULDERS = "trap_head_shoulders"


@dataclass
class ImpulseLeg:
    """Detected impulse leg info."""
    start_time: datetime
    end_time: datetime
    low: float
    high: float
    pct_move: float
    green_candles: int
    avg_volume: float
    
    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class FlagPattern:
    """Detected flag pattern info."""
    start_time: datetime
    high: float  # Flag high
    low: float   # Flag low
    retrace_pct: float
    duration_minutes: int
    avg_volume: float
    slope: float  # Slightly down is good
    
    @property
    def is_valid(self) -> bool:
        return (
            0.2 <= self.retrace_pct <= 0.5 and
            10 <= self.duration_minutes <= 40 and
            self.slope <= 0.001  # Flat to slightly down
        )


@dataclass
class Signal:
    """Trading signal."""
    symbol: str
    type: SignalType
    timestamp: datetime
    price: float
    strategy_id: str = ""  # Source strategy identifier
    confidence: float = 0.0
    impulse: Optional[ImpulseLeg] = None
    flag: Optional[FlagPattern] = None
    stop_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    reason: str = ""
    
    # Burst metrics for intelligence scoring
    vol_spike: float = 1.0
    range_spike: float = 1.0
    trend_15m: float = 0.0
    vwap_distance: float = 0.0
    spread_bps: float = 50.0
    tier: str = "unknown"

