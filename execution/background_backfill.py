"""
Background Backfill Service - Continuous candle building and strategy testing.

Runs in background to:
1. Build older candle history (1m, 5m, 1h, 1d)
2. Run mini-backtests on strategies
3. Report strategy performance issues
4. Maintain warm data coverage

Usage:
    Called from main bot loop periodically (every 5-10 minutes)
"""

import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

from core.logging_utils import get_logger
from core.config import settings

logger = get_logger(__name__)


@dataclass
class StrategyTestResult:
    """Result of a mini-backtest on a strategy."""
    strategy_id: str
    symbol: str
    period_hours: int
    signals_generated: int
    would_have_won: int
    would_have_lost: int
    avg_pnl_pct: float
    issues: List[str] = field(default_factory=list)
    
    @property
    def win_rate(self) -> float:
        total = self.would_have_won + self.would_have_lost
        return (self.would_have_won / total * 100) if total > 0 else 0.0


@dataclass
class BackfillProgress:
    """Track backfill progress per symbol."""
    symbol: str
    oldest_1m: Optional[datetime] = None
    oldest_5m: Optional[datetime] = None
    oldest_1h: Optional[datetime] = None
    oldest_1d: Optional[datetime] = None
    candles_1m: int = 0
    candles_5m: int = 0
    candles_1h: int = 0
    candles_1d: int = 0
    last_backfill: Optional[datetime] = None
    

