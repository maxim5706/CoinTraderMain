"""Helpers for mode-scoped paths (data/logs per trading mode).

These helpers avoid paper/live cross-contamination by keeping outputs
under data/<mode>/ and logs/<mode>/ directories, keyed off TRADING_MODE.
"""

import os
from pathlib import Path
from typing import Optional

from core.mode_configs import TradingMode


def _normalize_mode(mode: Optional[str | TradingMode] = None) -> str:
    """Return lowercase mode string using env override if none provided."""
    if isinstance(mode, TradingMode):
        return mode.value
    if isinstance(mode, str) and mode:
        return mode.lower()
    return (os.getenv("TRADING_MODE") or "paper").lower()


def get_data_dir(mode: Optional[str | TradingMode] = None) -> Path:
    """Mode-specific data directory (data/<mode>/)."""
    path = Path("data") / _normalize_mode(mode)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_logs_dir(mode: Optional[str | TradingMode] = None) -> Path:
    """Mode-specific logs directory (logs/<mode>/)."""
    path = Path("logs") / _normalize_mode(mode)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_status_path(mode: Optional[str | TradingMode] = None) -> Path:
    """Path to lightweight status snapshot for health checks."""
    return get_data_dir(mode) / "status.json"
