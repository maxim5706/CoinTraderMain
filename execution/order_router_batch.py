"""
Smart Multi-Serve Methods for Order Router.

Add these methods to OrderRouter class to enable momentum-based ranking.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
import asyncio


@dataclass
class RankedSignal:
    """Signal with momentum ranking for portfolio optimization."""
    symbol: str
    signal: any  # Original Signal object
    score: int
    momentum_1h: float
    momentum_15m: float
    volume_spike: float
    combined_rank: float
    features: dict


# ADD THESE METHODS TO OrderRouter CLASS:

def add_signal_to_batch(self, signal, features: dict = None):
    """
    Collect signal for batching (SMART MULTI-SERVE).
    
    Instead of immediately executing, collect signals for 30s,
    then rank by momentum and execute best ones first.
    """
    if features is None:
        features = {}
    
    # Extract momentum metrics
    momentum_1h = features.get('trend_1h', 0) or features.get('trend_15m', 0) * 4
    momentum_15m = features.get('trend_15m', 0) or features.get('trend_5m', 0) * 3
    volume_spike = features.get('vol_spike_5m', 1.0)
    
    # Get score from signal
    score = getattr(signal, 'score', 70)
    entry_score = getattr(signal, 'entry_score', None)
    if entry_score and hasattr(entry_score, 'total_score'):
        score = int(entry_score.total_score)
    
    # Calculate combined rank (momentum-weighted)
    # Momentum is 60% of ranking (catches movers like LRDS +63%)
    # Volume is 25% (confirmation)
    # Score is 15% (quality filter)
    combined_rank = (
        abs(momentum_1h) * 0.4 +      # 40% weight on 1h momentum
        abs(momentum_15m) * 0.2 +      # 20% weight on 15m momentum  
        max(0, (volume_spike - 1.0)) * 0.25 +  # 25% weight on volume spike
        (score / 100) * 0.15           # 15% weight on score
    )
    
    ranked = RankedSignal(
        symbol=signal.symbol,
        signal=signal,
        score=score,
        momentum_1h=momentum_1h,
        momentum_15m=momentum_15m,
        volume_spike=volume_spike,
        combined_rank=combined_rank,
        features=features
    )
    
    self._signal_buffer.append(ranked)
    logger.info(
        "[BATCH] Collected %s: rank=%.2f (mom1h=%.1f%%, vol=%.1fx, score=%d)",
        signal.symbol, combined_rank, momentum_1h, volume_spike, score
    )


async def process_signal_batch(self) -> int:
    """
    Process batch of signals: rank by momentum, execute best ones.
    
    Returns number of positions opened.
    """
    now = datetime.now(timezone.utc)
    
    # Initialize batch timer
    if self._last_batch_flush is None:
        self._last_batch_flush = now
        return 0
    
    # Check if batch window elapsed
    elapsed = (now - self._last_batch_flush).total_seconds()
    if elapsed < self._batch_window_sec and len(self._signal_buffer) < 20:
        return 0  # Keep collecting signals
    
    if not self._signal_buffer:
        return 0  # No signals to process
    
    # Rank signals by combined momentum/quality score
    sorted_signals = sorted(
        self._signal_buffer,
        key=lambda x: x.combined_rank,
        reverse=True
    )
    
    # Log rankings
    logger.info("[BATCH] Processing %d signals:", len(sorted_signals))
    for i, rs in enumerate(sorted_signals[:5], 1):
        logger.info(
            "  %d. %s: rank=%.2f (mom1h=%+.1f%%, vol=%.1fx, score=%d)",
            i, rs.symbol, rs.combined_rank, rs.momentum_1h, rs.volume_spike, rs.score
        )
    
    # Calculate how many we can take
    available_slots = settings.max_positions - len(self.positions)
    if available_slots <= 0:
        logger.info("[BATCH] No position slots available")
        self._signal_buffer.clear()
        self._last_batch_flush = now
        return 0
    
    # Take top N signals (best momentum)
    to_execute = sorted_signals[:min(available_slots, 10)]
    
    # Execute in order of rank (best first)
    opened = 0
    for ranked_signal in to_execute:
        try:
            result = await self.open_position(ranked_signal.signal)
            if result:
                opened += 1
                logger.info(
                    "[BATCH] ✓ Opened %s (rank=%.2f, mom1h=%+.1f%%)",
                    ranked_signal.symbol, ranked_signal.combined_rank, ranked_signal.momentum_1h
                )
        except Exception as e:
            logger.error("[BATCH] Error opening %s: %s", ranked_signal.symbol, e)
    
    # Clear buffer and reset timer
    self._signal_buffer.clear()
    self._last_batch_flush = now
    
    logger.info("[BATCH] Opened %d/%d positions", opened, len(to_execute))
    return opened


def should_skip_weak_mover(self, symbol: str, momentum_1h: float) -> bool:
    """
    Skip signals with weak momentum when leaders are pumping.
    
    Example: Skip ETH at +2% when LRDS is at +9%
    """
    if not self._signal_buffer:
        return False
    
    # Get max momentum in current batch
    max_momentum = max((s.momentum_1h for s in self._signal_buffer), default=0)
    
    # Skip if this signal's momentum is <50% of leader
    if max_momentum > 5.0 and momentum_1h < max_momentum * 0.5:
        logger.info(
            "[BATCH] Skipping %s (mom=%.1f%%) - leader at %.1f%%",
            symbol, momentum_1h, max_momentum
        )
        return True
    
    return False
