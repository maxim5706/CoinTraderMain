"""Position and side enums."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class PositionState(Enum):
    FLAT = "flat"
    PENDING = "pending"
    OPEN = "open"
    CLOSING = "closing"


@dataclass
class Position:
    """Open position tracking with play-based confidence."""
    symbol: str
    side: Side
    entry_price: float
    entry_time: datetime
    size_usd: float       # Current market value (updates with price)
    size_qty: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    time_stop_min: int = 0
    state: PositionState = PositionState.OPEN
    realized_pnl: float = 0.0
    partial_closed: bool = False
    strategy_id: str = ""  # Source strategy for tracking
    entry_cost_usd: float = 0.0  # Original cost at entry (doesn't change!)
    
    # Play-based confidence tracking
    entry_confidence: float = 0.0   # Confidence at entry (0-100)
    current_confidence: float = 0.0 # Updated confidence (recalculated each cycle)
    peak_confidence: float = 0.0    # Highest confidence seen during play
    ml_score_entry: float = 0.0     # ML score at entry
    ml_score_current: float = 0.0   # Current ML score
    
    # Trailing stop
    highest_price: float = 0.0      # Highest price seen since entry
    trailing_stop_pct: float = 0.0  # Trailing stop % (0 = disabled)
    trailing_active: bool = False   # Is trailing stop active?
    
    @property
    def confidence_trend(self) -> str:
        """Is confidence rising, falling, or stable?"""
        if self.current_confidence > self.entry_confidence * 1.1:
            return "rising"
        elif self.current_confidence < self.entry_confidence * 0.8:
            return "falling"
        return "stable"
    
    @property
    def play_quality(self) -> str:
        """Overall play quality assessment."""
        if self.current_confidence >= 80 and self.confidence_trend != "falling":
            return "strong"
        elif self.current_confidence < 50 or self.confidence_trend == "falling":
            return "weak"
        return "neutral"
    
    @property
    def cost_basis(self) -> float:
        """Original cost at entry - use for budget calculations."""
        if self.entry_cost_usd > 0:
            return self.entry_cost_usd
        return self.entry_price * self.size_qty
    
    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == Side.BUY:
            return (current_price - self.entry_price) * self.size_qty
        else:
            return (self.entry_price - current_price) * self.size_qty
    
    def should_stop(self, current_price: float) -> bool:
        if self.side == Side.BUY:
            return current_price <= self.stop_price
        return current_price >= self.stop_price
    
    def should_tp1(self, current_price: float) -> bool:
        if self.partial_closed:
            return False
        if self.side == Side.BUY:
            return current_price >= self.tp1_price
        return current_price <= self.tp1_price
    
    def should_tp2(self, current_price: float) -> bool:
        if self.side == Side.BUY:
            return current_price >= self.tp2_price
        return current_price <= self.tp2_price
    
    def hold_duration_minutes(self) -> int:
        # entry_time is UTC-aware from collector; keep math in UTC
        now = datetime.now(timezone.utc) if self.entry_time.tzinfo else datetime.now()
        return int((now - self.entry_time).total_seconds() / 60)
