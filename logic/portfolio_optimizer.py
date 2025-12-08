"""
Portfolio Optimizer - Don't miss the movers!

Problem: Bot sees LRDS at +9% (went to +63%) but enters ETH at +2.9% instead.
Solution: Rank ALL concurrent signals by momentum, enter BEST ones first.

This ensures we catch the actual market movers, not just "good enough" signals.
"""

from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime, timedelta, timezone


@dataclass
class RankedSignal:
    """Signal with momentum ranking."""
    symbol: str
    strategy_id: str
    score: int
    momentum_1h: float  # % move in last hour
    momentum_15m: float  # % move in last 15 min
    volume_spike: float  # Volume vs average
    combined_rank: float  # Composite score for ranking
    signal: any  # Original signal object


class PortfolioOptimizer:
    """
    Ranks all concurrent signals and picks best opportunities.
    
    Ensures we enter LRDS at +63% potential, not ETH at +2.9%.
    """
    
    def __init__(self):
        self._signal_buffer: List[RankedSignal] = []
        self._last_flush: Optional[datetime] = None
        self._flush_interval_sec = 30  # Collect signals for 30 sec, then pick best
    
    def add_signal(
        self,
        symbol: str,
        strategy_id: str,
        score: int,
        signal,
        features: dict
    ):
        """Add signal to buffer for ranking."""
        
        # Extract momentum metrics
        momentum_1h = features.get('trend_1h', 0) or features.get('trend_15m', 0) * 4
        momentum_15m = features.get('trend_15m', 0) or features.get('trend_5m', 0) * 3
        volume_spike = features.get('vol_spike_5m', 1.0)
        
        # Calculate combined rank (momentum + volume + score)
        # Momentum is 60% of rank (most important)
        # Volume is 25% (confirmation)
        # Score is 15% (quality)
        combined_rank = (
            abs(momentum_1h) * 0.4 +  # 40% weight on 1h momentum
            abs(momentum_15m) * 0.2 +  # 20% weight on 15m momentum
            (volume_spike - 1.0) * 0.25 +  # 25% weight on volume spike
            (score / 100) * 0.15  # 15% weight on score
        )
        
        ranked = RankedSignal(
            symbol=symbol,
            strategy_id=strategy_id,
            score=score,
            momentum_1h=momentum_1h,
            momentum_15m=momentum_15m,
            volume_spike=volume_spike,
            combined_rank=combined_rank,
            signal=signal
        )
        
        self._signal_buffer.append(ranked)
    
    def get_best_signals(self, max_count: int = 10) -> List[RankedSignal]:
        """
        Get top N signals by momentum ranking.
        
        Called every flush_interval_sec to pick best opportunities.
        """
        now = datetime.now(timezone.utc)
        
        # Initialize timer
        if self._last_flush is None:
            self._last_flush = now
            return []
        
        # Check if it's time to flush
        elapsed = (now - self._last_flush).total_seconds()
        if elapsed < self._flush_interval_sec and len(self._signal_buffer) < 20:
            return []  # Keep collecting
        
        if not self._signal_buffer:
            return []
        
        # Sort by combined rank (highest first)
        sorted_signals = sorted(
            self._signal_buffer,
            key=lambda x: x.combined_rank,
            reverse=True
        )
        
        # Take top N
        best = sorted_signals[:max_count]
        
        # Clear buffer
        self._signal_buffer.clear()
        self._last_flush = now
        
        return best
    
    def force_flush(self) -> List[RankedSignal]:
        """Force immediate flush (for testing)."""
        if not self._signal_buffer:
            return []
        
        sorted_signals = sorted(
            self._signal_buffer,
            key=lambda x: x.combined_rank,
            reverse=True
        )
        
        self._signal_buffer.clear()
        self._last_flush = datetime.now(timezone.utc)
        
        return sorted_signals
    
    def should_skip_signal(self, symbol: str, momentum_1h: float) -> bool:
        """
        Should we skip this signal even if score is high?
        
        Skip if momentum is weak compared to market leaders.
        """
        if not self._signal_buffer:
            return False
        
        # Get max momentum in buffer
        max_momentum = max(s.momentum_1h for s in self._signal_buffer)
        
        # Skip if this signal's momentum is <50% of leader
        if max_momentum > 5.0 and momentum_1h < max_momentum * 0.5:
            return True  # Leader is pumping, this one is lagging
        
        return False


# Global optimizer
portfolio_optimizer = PortfolioOptimizer()
