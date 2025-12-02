"""
Multi-strategy architecture.

All strategies share the same gate funnel. Each produces a base edge score.
The orchestrator picks the highest-scoring eligible signal per symbol.

Canonical pipeline order:
1. Warmth gate
2. BTC regime gate  
3. Strategy base edge score (burst_flag | vwap_reclaim | mean_reversion | rotation)
4. R:R geometry gate
5. Execution reality gate
6. ML gate/boost last (cached, never creates trades)
"""

from .base import BaseStrategy, StrategySignal
from .burst_flag import BurstFlagStrategy
from .vwap_reclaim import VWAPReclaimStrategy
from .mean_reversion import MeanReversionStrategy
from .orchestrator import StrategyOrchestrator

__all__ = [
    "BaseStrategy",
    "StrategySignal", 
    "BurstFlagStrategy",
    "VWAPReclaimStrategy",
    "MeanReversionStrategy",
    "StrategyOrchestrator",
]
