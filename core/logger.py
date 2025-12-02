"""JSON lines logger for session data capture."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs"  # Project root logs/


def ensure_logs_dir():
    """Create logs/ directory if it doesn't exist."""
    try:
        LOGS_DIR.mkdir(exist_ok=True)
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
    """Return path for logs/{layer}_{date}.jsonl."""
    date_str = utc_date_str(ts)
    return LOGS_DIR / f"{layer}_{date_str}.jsonl"


def append_jsonl(path: Path, record: dict):
    """Append a JSON record as a single line. Thread-safe via append mode."""
    ensure_logs_dir()
    line = json.dumps(record, separators=(",", ":"), default=str)
    with open(path, "a") as f:
        f.write(line + "\n")


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
    """Log trade lifecycle event."""
    append_jsonl(log_path("trades", ts), record)


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
    """Log order placement/response (buy/sell market/limit)."""
    append_jsonl(log_path("orders", ts), record)


def log_stop_order(record: dict, ts: datetime = None):
    """Log stop order placement/modification/cancellation."""
    append_jsonl(log_path("stops", ts), record)


def log_exit_decision(record: dict, ts: datetime = None):
    """Log exit decision reasoning (stop/TP/thesis/time)."""
    append_jsonl(log_path("exits", ts), record)


def log_quality_score(record: dict, ts: datetime = None):
    """Log quality filter scoring breakdown."""
    append_jsonl(log_path("quality", ts), record)
