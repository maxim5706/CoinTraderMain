"""
Multi-strategy architecture.

All strategies share the same gate funnel. Each produces a base edge score.
The orchestrator picks the highest-scoring eligible signal per symbol.

Active strategies: burst_flag, vwap_reclaim, daily_momentum, range_breakout,
relative_strength, support_bounce, momentum_1h, rsi_momentum, bb_expansion
"""

from .base import BaseStrategy, StrategySignal
from .burst_flag import BurstFlagStrategy
from .vwap_reclaim import VWAPReclaimStrategy
from .daily_momentum import DailyMomentumStrategy
from .range_breakout import RangeBreakoutStrategy
from .relative_strength import RelativeStrengthStrategy
from .support_bounce import SupportBounceStrategy
from .momentum_1h import Momentum1HStrategy
from .rsi_momentum import RSIMomentumStrategy
from .bb_expansion import BBExpansionStrategy
from .orchestrator import StrategyOrchestrator

__all__ = [
    "BaseStrategy",
    "StrategySignal", 
    "BurstFlagStrategy",
    "VWAPReclaimStrategy",
    "DailyMomentumStrategy",
    "RangeBreakoutStrategy",
    "RelativeStrengthStrategy",
    "SupportBounceStrategy",
    "Momentum1HStrategy",
    "RSIMomentumStrategy",
    "BBExpansionStrategy",
    "StrategyOrchestrator",
]
