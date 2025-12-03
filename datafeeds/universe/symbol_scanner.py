"""
Symbol Scanner - Three-clock architecture for burst detection.

Clock A: Real-time WebSocket stream (always on)
Clock B: Rolling intraday context (every minute) 
Clock C: Background slow context (every 10-60 minutes)
"""

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable
import numpy as np

from core.config import settings
from core.logging_utils import get_logger
from core.models import Candle

logger = get_logger(__name__)


# ============================================================
# RATE LIMITER - Prevents 429 errors from Coinbase
# ============================================================

class RateLimiter:
    """
    Token bucket rate limiter with exponential backoff.
    
    Coinbase limits:
    - Public endpoints: ~10 requests/second
    - Private endpoints: ~15 requests/second
    """
    
    def __init__(self, requests_per_second: float = 5.0, burst_size: int = 10):
        self.rate = requests_per_second
        self.burst_size = burst_size
        self.tokens = burst_size
        self.last_update = time.monotonic()
        self._lock = None  # Created lazily if needed for async
        self._sync_lock = False
        
        # Backoff tracking per symbol
        self._backoff: dict[str, float] = {}  # symbol -> next_allowed_time
        self._failures: dict[str, int] = {}   # symbol -> consecutive failures
        
    def _refill(self):
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.burst_size, self.tokens + elapsed * self.rate)
        self.last_update = now
    
    def acquire_sync(self, symbol: str = "") -> float:
        """
        Synchronous acquire - returns wait time in seconds.
        Returns 0 if can proceed immediately.
        """
        self._refill()
        
        # Check symbol-specific backoff
        if symbol and symbol in self._backoff:
            wait_until = self._backoff[symbol]
            now = time.monotonic()
            if now < wait_until:
                return wait_until - now
            else:
                # Backoff expired, clear it
                del self._backoff[symbol]
                self._failures.pop(symbol, None)
        
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        else:
            # Need to wait for a token
            wait_time = (1 - self.tokens) / self.rate
            return wait_time
    
    def wait_sync(self, symbol: str = ""):
        """Synchronous wait - blocks until request can proceed."""
        wait_time = self.acquire_sync(symbol)
        if wait_time > 0:
            time.sleep(wait_time)
    
    def record_success(self, symbol: str):
        """Record successful request - clear backoff."""
        self._failures.pop(symbol, None)
        self._backoff.pop(symbol, None)
    
    def record_failure(self, symbol: str, is_rate_limit: bool = False):
        """Record failed request - apply exponential backoff."""
        failures = self._failures.get(symbol, 0) + 1
        self._failures[symbol] = failures
        
        if is_rate_limit:
            # Exponential backoff: 1s, 2s, 4s, 8s, 16s, max 30s
            backoff_seconds = min(30, 2 ** (failures - 1))
            self._backoff[symbol] = time.monotonic() + backoff_seconds
            logger.debug("[RATE] %s backoff %ds after %d failures", symbol, backoff_seconds, failures)
    
    def get_stats(self) -> dict:
        """Get rate limiter stats."""
        return {
            "tokens_available": self.tokens,
            "symbols_in_backoff": len(self._backoff),
            "total_failures": sum(self._failures.values()),
        }


# Global rate limiter instance
_rate_limiter = RateLimiter(requests_per_second=5.0, burst_size=10)


@dataclass
class SymbolInfo:
    """Static/slow info about a symbol."""
    symbol: str
    base_currency: str = ""
    quote_currency: str = "USD"
    
    # Liquidity metrics (Clock C - updated every 10-60 min)
    volume_24h_usd: float = 0.0
    avg_spread_bps: float = 0.0
    price: float = 0.0
    trades_last_hour: int = 0
    
    # Daily baseline (Clock C)
    atr_24h: float = 0.0
    range_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    
    # Tier classification
    tier: str = "unknown"  # "large", "mid", "small", "micro"
    
    # Eligibility
    is_eligible: bool = False
    skip_reason: str = ""
    
    last_updated: Optional[datetime] = None


