"""Signal batching and ranking for smart multi-serve.

Extracted from order_router.py - collects signals over a window
and ranks them by momentum for optimal execution order.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, TYPE_CHECKING

from core.config import settings
from core.logging_utils import get_logger

if TYPE_CHECKING:
    from core.models import Signal

logger = get_logger(__name__)


@dataclass
class RankedSignal:
    """Signal with momentum ranking for portfolio optimization."""
    symbol: str
    signal: "Signal"
    score: int
    momentum_1h: float
    momentum_15m: float
    volume_spike: float
    combined_rank: float
    features: dict = field(default_factory=dict)


class SignalBatcher:
    """Collects and ranks signals for batch execution."""
    
    def __init__(self, batch_window_seconds: int = 30):
        self.batch_window = batch_window_seconds
        self._signal_buffer: List[RankedSignal] = []
        self._last_batch_flush: datetime = datetime.now(timezone.utc)
    
    @property
    def buffer_size(self) -> int:
        return len(self._signal_buffer)
    
    @property
    def time_since_flush(self) -> float:
        return (datetime.now(timezone.utc) - self._last_batch_flush).total_seconds()
    
    def add_signal(self, signal: "Signal", features: dict = None) -> None:
        """
        Add signal to batch buffer.
        
        Instead of immediately executing, collect signals for batch_window seconds,
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
        # Momentum is 60% of ranking (catches movers)
        combined_rank = (
            score * 0.4 +
            momentum_1h * 10 +  # 1h trend: +/-10% → +/-100 points
            momentum_15m * 20 +  # 15m trend more recent
            volume_spike * 10    # Volume spike bonus
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
        
        # Check for duplicate (same symbol)
        for i, existing in enumerate(self._signal_buffer):
            if existing.symbol == signal.symbol:
                # Keep higher ranked one
                if combined_rank > existing.combined_rank:
                    self._signal_buffer[i] = ranked
                    logger.debug("[BATCH] Updated %s: rank %.0f → %.0f",
                                signal.symbol, existing.combined_rank, combined_rank)
                return
        
        self._signal_buffer.append(ranked)
        logger.info("[BATCH] Added %s (score:%d, rank:%.0f, mom1h:%.1f%%)",
                   signal.symbol, score, combined_rank, momentum_1h)
    
    def should_flush(self) -> bool:
        """Check if batch window has elapsed."""
        return self.time_since_flush >= self.batch_window and self.buffer_size > 0
    
    def get_ranked_signals(self, max_positions: int = None) -> List[RankedSignal]:
        """
        Get signals ranked by combined score.
        
        Args:
            max_positions: Maximum number of positions to return
            
        Returns:
            List of RankedSignal sorted by combined_rank (best first)
        """
        if not self._signal_buffer:
            return []
        
        # Sort by combined rank (highest first)
        sorted_signals = sorted(
            self._signal_buffer,
            key=lambda x: x.combined_rank,
            reverse=True
        )
        
        if max_positions:
            sorted_signals = sorted_signals[:max_positions]
        
        return sorted_signals
    
    def flush(self) -> List[RankedSignal]:
        """
        Flush the buffer and return ranked signals.
        
        Returns:
            List of RankedSignal to execute
        """
        signals = self.get_ranked_signals()
        self._signal_buffer.clear()
        self._last_batch_flush = datetime.now(timezone.utc)
        return signals
    
    def clear(self):
        """Clear the buffer without returning signals."""
        self._signal_buffer.clear()
        self._last_batch_flush = datetime.now(timezone.utc)
    
    def log_batch_stats(self):
        """Log current batch statistics."""
        if not self._signal_buffer:
            return
        
        avg_score = sum(s.score for s in self._signal_buffer) / len(self._signal_buffer)
        avg_rank = sum(s.combined_rank for s in self._signal_buffer) / len(self._signal_buffer)
        
        logger.info("[BATCH] %d signals buffered, avg_score=%.0f, avg_rank=%.0f",
                   len(self._signal_buffer), avg_score, avg_rank)


async def process_signal_batch(
    batcher: SignalBatcher,
    open_position_func,
    current_positions: dict,
    max_new_positions: int = 3
) -> int:
    """
    Process buffered signals in ranked order.
    
    Args:
        batcher: SignalBatcher with buffered signals
        open_position_func: Async function to open position (signal) -> Position
        current_positions: Dict of current open positions
        max_new_positions: Max new positions to open this batch
        
    Returns:
        Number of positions opened
    """
    if not batcher.should_flush():
        return 0
    
    ranked_signals = batcher.flush()
    
    if not ranked_signals:
        return 0
    
    logger.info("[BATCH] Processing %d signals (max %d new)", len(ranked_signals), max_new_positions)
    
    # Log ranking
    for i, rs in enumerate(ranked_signals[:5]):
        logger.info("[BATCH] #%d: %s (rank:%.0f, score:%d, mom:%.1f%%)",
                   i + 1, rs.symbol, rs.combined_rank, rs.score, rs.momentum_1h)
    
    # Calculate how many we can open
    available_slots = settings.max_positions - len(current_positions)
    to_open = min(available_slots, max_new_positions, len(ranked_signals))
    
    if to_open <= 0:
        logger.info("[BATCH] No slots available (have %d/%d positions)",
                   len(current_positions), settings.max_positions)
        return 0
    
    # Execute top signals
    to_execute = ranked_signals[:to_open]
    opened = 0
    
    for ranked in to_execute:
        # Skip if we already have this position
        if ranked.symbol in current_positions:
            logger.debug("[BATCH] Skipping %s - already have position", ranked.symbol)
            continue
        
        try:
            position = await open_position_func(ranked.signal)
            if position:
                opened += 1
                logger.info("[BATCH] ✓ Opened %s (#%d of %d)", ranked.symbol, opened, to_open)
        except Exception as e:
            logger.error("[BATCH] Failed to open %s: %s", ranked.symbol, e)
    
    logger.info("[BATCH] Opened %d/%d positions", opened, len(to_execute))
    return opened
