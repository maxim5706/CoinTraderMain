"""
Base strategy interface and signal dataclass.

All strategies inherit from BaseStrategy and produce StrategySignal objects.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List
from enum import Enum


class SignalDirection(Enum):
    LONG = "long"
    SHORT = "short"  # Future use
    NONE = "none"


@dataclass
class StrategySignal:
    """
    Unified signal from any strategy.
    
    All strategies produce this same shape. The orchestrator picks the best
    signal per symbol and sends it through the shared gate funnel.
    """
    symbol: str
    strategy_id: str  # "burst_flag" | "vwap_reclaim" | "mean_reversion" | "rotation"
    direction: SignalDirection = SignalDirection.NONE
    
    # Base edge score (0-100) - BEFORE ML boost
    edge_score_base: float = 0.0
    
    # Signal quality components (for logging/debugging)
    trend_score: float = 0.0
    volume_score: float = 0.0
    pattern_score: float = 0.0
    timing_score: float = 0.0
    
    # Price levels (strategy-specific)
    entry_price: float = 0.0
    stop_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    
    # Geometry
    risk_pct: float = 0.0  # Distance to stop as %
    rr_ratio: float = 0.0  # Reward:Risk to TP1
    
    # Context
    reason: str = ""
    reasons: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    # Impulse/Flag data (for burst_flag strategy)
    impulse_pct: float = 0.0
    flag_retrace_pct: float = 0.0
    
    # VWAP data (for vwap_reclaim strategy)
    vwap_distance_pct: float = 0.0
    pullback_depth_atr: float = 0.0
    
    # Mean reversion data
    bb_position: float = 0.5  # 0=lower band, 1=upper band
    rsi: float = 50.0
    
    @property
    def is_valid(self) -> bool:
        """Check if signal is actionable."""
        return (
            self.direction != SignalDirection.NONE
            and self.edge_score_base > 0
            and self.entry_price > 0
            and self.stop_price > 0
        )
    
    @property
    def total_score(self) -> float:
        """Alias for edge_score_base (ML boost applied later in funnel)."""
        return self.edge_score_base


class BaseStrategy(ABC):
    """
    Abstract base class for all trading strategies.
    
    Each strategy:
    - Analyzes market data for its specific pattern/edge
    - Produces a StrategySignal with edge_score_base
    - Does NOT apply gates (gates are shared in orchestrator)
    """
    
    strategy_id: str = "base"
    
    @abstractmethod
    def analyze(
        self,
        symbol: str,
        buffer,  # CandleBuffer
        features: dict,  # Live features from feature engine
        market_context: dict,  # BTC regime, vol regime, etc.
    ) -> Optional[StrategySignal]:
        """
        Analyze symbol for this strategy's pattern.
        
        Returns StrategySignal if pattern found, None otherwise.
        Signal should have edge_score_base set but NO gate filtering.
        """
        pass
    
    def reset(self, symbol: str):
        """Reset strategy state for symbol (after trade or invalidation)."""
        pass