@dataclass
class BurstMetrics:
    """Intraday burst metrics for a symbol (Clock B - updated every minute)."""
    symbol: str
    
    # Current price
    price: float = 0.0
    
    # Volatility spikes
    vol_spike: float = 0.0      # vol_5m / median(vol_2h)
    range_spike: float = 0.0    # range_5m / median(range_2h)
    
    # Trend
    trend_15m: float = 0.0      # % return last 15m
    trend_slope: float = 0.0    # Linear slope of 1m closes
    
    # Daily abnormality
    daily_move: float = 0.0     # abs(return_1h) / ATR_24h
    range_vs_day: float = 0.0   # 1h_range / 24h_range
    
    # VWAP position
    vwap_distance: float = 0.0  # % above/below VWAP
    
    # Composite score
    burst_score: float = 0.0
    
    # Ranking
    rank: int = 0
    
    last_updated: Optional[datetime] = None


@dataclass
class HotList:
    """The hot list of burst candidates."""
    symbols: list[BurstMetrics] = field(default_factory=list)
    last_updated: Optional[datetime] = None
    
    @property
    def top(self) -> Optional[BurstMetrics]:
        return self.symbols[0] if self.symbols else None
    
    def get_symbols(self, n: int = 10) -> list[str]:
        return [m.symbol for m in self.symbols[:n]]


