"""Lightweight logging helpers with UTC timestamps."""

from __future__ import annotations

import logging
import os
import time

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_HANDLER_NAME = "cointrader-root-handler"

# Global flag to suppress console output when TUI is active
_console_suppressed = False


def suppress_console_logging(suppress: bool = True):
    """Suppress console logging (for TUI mode)."""
    global _console_suppressed
    _console_suppressed = suppress
    
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, "name", "") == _HANDLER_NAME:
            if suppress:
                handler.setLevel(logging.CRITICAL + 1)  # Suppress all
            else:
                handler.setLevel(_resolve_level(None))
    
    # Also suppress external library loggers that print to console
    external_loggers = ["coinbase", "coinbase.RESTClient", "urllib3", "httpx"]
    for name in external_loggers:
        ext_logger = logging.getLogger(name)
        if suppress:
            ext_logger.setLevel(logging.CRITICAL + 1)
        else:
            ext_logger.setLevel(logging.WARNING)


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
