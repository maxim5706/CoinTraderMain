"""Standardized gate reasons for consistency across logging and UI."""

from enum import Enum
from typing import Union


class GateReason(str, Enum):
    WARMTH = "warmth"
    REGIME = "regime"
    SCORE = "score"
    RR = "rr"
    LIMITS = "limits"
    SPREAD = "spread"
    TRUTH = "truth"
    CIRCUIT = "circuit_breaker"
    WHITELIST = "whitelist"
    COOLDOWN = "cooldown"
    BUDGET = "budget"
    RISK = "risk"

    @classmethod
    def from_value(cls, value: Union[str, "GateReason"]) -> "GateReason":
        if isinstance(value, GateReason):
            return value
        try:
            return cls(value)
        except Exception:
            # Fallback for unexpected strings
            return GateReason.LIMITS if value == "limits" else GateReason.SCORE
