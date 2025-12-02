"""Completed trade result model."""

from dataclasses import dataclass
from datetime import datetime

from core.models.position import Side


@dataclass
class TradeResult:
    """Completed trade result."""
    symbol: str
    side: Side
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    size_usd: float
    pnl: float
    pnl_pct: float
    exit_reason: str  # "stop", "tp1", "tp2", "time_stop"

