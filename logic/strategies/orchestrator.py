"""
Strategy Orchestrator - Runs all strategies and picks best signal.

All strategies share the same gate funnel. The orchestrator:
1. Runs all enabled strategies for each symbol
2. Collects valid signals
3. Picks the highest edge_score_base signal
4. Returns it for gate processing
"""

from typing import Optional, List, Dict
from dataclasses import dataclass

from .base import BaseStrategy, StrategySignal
from .burst_flag import BurstFlagStrategy
from .vwap_reclaim import VWAPReclaimStrategy
from .mean_reversion import MeanReversionStrategy
from .daily_momentum import DailyMomentumStrategy
from .range_breakout import RangeBreakoutStrategy
from .relative_strength import RelativeStrengthStrategy
from .support_bounce import SupportBounceStrategy


@dataclass
class OrchestratorConfig:
    """Configuration for which strategies are enabled."""
    enable_burst_flag: bool = True
    enable_vwap_reclaim: bool = True
    enable_mean_reversion: bool = False  # DISABLED - 0% win rate, losing -$16.91
    enable_daily_momentum: bool = True   # Catches multi-day trends like SUI
    enable_range_breakout: bool = True   # Consolidation breakouts
    enable_relative_strength: bool = True # Outperformers vs BTC
    enable_support_bounce: bool = True    # Key level bounces
    enable_rotation: bool = False  # Future
    
    # Confluence settings
    require_confluence: bool = True       # Require 2+ strategies to agree
    confluence_boost: float = 15.0        # Score boost when confluence detected
    solo_signal_penalty: float = 0.7      # Size multiplier for solo signals


class StrategyOrchestrator:
    """
    Orchestrates multiple strategies, picking the best signal per symbol.
    
    Key principle: One signal per symbol per tick.
    Selection: Highest edge_score_base wins.
    All signals go through the SAME gate funnel after selection.
    """
    
    def __init__(self, config: Optional[OrchestratorConfig] = None):
        self.config = config or OrchestratorConfig()
        self.strategies: List[BaseStrategy] = []
        
        # Initialize enabled strategies
        if self.config.enable_burst_flag:
            self.strategies.append(BurstFlagStrategy())
        if self.config.enable_vwap_reclaim:
            self.strategies.append(VWAPReclaimStrategy())
        if self.config.enable_mean_reversion:
            self.strategies.append(MeanReversionStrategy())
        if self.config.enable_daily_momentum:
            self.strategies.append(DailyMomentumStrategy())
        if self.config.enable_range_breakout:
            self.strategies.append(RangeBreakoutStrategy())
        if self.config.enable_relative_strength:
            self.strategies.append(RelativeStrengthStrategy())
        if self.config.enable_support_bounce:
            self.strategies.append(SupportBounceStrategy())
        
        # Stats tracking
        self._signal_counts: Dict[str, int] = {}
        self._selection_counts: Dict[str, int] = {}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict,
    ) -> Optional[StrategySignal]:
        """
        Run all strategies and return the best signal.
        
        Args:
            symbol: Trading pair
            buffer: CandleBuffer with price data
            features: Live features from feature engine
            market_context: BTC regime, vol regime, etc.
        
        Returns:
            Best StrategySignal (highest edge_score_base) or None
        """
        candidates: List[StrategySignal] = []
        
        for strategy in self.strategies:
            try:
                signal = strategy.analyze(symbol, buffer, features, market_context)
                if signal is not None and signal.is_valid:
                    candidates.append(signal)
                    
                    # Track signal generation
                    sid = signal.strategy_id
                    self._signal_counts[sid] = self._signal_counts.get(sid, 0) + 1
                    
            except Exception as e:
                print(f"[ORCH] {strategy.strategy_id} error on {symbol}: {e}")
        
        if not candidates:
            return None
        
        # Confluence detection: count how many strategies agree
        confluence_count = len(candidates)
        has_confluence = confluence_count >= 2
        
        # Select highest edge_score_base
        best = max(candidates, key=lambda s: s.edge_score_base)
        
        # Apply confluence boost or solo penalty
        if has_confluence:
            # Multiple strategies agree - boost confidence
            best.edge_score_base = min(100, best.edge_score_base + self.config.confluence_boost)
            best.confluence_count = confluence_count
            best.reasons.append(f"confluence_{confluence_count}")
        else:
            # Solo signal - track for potential size reduction
            best.confluence_count = 1
            best.reasons.append("solo_signal")
        
        # Track selection
        self._selection_counts[best.strategy_id] = \
            self._selection_counts.get(best.strategy_id, 0) + 1
        
        return best
    
    def reset(self, symbol: str):
        """Reset all strategy states for symbol."""
        for strategy in self.strategies:
            strategy.reset(symbol)
    
    def get_stats(self) -> dict:
        """Get orchestrator statistics."""
        return {
            "signals_generated": dict(self._signal_counts),
            "signals_selected": dict(self._selection_counts),
            "strategies_enabled": [s.strategy_id for s in self.strategies],
        }
    
    def reset_stats(self):
        """Reset statistics."""
        self._signal_counts.clear()
        self._selection_counts.clear()


# Singleton instance
orchestrator = StrategyOrchestrator()
