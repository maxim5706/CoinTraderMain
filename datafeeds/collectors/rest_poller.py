"""
REST Candle Poller

Handles REST API polling for Tier 2 (fast) and Tier 3 (slow) symbols.
- Rate limit aware with graceful degradation
- Async polling with concurrency control
- Integrates with tier scheduler and candle store
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable, List
from dataclasses import dataclass
import time

from core.logging_utils import get_logger
from core.models import Candle, CandleBuffer

logger = get_logger(__name__)


@dataclass
class RateLimitState:
    """Tracks rate limit status."""
    tokens: float = 10.0           # Current tokens
    max_tokens: float = 10.0       # Max tokens (Coinbase ~10 req/sec)
    refill_rate: float = 8.0       # Tokens per second (conservative)
    last_refill: float = 0.0       # Timestamp of last refill
    
    # Degradation state
    is_degraded: bool = False
    degraded_until: Optional[datetime] = None
    consecutive_429s: int = 0
    
    def refill(self):
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
    
    def try_consume(self, count: int = 1) -> bool:
        """Try to consume tokens. Returns True if allowed."""
        self.refill()
        if self.tokens >= count:
            self.tokens -= count
            return True
        return False
    
    def wait_time(self, count: int = 1) -> float:
        """Time to wait before tokens available."""
        self.refill()
        if self.tokens >= count:
            return 0.0
        needed = count - self.tokens
        return needed / self.refill_rate
    
    def record_429(self):
        """Record a 429 rate limit response."""
        self.consecutive_429s += 1
        self.is_degraded = True
        # Back off exponentially
        backoff = min(60, 2 ** self.consecutive_429s)
        self.degraded_until = datetime.now(timezone.utc) + timedelta(seconds=backoff)
        self.tokens = 0  # Empty the bucket
        logger.warning("[RATE] 429 received, backing off %ss ( #%s)", backoff, self.consecutive_429s)
    
    def record_success(self):
        """Record successful request."""
        self.consecutive_429s = 0
        if self.is_degraded and self.degraded_until:
            if datetime.now(timezone.utc) >= self.degraded_until:
                self.is_degraded = False
                self.degraded_until = None
    
    def should_skip_tier3(self) -> bool:
        """In degraded mode, skip Tier 3 polling."""
        return self.is_degraded and self.consecutive_429s >= 2


class RestPoller:
    """
    Polls REST API for Tier 2 and Tier 3 symbols.
    
    Features:
    - Concurrent but rate-limited requests
    - Graceful degradation on rate limits
    - Tier 3 slows first, then Tier 2
    """
    
    def __init__(
        self,
        fetch_candles_func: Callable,  # (symbol, granularity_s, lookback_min) -> List[Candle]
        fetch_spread_func: Optional[Callable] = None,  # (symbol) -> float
    ):
        self.fetch_candles = fetch_candles_func
        self.fetch_spread = fetch_spread_func
        self.rate_limit = RateLimitState()
        
        self._running = False
        self._tier2_task: Optional[asyncio.Task] = None
        self._tier3_task: Optional[asyncio.Task] = None
        
        # Callbacks
        self.on_candles: Optional[Callable] = None  # (symbol, candles_1m, candles_5m)
        self.on_spread: Optional[Callable] = None   # (symbol, spread_bps)
        
        # Stats
        self.polls_tier2 = 0
        self.polls_tier3 = 0
        self.errors = 0
    
    async def start(self, tier_scheduler):
        """Start polling loops."""
        self._running = True
        self._tier2_task = asyncio.create_task(self._tier2_loop(tier_scheduler))
        self._tier3_task = asyncio.create_task(self._tier3_loop(tier_scheduler))
        logger.info("[POLLER] Started Tier 2 (15s) and Tier 3 (60s) loops")
    
    async def stop(self):
        """Stop polling loops."""
        self._running = False
        if self._tier2_task:
            self._tier2_task.cancel()
        if self._tier3_task:
            self._tier3_task.cancel()
    
    async def _tier2_loop(self, tier_scheduler):
        """Fast polling loop for Tier 2 symbols (every 15s)."""
        while self._running:
            try:
                await asyncio.sleep(15)
                
                if not self._running:
                    break
                
                symbols = tier_scheduler.get_tier2_symbols()
                if not symbols:
                    continue
                
                # Poll in batches to respect rate limits
                await self._poll_batch(symbols, tier_scheduler, is_tier2=True)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[POLLER] Tier 2 error: %s", e, exc_info=True)
                self.errors += 1
    
    async def _tier3_loop(self, tier_scheduler):
        """Slow polling loop for Tier 3 symbols (every 60s)."""
        while self._running:
            try:
                await asyncio.sleep(60)
                
                if not self._running:
                    break
                
                # Skip Tier 3 if degraded
                if self.rate_limit.should_skip_tier3():
                    logger.info("[POLLER] Skipping Tier 3 (rate limit degraded)")
                    continue
                
                symbols = tier_scheduler.get_tier3_symbols()
                if not symbols:
                    continue
                
                # Poll in batches
                await self._poll_batch(symbols, tier_scheduler, is_tier2=False)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("[POLLER] Tier 3 error: %s", e, exc_info=True)
                self.errors += 1
    
    async def _poll_batch(self, symbols: List[str], tier_scheduler, is_tier2: bool):
        """Poll a batch of symbols with rate limiting."""
        batch_size = 5 if is_tier2 else 3  # Smaller batches for Tier 3
        
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            
            # Wait for rate limit tokens
            wait_time = self.rate_limit.wait_time(len(batch))
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            
            if not self.rate_limit.try_consume(len(batch)):
                # Still no tokens, skip this batch
                continue
            
            # Poll batch concurrently
            tasks = [self._poll_symbol(sym, tier_scheduler) for sym in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Count successes and failures
            for result in results:
                if isinstance(result, Exception):
                    self.errors += 1
                    if "429" in str(result):
                        self.rate_limit.record_429()
                else:
                    self.rate_limit.record_success()
                    if is_tier2:
                        self.polls_tier2 += 1
                    else:
                        self.polls_tier3 += 1
            
            # Small delay between batches
            await asyncio.sleep(0.5)
    
    async def _poll_symbol(self, symbol: str, tier_scheduler):
        """Poll a single symbol for candles."""
        try:
            # Fetch 1m candles (30 min lookback for fast refresh)
            candles_1m = await asyncio.to_thread(
                self.fetch_candles, symbol, 60, 30
            )
            
            # Fetch 5m candles (60 min lookback)
            candles_5m = await asyncio.to_thread(
                self.fetch_candles, symbol, 300, 60
            )
            
            # Update tier scheduler with candle counts
            tier_scheduler.record_poll(
                symbol,
                candle_count_1m=len(candles_1m) if candles_1m else 0,
                candle_count_5m=len(candles_5m) if candles_5m else 0
            )
            
            # Callback with candles
            if self.on_candles and (candles_1m or candles_5m):
                self.on_candles(symbol, candles_1m or [], candles_5m or [])
            
            # Optionally fetch spread
            if self.fetch_spread:
                try:
                    spread = await asyncio.to_thread(self.fetch_spread, symbol)
                    if self.on_spread and spread is not None:
                        self.on_spread(symbol, spread)
                except Exception:
                    pass  # Spread is optional
            
            return True
            
        except Exception as e:
            raise e
    
    async def poll_single(self, symbol: str, tier_scheduler) -> bool:
        """Poll a single symbol on-demand (for backfill etc)."""
        if not self.rate_limit.try_consume(1):
            wait = self.rate_limit.wait_time(1)
            await asyncio.sleep(wait)
            if not self.rate_limit.try_consume(1):
                return False
        
        try:
            await self._poll_symbol(symbol, tier_scheduler)
            return True
        except Exception as e:
            logger.warning("[POLLER] Single poll error %s: %s", symbol, e, exc_info=True)
            return False
    
    def get_stats(self) -> dict:
        """Get poller statistics."""
        return {
            "polls_tier2": self.polls_tier2,
            "polls_tier3": self.polls_tier3,
            "errors": self.errors,
            "rate_tokens": self.rate_limit.tokens,
            "is_degraded": self.rate_limit.is_degraded,
            "consecutive_429s": self.rate_limit.consecutive_429s,
        }