class SymbolScanner:
    """
    Three-clock symbol scanner for burst detection.
    
    Discovers tradable symbols, filters for eligibility,
    and ranks by burst score every minute.
    """
    
    # Eligibility thresholds (relaxed to include more coins)
    MIN_VOLUME_24H = 500_000     # $500K minimum (was $2M)
    MAX_SPREAD_BPS = 40          # 40 basis points max spread
    MIN_PRICE = 0.001            # $0.001 minimum (was $0.03) - allows memes
    MIN_TRADES_HOUR = 50         # At least 50 trades/hour
    
    # Tier thresholds (24h volume USD)
    TIER_LARGE = 500_000_000     # >$500M = large cap (BTC, ETH)
    TIER_MID = 50_000_000        # $50M-$500M = mid cap
    TIER_SMALL = 5_000_000       # $5M-$50M = small cap
    TIER_MICRO = 500_000         # $500K-$5M = micro cap (most volatile!)
    
    # Spicy small cap range (where memes live)
    SPICY_VOL_MIN = 500_000      # $500K
    SPICY_VOL_MAX = 80_000_000   # $80M
    
    # Stablecoins to ALWAYS exclude (no volatility = no profit)
    STABLECOINS = {
        "USDT-USD", "USDC-USD", "DAI-USD", "PYUSD-USD", 
        "GUSD-USD", "TUSD-USD", "USDP-USD", "EURC-USD",
        "PAX-USD", "BUSD-USD", "FRAX-USD"
    }
    
    def __init__(self):
        self.universe: dict[str, SymbolInfo] = {}
        self.burst_metrics: dict[str, BurstMetrics] = {}
        self.hot_list = HotList()
        
        self._client = None
        self._last_universe_refresh: Optional[datetime] = None
        self._last_burst_update: Optional[datetime] = None
        
        # Callbacks
        self.on_hot_list_update: Optional[Callable] = None
    
    def _init_client(self):
        """Initialize Coinbase REST client."""
        if self._client is not None:
            return True
        
        try:
            from coinbase.rest import RESTClient
            import os
            
            key = os.getenv("COINBASE_API_KEY")
            secret = os.getenv("COINBASE_API_SECRET")
            
            if key and secret:
                self._client = RESTClient(api_key=key, api_secret=secret)
                return True
        except Exception as e:
            logger.warning("[SCANNER] Failed to init client: %s", e, exc_info=True)
        
        return False
    
    async def refresh_universe(self):
        """
        Clock C: Refresh the eligible symbol universe.
        Call every 10-60 minutes.
        """
        if not self._init_client():
            logger.info("[SCANNER] No client, using default symbols")
            self._use_default_universe()
            return
        
        try:
            logger.info("[SCANNER] Refreshing universe...")
            
            # Get all products
            products = self._client.get_products()
            
            product_list = []
            if hasattr(products, 'products'):
                product_list = products.products
            elif isinstance(products, dict):
                product_list = products.get('products', [])
            
            # Filter to USD pairs
            usd_pairs = []
            for p in product_list:
                product_id = getattr(p, 'product_id', None) or (p.get('product_id', '') if isinstance(p, dict) else '')
                quote = getattr(p, 'quote_currency_id', None) or (p.get('quote_currency_id', '') if isinstance(p, dict) else '')
                
                if quote == 'USD' and product_id:
                    usd_pairs.append(p)
            
            logger.info("[SCANNER] Found %s USD pairs", len(usd_pairs))
            
            # Build universe with eligibility check
            eligible_count = 0
            for p in usd_pairs:
                product_id = getattr(p, 'product_id', None) or (p.get('product_id', '') if isinstance(p, dict) else '')
                base = getattr(p, 'base_currency_id', None) or (p.get('base_currency_id', '') if isinstance(p, dict) else '')
                
                # Get 24h stats
                volume_24h = 0.0
                price = 0.0
                
                try:
                    # Try to get product stats - Product objects use attributes, not dict access
                    vol_str = getattr(p, 'volume_24h', None)
                    price_str = getattr(p, 'price', None)
                    
                    # Fallback to dict access only if it's actually a dict
                    if vol_str is None and isinstance(p, dict):
                        vol_str = p.get('volume_24h', '0')
                    if price_str is None and isinstance(p, dict):
                        price_str = p.get('price', '0')
                    
                    volume_24h = float(vol_str) if vol_str else 0
                    price = float(price_str) if price_str else 0
                except Exception as e:
                    logger.warning("[SCANNER] Failed to parse product stats for %s: %s", product_id, e)
                    continue
                
                # Calculate volume in USD
                volume_usd = volume_24h * price if price > 0 else 0
                
                # Determine tier
                if volume_usd >= self.TIER_LARGE:
                    tier = "large"
                elif volume_usd >= self.TIER_MID:
                    tier = "mid"
                elif volume_usd >= self.TIER_SMALL:
                    tier = "small"
                else:
                    tier = "micro"
                
                # Check eligibility
                is_eligible = True
                skip_reason = ""
                
                # ALWAYS exclude stablecoins (no volatility = no profit)
                if product_id in self.STABLECOINS:
                    is_eligible = False
                    skip_reason = "Stablecoin - no volatility"
                elif volume_usd < self.MIN_VOLUME_24H:
                    is_eligible = False
                    skip_reason = f"Low volume: ${volume_usd/1e6:.1f}M"
                elif price < self.MIN_PRICE:
                    is_eligible = False
                    skip_reason = f"Price too low: ${price:.4f}"
                
                if is_eligible:
                    eligible_count += 1
                
                self.universe[product_id] = SymbolInfo(
                    symbol=product_id,
                    base_currency=base,
                    quote_currency="USD",
                    volume_24h_usd=volume_usd,
                    price=price,
                    tier=tier,
                    is_eligible=is_eligible,
                    skip_reason=skip_reason,
                    last_updated=datetime.now()
                )
            
            self._last_universe_refresh = datetime.now()
            logger.info("[SCANNER] Universe: %s eligible / %s total", eligible_count, len(self.universe))
            
        except Exception as e:
            logger.error("[SCANNER] Error refreshing universe: %s", e, exc_info=True)
            self._use_default_universe()
    
    def _use_default_universe(self):
        """Fallback to configured symbols."""
        for symbol in settings.coins:
            self.universe[symbol] = SymbolInfo(
                symbol=symbol,
                is_eligible=True,
                tier="unknown",
                last_updated=datetime.now()
            )
    
    def get_eligible_symbols(self) -> list[str]:
        """Get list of eligible symbols."""
        return [s.symbol for s in self.universe.values() if s.is_eligible]
    
    def get_spicy_smallcaps(self) -> list[str]:
        """Get symbols in the spicy small-cap range."""
        return [
            s.symbol for s in self.universe.values()
            if s.is_eligible 
            and self.SPICY_VOL_MIN <= s.volume_24h_usd <= self.SPICY_VOL_MAX
        ]
    
    def update_burst_metrics(
        self,
        symbol: str,
        candles_1m: list,  # List of Candle objects
        candles_5m: list,
        vwap: float = 0.0,
        atr_24h: float = 0.0
    ):
        """
        Clock B: Update burst metrics for a symbol.
        Call every minute with fresh candle data.
        """
        # Work with whatever data we have (minimum 3 candles)
        if len(candles_1m) < 3:
            return
        
        # Get arrays
        closes_1m = np.array([c.close for c in candles_1m])
        volumes_5m = np.array([c.volume for c in candles_5m])
        ranges_5m = np.array([c.high - c.low for c in candles_5m])
        
        price = closes_1m[-1]
        
        # Volume spike: last 5m bar vs median (use what we have)
        if len(volumes_5m) >= 2:
            vol_median = np.median(volumes_5m[:-1]) if len(volumes_5m) > 1 else volumes_5m[0]
            vol_spike = volumes_5m[-1] / vol_median if vol_median > 0 else 1.0
        else:
            vol_spike = 1.0  # Not enough data yet
        
        # Range spike: last 5m bar vs median
        if len(ranges_5m) >= 2:
            range_median = np.median(ranges_5m[:-1]) if len(ranges_5m) > 1 else ranges_5m[0]
            range_spike = ranges_5m[-1] / range_median if range_median > 0 else 1.0
        else:
            range_spike = 1.0  # Not enough data yet
        
        # Trend: use whatever window we have
        lookback = min(len(closes_1m) - 1, 15)
        if lookback >= 1:
            trend_15m = ((closes_1m[-1] / closes_1m[-lookback-1]) - 1) * 100
        else:
            trend_15m = 0
        
        # Trend slope: linear regression slope
        if len(closes_1m) >= 10:
            x = np.arange(len(closes_1m[-15:]))
            y = closes_1m[-15:]
            slope = np.polyfit(x, y, 1)[0]
            trend_slope = slope / price * 100  # Normalize as %
        else:
            trend_slope = 0
        
        # VWAP distance
        if vwap > 0:
            vwap_distance = ((price / vwap) - 1) * 100
        else:
            vwap_distance = 0
        
        # Daily abnormality
        if atr_24h > 0:
            # 1h return vs daily ATR
            if len(closes_1m) >= 60:
                return_1h = abs(closes_1m[-1] - closes_1m[-60])
                daily_move = return_1h / atr_24h
            else:
                daily_move = 0
        else:
            daily_move = 0
        
        # Range vs day
        info = self.universe.get(symbol)
        if info and info.range_24h > 0:
            range_1h = max(closes_1m[-60:]) - min(closes_1m[-60:]) if len(closes_1m) >= 60 else 0
            range_vs_day = range_1h / info.range_24h
        else:
            range_vs_day = 0
        
        # Composite burst score
        # Higher when: vol spike + range spike + positive trend
        burst_score = vol_spike * range_spike * max(trend_15m / 100, 0.01)
        
        # Bonus for VWAP position (price above VWAP is bullish)
        if vwap_distance > 0:
            burst_score *= (1 + min(vwap_distance, 5) / 100)
        
        self.burst_metrics[symbol] = BurstMetrics(
            symbol=symbol,
            price=price,
            vol_spike=vol_spike,
            range_spike=range_spike,
            trend_15m=trend_15m,
            trend_slope=trend_slope,
            daily_move=daily_move,
            range_vs_day=range_vs_day,
            vwap_distance=vwap_distance,
            burst_score=burst_score,
            last_updated=datetime.now()
        )
    
    def compute_hot_list(self, top_n: int = 10) -> HotList:
        """
        Rank all symbols by burst score and produce hot list.
        Call after updating all burst metrics.
        """
        # Get all metrics, filter for eligible symbols
        eligible_metrics = [
            m for m in self.burst_metrics.values()
            if m.symbol in self.universe 
            and self.universe[m.symbol].is_eligible
            and m.burst_score > 0
        ]
        
        # Sort by burst score descending
        eligible_metrics.sort(key=lambda x: x.burst_score, reverse=True)
        
        # Assign ranks
        for i, m in enumerate(eligible_metrics):
            m.rank = i + 1
        
        self.hot_list = HotList(
            symbols=eligible_metrics[:top_n],
            last_updated=datetime.now()
        )
        
        self._last_burst_update = datetime.now()
        
        if self.on_hot_list_update:
            self.on_hot_list_update(self.hot_list)
        
        return self.hot_list
    
    def get_ranked_universe(self, top_k: int = 50) -> list[dict]:
        """
        Get top-K ranked symbols for trading universe.
        Returns list of {product_id, rank, score, tier, sector}.
        """
        from logic.intelligence import SECTOR_MAP
        
        eligible = [
            m for m in self.burst_metrics.values()
            if m.symbol in self.universe 
            and self.universe[m.symbol].is_eligible
        ]
        
        # Sort by burst score
        eligible.sort(key=lambda x: x.burst_score, reverse=True)
        
        result = []
        for i, m in enumerate(eligible[:top_k]):
            info = self.universe.get(m.symbol)
            base = m.symbol.split("-")[0]
            result.append({
                "product_id": m.symbol,
                "rank": i + 1,
                "score": round(m.burst_score, 1),
                "tier": info.tier if info else "unknown",
                "sector": SECTOR_MAP.get(base, "other")
            })
        
        return result
    
    def get_focus_symbol(self) -> Optional[str]:
        """Get the current #1 focus symbol."""
        if self.hot_list.top:
            return self.hot_list.top.symbol
        return None
    
    def refresh_spread_snapshots(self, symbols: list[str]):
        """
        Fetch best bid/ask via REST for a set of symbols to estimate spreads.
        Safe to call periodically; errors are swallowed.
        """
        if not self._init_client():
            return
        
        for sym in symbols:
            info = self.universe.get(sym)
            try:
                # Advanced Trade book endpoint; limit=1 keeps it light
                book = self._client.get_product_book(product_id=sym, limit=1)
                bids = getattr(book, "bids", None) or book.get("bids", [])
                asks = getattr(book, "asks", None) or book.get("asks", [])
                best_bid = float(bids[0]["price"]) if bids else 0.0
                best_ask = float(asks[0]["price"]) if asks else 0.0
                mid = (best_bid + best_ask) / 2 if (best_bid and best_ask) else 0.0
                spread_bps = ((best_ask - best_bid) / mid * 10000) if mid > 0 else 0.0
                if info:
                    info.avg_spread_bps = spread_bps
                    info.price = mid or info.price
                    info.last_updated = datetime.now()
            except Exception:
                continue
    
    def fetch_history(
        self,
        symbol: str,
        granularity_s: int = 60,
        lookback_minutes: int = 90,
        max_retries: int = 3
    ) -> list[Candle]:
        """
        Fetch historical candles for warmup/backfill.
        Returns a list of Candle objects sorted oldest→newest.
        
        Includes rate limiting and exponential backoff for 429 errors.
        """
        # Map seconds to Coinbase granularity strings
        granularity_map = {
            60: "ONE_MINUTE",
            300: "FIVE_MINUTE",
            900: "FIFTEEN_MINUTE",
            1800: "THIRTY_MINUTE",
            3600: "ONE_HOUR",
            7200: "TWO_HOUR",
            21600: "SIX_HOUR",
            86400: "ONE_DAY",
        }
        granularity = granularity_map.get(granularity_s, "ONE_MINUTE")
        
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=lookback_minutes)
        
        for attempt in range(max_retries):
            try:
                # Rate limit before making request
                _rate_limiter.wait_sync(symbol)
                
                # Use public endpoint (works without auth)
                from coinbase.rest import RESTClient
                public_client = RESTClient()  # Unauthenticated for public data
                
                resp = public_client.get_public_candles(
                    product_id=symbol,
                    start=str(int(start.timestamp())),
                    end=str(int(end.timestamp())),
                    granularity=granularity
                )
                raw = getattr(resp, "candles", None) or []
                candles: list[Candle] = []
                for c in raw:
                    # Coinbase returns objects with attributes
                    start_ts = getattr(c, "start", 0)
                    open_p = getattr(c, "open", 0)
                    high_p = getattr(c, "high", 0)
                    low_p = getattr(c, "low", 0)
                    close_p = getattr(c, "close", 0)
                    vol_p = getattr(c, "volume", 0)
                    
                    # start is Unix timestamp as string
                    ts = datetime.fromtimestamp(int(start_ts), tz=timezone.utc)
                    
                    candles.append(Candle(
                        timestamp=ts,
                        open=float(open_p),
                        high=float(high_p),
                        low=float(low_p),
                        close=float(close_p),
                        volume=float(vol_p)
                    ))
                
                candles.sort(key=lambda x: x.timestamp)
                
                # Success - clear any backoff
                _rate_limiter.record_success(symbol)
                return candles
                
            except Exception as e:
                error_str = str(e)
                is_rate_limit = "429" in error_str or "Too Many Requests" in error_str
                
                # Record failure for backoff
                _rate_limiter.record_failure(symbol, is_rate_limit=is_rate_limit)
                
                if is_rate_limit and attempt < max_retries - 1:
                    # Wait with exponential backoff before retry
                    backoff = min(30, 2 ** attempt)
                    logger.debug("[SCANNER] Rate limited on %s, waiting %ds (attempt %d/%d)", 
                               symbol, backoff, attempt + 1, max_retries)
                    time.sleep(backoff)
                    continue
                elif attempt < max_retries - 1:
                    # Non-rate-limit error, brief pause then retry
                    time.sleep(0.5)
                    continue
                else:
                    # Final attempt failed
                    if not is_rate_limit:
                        logger.warning("[SCANNER] History fetch failed for %s: %s", symbol, e)
                    return []
        
        return []
    
    def should_refresh_universe(self) -> bool:
        """Check if universe needs refresh (every 30 min)."""
        if self._last_universe_refresh is None:
            return True
        return (datetime.now() - self._last_universe_refresh) > timedelta(minutes=30)
    
    def get_tier_symbols(self, tier: str) -> list[str]:
        """Get symbols for a specific tier."""
        return [s.symbol for s in self.universe.values() if s.tier == tier and s.is_eligible]
    
    def print_hot_list(self):
        """Print current hot list to console."""
        if not self.hot_list.symbols:
            logger.info("[HOT LIST] Empty")
            return
        
        logger.info("[HOT LIST] Top Burst Candidates:")
        
        for m in self.hot_list.symbols[:10]:
            logger.info(
                "%s %s $%s burst=%s vol=%sx rng=%sx trend=%s%%",
                f"{m.rank:>2}",
                f"{m.symbol:<10}",
                f"{m.price:>9.4f}",
                f"{m.burst_score:>8.2f}",
                f"{m.vol_spike:>5.1f}",
                f"{m.range_spike:>5.1f}",
                f"{m.trend_15m:>+7.2f}",
            )
