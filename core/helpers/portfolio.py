"""Portfolio helpers for consistent dust/minimum handling."""

from core.config import settings


def is_dust(value_usd: float) -> bool:
    """Return True if a position/value is below the minimum trading threshold."""
    try:
        return value_usd < settings.position_min_usd
    except Exception:
        return value_usd < 1.0