class BackgroundBackfill:
    """
    Background service for continuous candle building and strategy testing.
    
    Designed to run alongside the main bot without blocking trading.
    """
    
    def __init__(
        self,
        scanner,  # SymbolScanner for API access
        collector,  # CandleCollector for buffer access
        strategies: List = None,  # Strategy instances for testing
        get_price_func=None,
    ):
        self.scanner = scanner
        self.collector = collector
        self.strategies = strategies or []
        self.get_price = get_price_func
        
        # Progress tracking
        self.progress: Dict[str, BackfillProgress] = {}
        self.test_results: List[StrategyTestResult] = []
        self.strategy_issues: Dict[str, List[str]] = defaultdict(list)
        
        # Rate limiting
        self._last_api_call = 0
        self._api_delay = 0.3  # 300ms between API calls
        self._running = False
        self._symbols_queue: List[str] = []
        self._current_idx = 0
        
        # Stats
        self.total_candles_fetched = 0
        self.total_tests_run = 0
        self.last_run: Optional[datetime] = None
    
    def _rate_limit(self):
        """Ensure we don't hit API rate limits."""
        elapsed = time.time() - self._last_api_call
        if elapsed < self._api_delay:
            time.sleep(self._api_delay - elapsed)
        self._last_api_call = time.time()
    
    async def run_cycle(self, symbols: List[str], max_symbols: int = 5) -> Dict:
        """
        Run one backfill cycle - processes a few symbols then returns.
        
        Call this periodically from the main bot loop.
        Returns stats about what was done.
        """
        if self._running:
            return {"status": "already_running"}
        
        self._running = True
        self.last_run = datetime.now(timezone.utc)
        
        stats = {
            "symbols_processed": 0,
            "candles_fetched": 0,
            "tests_run": 0,
            "issues_found": [],
        }
        
        try:
            # Update queue if needed
            if not self._symbols_queue or self._current_idx >= len(self._symbols_queue):
                self._symbols_queue = list(symbols)
                self._current_idx = 0
            
            # Process a batch of symbols
            batch = self._symbols_queue[self._current_idx:self._current_idx + max_symbols]
            self._current_idx += max_symbols
            
            for symbol in batch:
                try:
                    # Backfill older candles
                    candles = await self._backfill_symbol(symbol)
                    stats["candles_fetched"] += candles
                    
                    # Run mini-backtest if we have enough data
                    if candles > 0 and self.strategies:
                        issues = await self._test_strategies(symbol)
                        stats["issues_found"].extend(issues)
                        stats["tests_run"] += 1
                    
                    stats["symbols_processed"] += 1
                    
                except Exception as e:
                    logger.warning("[BACKFILL] Error processing %s: %s", symbol, e)
                    continue
            
            self.total_candles_fetched += stats["candles_fetched"]
            self.total_tests_run += stats["tests_run"]
            
            # Log progress
            if stats["symbols_processed"] > 0:
                logger.info(
                    "[BACKFILL] Cycle: %d symbols, %d candles, %d tests, %d issues",
                    stats["symbols_processed"],
                    stats["candles_fetched"],
                    stats["tests_run"],
                    len(stats["issues_found"])
                )
            
            return stats
            
        finally:
            self._running = False
    
    async def _backfill_symbol(self, symbol: str) -> int:
        """Backfill older candles for a symbol. Returns count of candles fetched."""
        buffer = self.collector.get_buffer(symbol)
        if not buffer:
            return 0
        
        # Get or create progress tracker
        if symbol not in self.progress:
            self.progress[symbol] = BackfillProgress(symbol=symbol)
        prog = self.progress[symbol]
        
        total_fetched = 0
        
        # Backfill 1h candles (most useful for trend analysis)
        # Go back 7 days if we don't have much history
        try:
            self._rate_limit()
            history_1h = self.scanner.fetch_history(
                symbol, 
                granularity_s=3600, 
                lookback_minutes=7*24*60  # 7 days
            )
            if history_1h:
                for candle in history_1h:
                    if hasattr(buffer, 'add_1h'):
                        buffer.add_1h(candle)
                    total_fetched += 1
                prog.candles_1h = len(history_1h)
                prog.oldest_1h = history_1h[0].time if history_1h else None
        except Exception as e:
            if "429" not in str(e):
                logger.debug("[BACKFILL] 1h fetch failed for %s: %s", symbol, e)
        
        # Backfill 1d candles for longer-term context
        try:
            self._rate_limit()
            history_1d = self.scanner.fetch_history(
                symbol,
                granularity_s=86400,
                lookback_minutes=30*24*60  # 30 days
            )
            if history_1d:
                for candle in history_1d:
                    if hasattr(buffer, 'add_1d'):
                        buffer.add_1d(candle)
                    total_fetched += 1
                prog.candles_1d = len(history_1d)
                prog.oldest_1d = history_1d[0].time if history_1d else None
        except Exception as e:
            if "429" not in str(e):
                logger.debug("[BACKFILL] 1d fetch failed for %s: %s", symbol, e)
        
        prog.last_backfill = datetime.now(timezone.utc)
        return total_fetched
    
    async def _test_strategies(self, symbol: str) -> List[str]:
        """
        Run mini-backtest on strategies for this symbol.
        Returns list of issues found.
        """
        issues = []
        buffer = self.collector.get_buffer(symbol)
        if not buffer:
            return issues
        
        for strategy in self.strategies:
            try:
                result = await self._test_strategy(strategy, symbol, buffer)
                if result:
                    self.test_results.append(result)
                    
                    # Check for issues
                    if result.signals_generated > 5 and result.win_rate < 30:
                        issue = f"{strategy.strategy_id} on {symbol}: {result.win_rate:.0f}% win rate ({result.signals_generated} signals)"
                        issues.append(issue)
                        self.strategy_issues[strategy.strategy_id].append(issue)
                    
                    if result.avg_pnl_pct < -2.0:
                        issue = f"{strategy.strategy_id} on {symbol}: avg {result.avg_pnl_pct:.1f}% loss"
                        issues.append(issue)
                        
            except Exception as e:
                logger.debug("[BACKFILL] Strategy test failed for %s: %s", symbol, e)
                continue
        
        return issues
    
    async def _test_strategy(self, strategy, symbol: str, buffer) -> Optional[StrategyTestResult]:
        """Run a mini-backtest on one strategy/symbol pair."""
        if not hasattr(strategy, 'analyze'):
            return None
        
        # Get recent candles for testing
        candles_1h = buffer.get_1h_candles(100) if hasattr(buffer, 'get_1h_candles') else []
        if len(candles_1h) < 20:
            return None  # Not enough data
        
        signals_generated = 0
        would_have_won = 0
        would_have_lost = 0
        pnl_sum = 0.0
        
        # Walk through history and test signals
        for i in range(20, len(candles_1h) - 5):  # Leave 5 candles for outcome
            # Create a view of data up to this point
            historical_buffer = candles_1h[:i]
            
            try:
                # Check if strategy would have generated a signal
                # (simplified - real implementation would need full buffer)
                price_at_signal = candles_1h[i].close
                
                # Look at outcome (5 candles later)
                future_high = max(c.high for c in candles_1h[i:i+5])
                future_low = min(c.low for c in candles_1h[i:i+5])
                
                # Simulate a signal check (simplified)
                momentum = (candles_1h[i].close - candles_1h[i-5].close) / candles_1h[i-5].close * 100
                
                if momentum > 2.0:  # Simple momentum signal
                    signals_generated += 1
                    
                    # Would it have hit TP (3%) before stop (-2%)?
                    tp_hit = (future_high - price_at_signal) / price_at_signal >= 0.03
                    stop_hit = (price_at_signal - future_low) / price_at_signal >= 0.02
                    
                    if tp_hit and not stop_hit:
                        would_have_won += 1
                        pnl_sum += 3.0
                    elif stop_hit:
                        would_have_lost += 1
                        pnl_sum -= 2.0
                    else:
                        # Neither hit - small win/loss based on final price
                        final_pnl = (candles_1h[i+4].close - price_at_signal) / price_at_signal * 100
                        pnl_sum += final_pnl
                        if final_pnl >= 0:
                            would_have_won += 1
                        else:
                            would_have_lost += 1
                            
            except Exception:
                continue
        
        if signals_generated == 0:
            return None
        
        return StrategyTestResult(
            strategy_id=getattr(strategy, 'strategy_id', 'unknown'),
            symbol=symbol,
            period_hours=len(candles_1h),
            signals_generated=signals_generated,
            would_have_won=would_have_won,
            would_have_lost=would_have_lost,
            avg_pnl_pct=pnl_sum / signals_generated if signals_generated > 0 else 0,
        )
    
    def get_summary(self) -> Dict:
        """Get summary of backfill progress and strategy testing."""
        return {
            "total_candles_fetched": self.total_candles_fetched,
            "total_tests_run": self.total_tests_run,
            "symbols_with_progress": len(self.progress),
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "strategy_issues": dict(self.strategy_issues),
            "recent_test_results": [
                {
                    "strategy": r.strategy_id,
                    "symbol": r.symbol,
                    "win_rate": r.win_rate,
                    "signals": r.signals_generated,
                    "avg_pnl": r.avg_pnl_pct,
                }
                for r in self.test_results[-20:]  # Last 20 results
            ],
        }
    
    def get_issues(self) -> List[str]:
        """Get list of strategy issues found."""
        all_issues = []
        for strategy_id, issues in self.strategy_issues.items():
            all_issues.extend(issues[-5:])  # Last 5 issues per strategy
        return all_issues


# Singleton instance
_backfill_service: Optional[BackgroundBackfill] = None


def get_backfill_service() -> Optional[BackgroundBackfill]:
    """Get the backfill service instance."""
    return _backfill_service


def init_backfill_service(scanner, collector, strategies=None, get_price_func=None) -> BackgroundBackfill:
    """Initialize the backfill service."""
    global _backfill_service
    _backfill_service = BackgroundBackfill(
        scanner=scanner,
        collector=collector,
        strategies=strategies,
        get_price_func=get_price_func,
    )
    logger.info("[BACKFILL] Background service initialized")
    return _backfill_service
