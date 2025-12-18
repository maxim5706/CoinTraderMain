"""Validation helpers to keep feature values finite and well-shaped."""

import math
from typing import Any, Dict


def finite_float(value: Any, default: float = 0.0) -> float:
    """Return a finite float or a default fallback."""
    try:
        fval = float(value)
        if math.isfinite(fval):
            return fval
    except Exception:
        pass
    return default


def safe_features(features: Dict[str, Any]) -> Dict[str, float]:
    """Coerce feature dict into finite floats with required keys present."""
    keys = [
        "price",
        "trend_1h",
        "trend_15m",
        "trend_5m",
        "vol_ratio",
        "vol_spike_5m",
        "vwap_pct",
        "vwap_distance",
        "spread_bps",
    ]
    cleaned: Dict[str, float] = {}
    for key in keys:
        cleaned[key] = finite_float(features.get(key, 0.0))
    return cleaned
