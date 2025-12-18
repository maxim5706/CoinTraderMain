"""Normalized gate/signal event creation for logs and TUI."""

from datetime import datetime, timezone
from typing import Any, Tuple


def make_signal_event(
    ts: Any,
    symbol: str,
    strategy: str,
    score: int,
    spread_bps: float,
    taken: bool,
    reason: str,
) -> Tuple[datetime, str, str, int, float, bool, str]:
    """Return a normalized signal/gate event tuple used by the TUI and logs."""
    event_ts = ts or datetime.now(timezone.utc)
    if not isinstance(event_ts, datetime):
        event_ts = datetime.now(timezone.utc)
    return (
        event_ts,
        symbol,
        strategy,
        int(score) if score is not None else 0,
        float(spread_bps) if spread_bps is not None else 0.0,
        bool(taken),
        reason or "",
    )
