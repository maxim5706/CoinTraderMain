"""
Dynamic Backfill Service

Handles automatic backfill when symbols are promoted to Tier 1 (WebSocket).
- Triggers immediately on WS join
- Fetches 60 min 1m + 120 min 5m candles
- Fetches 1H and 1D candles for higher timeframe context
- Gates trading eligibility until warm
- Runs in background with retries
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, List, Set
from dataclasses import dataclass, field
from collections import deque

from core.logging_utils import get_logger
from core.models import Candle, CandleBuffer

logger = get_logger(__name__)


@dataclass
class BackfillJob:
    """A pending backfill job."""
    symbol: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    attempts: int = 0
    max_attempts: int = 3
    last_attempt: Optional[datetime] = None
    completed: bool = False
    success: bool = False
    candles_1m: int = 0
    candles_5m: int = 0
    candles_1h: int = 0      # Higher timeframe candles
    candles_1d: int = 0      # Daily candles
    error: Optional[str] = None
    
    def should_retry(self) -> bool:
        """Check if job should be retried."""
        if self.completed:
            return False
        if self.attempts >= self.max_attempts:
            return False
        if self.last_attempt:
            # Wait 5 seconds between retries
            elapsed = (datetime.now(timezone.utc) - self.last_attempt).total_seconds()
            return elapsed >= 5
        return True


class DynamicBackfill:
    """
    Manages automatic backfill for symbols joining WebSocket.
    
    Ensures no symbol is traded without sufficient history.
    """
    
    def __init__(
        self,
        fetch_candles_func: Callable,  # (symbol, granularity_s, lookback_min) -> List[Candle]
        min_candles_1m: int = 20,
        min_candles_5m: int = 10,
    ):
        self.fetch_candles = fetch_candles_func
        self.min_candles_1m = min_candles_1m
        self.min_candles_5m = min_candles_5m
        
        # Job queue
        self._pending_jobs: deque[BackfillJob] = deque()
        self._active_jobs: dict[str, BackfillJob] = {}
        self._completed_jobs: dict[str, BackfillJob] = {}
        
        # Eligibility tracking
        self._warm_symbols: Set[str] = set()
        
        # Callbacks
        self.on_candles: Optional[Callable] = None  # (symbol, candles_1m, candles_5m, candles_1h, candles_1d)
        self.on_warmup_complete: Optional[Callable] = None  # (symbol)
        
        # Control
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        
        # Stats
        self.total_jobs = 0
        self.successful_jobs = 0
        self.failed_jobs = 0
    
    async def start(self):
        """Start backfill worker."""
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        logger.info("[BACKFILL] Worker started")
    
    async def stop(self):
        """Stop backfill worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
    
    def queue_backfill(self, symbol: str):
        """Queue a symbol for backfill."""
        # Skip if already warm
        if symbol in self._warm_symbols:
            return
        
        # Skip if already queued or active
        if symbol in self._active_jobs:
            return
        if any(j.symbol == symbol for j in self._pending_jobs):
            return
        
        job = BackfillJob(symbol=symbol)
        self._pending_jobs.append(job)
        self.total_jobs += 1
        logger.info("[BACKFILL] Queued %s", symbol)
    
    def is_symbol_warm(self, symbol: str) -> bool:
        """Check if symbol has sufficient history for trading."""
        return symbol in self._warm_symbols
    
    def mark_warm(self, symbol: str, candles_1m: int, candles_5m: int):
        """Manually mark a symbol as warm (e.g., from startup backfill)."""
        if candles_1m >= self.min_candles_1m and candles_5m >= self.min_candles_5m:
            self._warm_symbols.add(symbol)
    
    async def _worker_loop(self):
        """Background worker that processes backfill queue."""
        while self._running:
            try:
                await asyncio.sleep(2)  # Check every 2 seconds (reduced frequency)
                
                if not self._running:
                    break
                
                # Process pending jobs
                if self._pending_jobs:
                    job = self._pending_jobs.popleft()
                    await self._process_job(job)
                
                # Retry failed jobs
                for symbol, job in list(self._active_jobs.items()):
                    if job.should_retry():
                        await self._process_job(job)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[BACKFILL] Worker error: %s", e, exc_info=True)
    
    async def _process_job(self, job: BackfillJob):
        """Process a single backfill job."""
        symbol = job.symbol
        self._active_jobs[symbol] = job
        job.attempts += 1
        job.last_attempt = datetime.now(timezone.utc)
        
        try:
            # Fetch 1m candles (60 min lookback)
            candles_1m = await asyncio.to_thread(
                self.fetch_candles, symbol, 60, 60
            )
            await asyncio.sleep(0.5)  # Small delay between fetches
            
            # Fetch 5m candles (60 min lookback - reduced from 120)
            candles_5m = await asyncio.to_thread(
                self.fetch_candles, symbol, 300, 60
            )
            
            # Skip higher timeframe candles to reduce API load
            # These are nice-to-have but not critical for trading
            candles_1h = []
            candles_1d = []
            
            job.candles_1m = len(candles_1m) if candles_1m else 0
            job.candles_5m = len(candles_5m) if candles_5m else 0
            job.candles_1h = len(candles_1h) if candles_1h else 0
            job.candles_1d = len(candles_1d) if candles_1d else 0
            
            # Deliver candles
            if self.on_candles and (candles_1m or candles_5m):
                self.on_candles(
                    symbol, 
                    candles_1m or [], 
                    candles_5m or [],
                    candles_1h or [],
                    candles_1d or []
                )
            
            # Check if warm
            if job.candles_1m >= self.min_candles_1m and job.candles_5m >= self.min_candles_5m:
                self._warm_symbols.add(symbol)
                job.completed = True
                job.success = True
                self.successful_jobs += 1
                
                # Move to completed
                del self._active_jobs[symbol]
                self._completed_jobs[symbol] = job
                
                logger.info(
                    "[BACKFILL] %s: %s 1m + %s 5m + %s 1h + %s 1d candles",
                    symbol,
                    job.candles_1m,
                    job.candles_5m,
                    job.candles_1h,
                    job.candles_1d,
                )
                
                if self.on_warmup_complete:
                    self.on_warmup_complete(symbol)
            else:
                # Not enough candles, will retry
                job.error = f"Insufficient candles: {job.candles_1m} 1m, {job.candles_5m} 5m"
                if job.attempts >= job.max_attempts:
                    job.completed = True
                    self.failed_jobs += 1
                    del self._active_jobs[symbol]
                    self._completed_jobs[symbol] = job
                    logger.warning(
                        "[BACKFILL] %s: %s after %s attempts",
                        symbol,
                        job.error,
                        job.attempts,
                    )
            
        except Exception as e:
            job.error = str(e)
            if "429" in str(e):
                # Rate limited, put back in queue for later
                job.attempts -= 1  # Don't count rate limit as attempt
                await asyncio.sleep(5)
            elif job.attempts >= job.max_attempts:
                job.completed = True
                self.failed_jobs += 1
                del self._active_jobs[symbol]
                self._completed_jobs[symbol] = job
                logger.error("[BACKFILL] %s: %s", symbol, e, exc_info=True)
    
    def get_pending_count(self) -> int:
        """Get number of pending backfill jobs."""
        return len(self._pending_jobs) + len(self._active_jobs)
    
    def get_stats(self) -> dict:
        """Get backfill statistics."""
        return {
            "pending": len(self._pending_jobs),
            "active": len(self._active_jobs),
            "completed": len(self._completed_jobs),
            "warm_symbols": len(self._warm_symbols),
            "total_jobs": self.total_jobs,
            "successful": self.successful_jobs,
            "failed": self.failed_jobs,
        }
