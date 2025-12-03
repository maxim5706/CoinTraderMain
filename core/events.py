"""Lightweight event definitions and bus for market/order lifecycle.

This keeps paper/live paths identical by emitting the same shapes for
ticks, candles, and order events regardless of data source or executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from core.mode_configs import TradingMode
from core.models import Candle, Position, Side, TradeResult


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TickEvent:
    symbol: str
    price: float
    spread_bps: Optional[float] = None
    source: str = "ws"
    ts: datetime = _utc_now()


@dataclass
class CandleEvent:
    symbol: str
    candle: Candle
    tf: str = "1m"
    source: str = "ws"
    ts: datetime = _utc_now()


@dataclass
class OrderEvent:
    """Normalized order lifecycle event."""

    event_type: str  # "open", "close", "partial_close"
    symbol: str
    side: Side
    mode: str
    strategy_id: str = ""
    price: float = 0.0
    size_usd: float = 0.0
    size_qty: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    ts: datetime = _utc_now()


class MarketEventBus:
    """Minimal sync bus; safe to call from existing callbacks."""

    def __init__(self, mode: TradingMode):
        self.mode = mode.value if isinstance(mode, TradingMode) else str(mode)
        self._tick_handlers: List[Callable[[TickEvent], None]] = []
        self._candle_handlers: List[Callable[[CandleEvent], None]] = []
        self._order_handlers: List[Callable[[OrderEvent], None]] = []

    # Subscription helpers
    def on_tick(self, handler: Callable[[TickEvent], None]) -> None:
        self._tick_handlers.append(handler)

    def on_candle(self, handler: Callable[[CandleEvent], None]) -> None:
        self._candle_handlers.append(handler)

    def on_order(self, handler: Callable[[OrderEvent], None]) -> None:
        self._order_handlers.append(handler)

    # Emitters
    def emit_tick(self, event: TickEvent) -> None:
        for handler in list(self._tick_handlers):
            try:
                handler(event)
            except Exception:
                # Non-fatal; this bus should never break the data path
                continue

    def emit_candle(self, event: CandleEvent) -> None:
        for handler in list(self._candle_handlers):
            try:
                handler(event)
            except Exception:
                continue

    def emit_order(self, event: OrderEvent) -> None:
        for handler in list(self._order_handlers):
            try:
                handler(event)
            except Exception:
                continue


def order_event_from_position(
    event_type: str,
    position: Position,
    price: float,
    reason: str = "",
    pnl: float = 0.0,
    pnl_pct: float = 0.0,
    mode: str = "",
) -> OrderEvent:
    """Utility to produce a normalized OrderEvent from an existing position."""
    return OrderEvent(
        event_type=event_type,
        symbol=position.symbol,
        side=position.side,
        mode=mode or "",
        strategy_id=position.strategy_id or "",
        price=price,
        size_usd=position.size_usd,
        size_qty=position.size_qty,
        reason=reason,
        pnl=pnl,
        pnl_pct=pnl_pct,
        ts=_utc_now(),
    )
