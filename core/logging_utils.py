"""Lightweight logging helpers with UTC timestamps."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from core.mode_paths import get_logs_dir

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_HANDLER_NAME = "cointrader-root-handler"
_EVENTS_HANDLER_NAME = "cointrader-events-handler"

# Global flag to suppress console output when TUI is active
_console_suppressed = False


_saved_handlers = []

_emit_lock = threading.Lock()
_last_emit_by_key: dict[str, float] = {}


def should_emit(key: str, interval_seconds: float) -> bool:
    now = time.monotonic()
    try:
        interval = float(interval_seconds)
    except Exception:
        interval = 0.0
    if interval <= 0:
        return True
    with _emit_lock:
        last = _last_emit_by_key.get(key)
        if last is not None and (now - last) < interval:
            return False
        _last_emit_by_key[key] = now
        return True


def _utc_iso(ts: datetime | None = None) -> str:
    dt = ts or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _events_log_path(ts: datetime | None = None):
    dt = ts or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")
    return get_logs_dir() / f"events_{date_str}.jsonl"


class JsonlEventsHandler(logging.Handler):
    def __init__(self, level: int = logging.WARNING):
        super().__init__(level=level)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < self.level:
                return
            evt = {
                "ts": _utc_iso(datetime.now(timezone.utc)),
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "module": record.module,
                "func": record.funcName,
                "line": record.lineno,
            }
            if record.exc_info:
                try:
                    import traceback

                    evt["exc"] = "".join(traceback.format_exception(*record.exc_info)).strip()
                except Exception:
                    evt["exc"] = "<exc_info unavailable>"

            path = _events_log_path(datetime.now(timezone.utc))
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(evt, separators=(",", ":"), default=str) + "\n"
            with self._lock:
                with open(path, "a") as f:
                    f.write(line)
        except Exception:
            return

def suppress_console_logging(suppress: bool = True):
    """Suppress ALL console logging (for TUI mode). File logging continues."""
    global _console_suppressed, _saved_handlers
    _console_suppressed = suppress
    
    root = logging.getLogger()
    
    if suppress:
        # NUCLEAR OPTION: Remove ALL StreamHandlers from root logger
        _saved_handlers = []
        for handler in root.handlers[:]:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                _saved_handlers.append(handler)
                root.removeHandler(handler)
        
        # Also set root level very high as backup
        root.setLevel(logging.CRITICAL + 1)
        
        # Suppress ALL known loggers by name
        all_loggers = list(logging.Logger.manager.loggerDict.keys())
        for name in all_loggers:
            logger = logging.getLogger(name)
            logger.setLevel(logging.CRITICAL + 1)
            # Remove their handlers too
            for h in logger.handlers[:]:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    logger.removeHandler(h)
    else:
        # Restore handlers
        for handler in _saved_handlers:
            root.addHandler(handler)
        _saved_handlers = []
        root.setLevel(_resolve_level(None))
        
        # Restore logger levels
        all_loggers = list(logging.Logger.manager.loggerDict.keys())
        for name in all_loggers:
            logger = logging.getLogger(name)
            logger.setLevel(logging.NOTSET)  # Inherit from parent


def _resolve_level(level: str | int | None) -> int:
    if level is None:
        env_level = os.getenv("LOG_LEVEL", "INFO").upper()
        return getattr(logging, env_level, logging.INFO)
    if isinstance(level, str):
        return getattr(logging, level.upper(), logging.INFO)
    return int(level)


def setup_logging(level: str | int | None = None) -> logging.Logger:
    """Configure a single root handler if one has not been attached."""
    root = logging.getLogger()
    resolved_level = _resolve_level(level)

    has_handler = any(getattr(h, "name", "") == _HANDLER_NAME for h in root.handlers)
    if not has_handler:
        handler = logging.StreamHandler()
        handler.name = _HANDLER_NAME
        formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
        formatter.converter = time.gmtime  # Force UTC timestamps
        handler.setFormatter(formatter)
        root.addHandler(handler)

    has_events_handler = any(getattr(h, "name", "") == _EVENTS_HANDLER_NAME for h in root.handlers)
    if not has_events_handler:
        events_handler = JsonlEventsHandler(level=logging.WARNING)
        events_handler.name = _EVENTS_HANDLER_NAME
        root.addHandler(events_handler)

    root.setLevel(resolved_level)
    for handler in root.handlers:
        if getattr(handler, "name", "") == _HANDLER_NAME:
            handler.setLevel(resolved_level if not _console_suppressed else logging.CRITICAL + 1)

    root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured with the shared format."""
    setup_logging()
    return logging.getLogger(name)
