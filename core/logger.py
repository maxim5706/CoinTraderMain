"""JSON lines logger for session data capture.

Logs are consolidated into 5 families for cleanliness:
- market: raw, candles_*, burst
- strategy: signals, quality, entry_attempts, rejections, universe
- trades: trades, orders, stops, exits
- pnl: pnl_snapshots, daily_pnl
- health: health

Atomic writes: Critical logs (trades, orders) use fsync to ensure
data is persisted even if the process crashes immediately after.
"""

import json
import os
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from core.mode_paths import get_logs_dir

# Map verbose layers to consolidated families to keep file count low
LAYER_FAMILY_MAP = {
    # Market data
    "raw": "market",
    "candles_1m": "market",
    "candles_5m": "market",
    "burst": "market",
    # Strategy/intel
    "signals": "strategy",
    "quality": "strategy",
    "entry_attempts": "strategy",
    "rejections": "strategy",
    "universe": "strategy",
    # Trades/execution
    "trades": "trades",
    "orders": "trades",
    "stops": "trades",
    "exits": "trades",
    # PnL
    "pnl_snapshots": "pnl",
    "daily_pnl": "pnl",
    # Health
    "health": "health",
}


def ensure_logs_dir():
    """Create logs/ directory if it doesn't exist."""
    try:
        get_logs_dir().mkdir(parents=True, exist_ok=True)
    except (OSError, SystemExit):
        pass  # Ignore during shutdown or if already exists


def utc_date_str(ts: datetime = None) -> str:
    """Return YYYY-MM-DD in UTC."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    elif ts.tzinfo is None:
        # Assume naive datetime is UTC
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%Y-%m-%d")


def utc_iso_str(ts: datetime = None) -> str:
    """Return ISO 8601 timestamp with Z suffix."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    elif ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def log_path(layer: str, ts: datetime = None) -> Path:
    """Return path for logs/{family}_{date}.jsonl."""
    date_str = utc_date_str(ts)
    family = LAYER_FAMILY_MAP.get(layer, layer)
    return get_logs_dir() / f"{family}_{date_str}.jsonl"


def append_jsonl(path: Path, record: dict, critical: bool = False):
    """
    Append a JSON record as a single line. Thread-safe via append mode.
    
    Args:
        path: Target log file path
        record: Dictionary to log as JSON
        critical: If True, use fsync to ensure write is persisted to disk
                  (slower but crash-safe). Use for trades/orders.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, SystemExit):
        pass
    
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    
    if critical:
        # Atomic write for critical data:
        # 1. Write to temp file in same directory
        # 2. fsync to ensure it's on disk
        # 3. Append to main file with fsync
        try:
            # Write directly with fsync for durability
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode('utf-8'))
                os.fsync(fd)  # Force write to disk
            finally:
                os.close(fd)
        except OSError:
            # Fallback to normal write if low-level fails
            with open(path, "a") as f:
                f.write(line)
    else:
        # Normal append for non-critical data (faster)
        with open(path, "a") as f:
            f.write(line)


# Convenience functions for each layer
def log_raw(record: dict, ts: datetime = None):
    """Log raw WS event (ticks, trades, heartbeats)."""
    append_jsonl(log_path("raw", ts), record)


def log_candle_1m(record: dict, ts: datetime = None):
    """Log completed 1m candle."""
    append_jsonl(log_path("candles_1m", ts), record)


def log_candle_5m(record: dict, ts: datetime = None):
    """Log completed 5m candle."""
    append_jsonl(log_path("candles_5m", ts), record)


def log_burst(record: dict, ts: datetime = None):
    """Log burst metrics snapshot."""
    append_jsonl(log_path("burst", ts), record)


def log_signal(record: dict, ts: datetime = None):
    """Log strategy signal."""
    append_jsonl(log_path("signals", ts), record)


def log_trade(record: dict, ts: datetime = None):
    """
    Log trade lifecycle event (critical - uses fsync).
    Deduplicates identical records within a rolling window to avoid double-logging fills.
    """
    cache_key = json.dumps(record, sort_keys=True, default=str)
    if not hasattr(log_trade, "_recent"):
        log_trade._recent = deque(maxlen=200)  # type: ignore[attr-defined]
    if cache_key in log_trade._recent:  # type: ignore[attr-defined]
        return
    log_trade._recent.append(cache_key)  # type: ignore[attr-defined]
    append_jsonl(log_path("trades", ts), record, critical=True)


def log_pnl_snapshot(record: dict, ts: datetime = None):
    """Log PnL snapshot (periodic, every X minutes)."""
    append_jsonl(log_path("pnl_snapshots", ts), record)


def log_daily_pnl(record: dict, ts: datetime = None):
    """Log daily PnL summary (once per day at rollover)."""
    append_jsonl(log_path("daily_pnl", ts), record)


def log_universe(record: dict, ts: datetime = None):
    """Log ranked universe snapshot."""
    append_jsonl(log_path("universe", ts), record)


def log_entry_attempt(record: dict, ts: datetime = None):
    """Log entry attempt with score breakdown (pass or fail)."""
    append_jsonl(log_path("entry_attempts", ts), record)


def log_rejection(record: dict, ts: datetime = None):
    """Log entry rejection with gate that blocked."""
    append_jsonl(log_path("rejections", ts), record)


def log_health(record: dict, ts: datetime = None):
    """Log health snapshot (ML %, warm count, etc)."""
    append_jsonl(log_path("health", ts), record)


def log_order(record: dict, ts: datetime = None):
    """Log order placement/response (critical - uses fsync)."""
    append_jsonl(log_path("orders", ts), record, critical=True)


def log_stop_order(record: dict, ts: datetime = None):
    """Log stop order placement/modification/cancellation (critical - uses fsync)."""
    append_jsonl(log_path("stops", ts), record, critical=True)


def log_exit_decision(record: dict, ts: datetime = None):
    """Log exit decision reasoning (critical - uses fsync)."""
    append_jsonl(log_path("exits", ts), record, critical=True)


def log_quality_score(record: dict, ts: datetime = None):
    """Log quality filter scoring breakdown."""
    append_jsonl(log_path("quality", ts), record)
