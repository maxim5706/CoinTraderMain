"""Trade intent and plan models for execution boundaries."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
import uuid

from core.models.position import Side
from core.models.signal import Signal, SignalType


def _to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "__dict__"):
        try:
            return _to_jsonable(vars(value))
        except Exception:
            return str(value)
    return str(value)


def _new_correlation_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Intent:
    """Strategy intent (no sizing or risk adjustments)."""

    symbol: str
    type: SignalType
    timestamp: datetime
    price: float
    strategy_id: str = ""
    confidence: float = 0.0
    reason: str = ""
    expires_at: Optional[datetime] = None

    # Scoring/context fields for intelligence + gates
    vol_spike: float = 1.0
    range_spike: float = 1.0
    trend_15m: float = 0.0
    vwap_distance: float = 0.0
    spread_bps: float = 50.0
    tier: str = "unknown"

    # Metadata
    version: str = "1"
    correlation_id: str = field(default_factory=_new_correlation_id)
    side: Side = Side.BUY
    score: Optional[float] = None
    confluence_count: int = 1

    def to_dict(self) -> dict:
        return _to_jsonable(asdict(self))

    @classmethod
    def from_signal(cls, signal: Signal) -> "Intent":
        side = Side.BUY
        if signal.type == SignalType.TRAP_HEAD_SHOULDERS:
            side = Side.SELL
        score = getattr(signal, "score", None)
        if score is None:
            score = (getattr(signal, "confidence", 0.0) or 0.0) * 100
        return cls(
            symbol=signal.symbol,
            type=signal.type,
            timestamp=signal.timestamp,
            price=signal.price,
            strategy_id=getattr(signal, "strategy_id", "") or "",
            confidence=getattr(signal, "confidence", 0.0) or 0.0,
            reason=getattr(signal, "reason", "") or "",
            vol_spike=getattr(signal, "vol_spike", 1.0),
            range_spike=getattr(signal, "range_spike", 1.0),
            trend_15m=getattr(signal, "trend_15m", 0.0),
            vwap_distance=getattr(signal, "vwap_distance", 0.0),
            spread_bps=getattr(signal, "spread_bps", 50.0),
            tier=getattr(signal, "tier", "unknown"),
            confluence_count=getattr(signal, "confluence_count", 1),
            score=score,
        )


@dataclass
class TradePlan:
    """Risk-approved trade plan ready for execution."""

    intent: Intent
    size_usd: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    time_stop_min: int
    rr_ratio: float
    tier: str  # Display tier with emoji
    tier_code: str  # Clean tier: scout/normal/strong/whale
    confluence: int
    entry_score: float
    available_budget: float

    version: str = "1"
    correlation_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.correlation_id:
            self.correlation_id = self.intent.correlation_id

    def to_dict(self) -> dict:
        return _to_jsonable(asdict(self))


@dataclass
class OrderRequest:
    """Execution-ready order request derived from a TradePlan."""

    symbol: str
    side: Side
    size_usd: float
    price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    time_stop_min: int
    version: str = "1"
    correlation_id: str = field(default_factory=_new_correlation_id)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return _to_jsonable(asdict(self))

    @classmethod
    def from_plan(cls, plan: TradePlan) -> "OrderRequest":
        return cls(
            symbol=plan.intent.symbol,
            side=plan.intent.side,
            size_usd=plan.size_usd,
            price=plan.intent.price,
            stop_price=plan.stop_price,
            tp1_price=plan.tp1_price,
            tp2_price=plan.tp2_price,
            time_stop_min=plan.time_stop_min,
            correlation_id=plan.correlation_id,
        )
