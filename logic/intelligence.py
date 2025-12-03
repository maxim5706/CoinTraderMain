"""
Intelligence Layer - Smart entry filtering and position management.

This module adds decision intelligence on top of the burst-flag strategy:
1. Entry confidence scoring (rules-based, ML-ready)
2. Trend filtering (BTC regime + symbol trend)
3. Position diversification (sector limits, correlation)
4. Hold spacing (global cooldown)
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date
from typing import Optional, Dict, List
import numpy as np

from core.config import settings
from core.models import Signal, SignalType

# Canonical gate ordering for entry decisions. Tests lock this to prevent regressions.
CANONICAL_GATE_ORDER = [
    "warmth",
    "limits",
    "spread",
    "score_regime",
    "risk_reward",
    "budget",
    "ml_boost",
]


# Sector classification for diversification
SECTOR_MAP = {
    # Layer 1 / Major
    "BTC": "major", "ETH": "major",
    
    # Alt Layer 1s (highly correlated!)
    "SOL": "L1", "AVAX": "L1", "ADA": "L1", "DOT": "L1", "NEAR": "L1",
    "APT": "L1", "SUI": "L1", "SEI": "L1", "INJ": "L1", "TIA": "L1",
    "ATOM": "L1", "ALGO": "L1", "HBAR": "L1", "ICP": "L1",
    
    # Solana Ecosystem (very correlated to SOL)
    "ORCA": "sol_eco", "TNSR": "sol_eco", "JTO": "sol_eco", "JUP": "sol_eco",
    "PYTH": "sol_eco", "BONK": "sol_eco", "WIF": "sol_eco",
    
    # DeFi
    "UNI": "defi", "AAVE": "defi", "COMP": "defi", "MKR": "defi",
    "LINK": "defi", "SNX": "defi", "SUSHI": "defi", "CRV": "defi",
    "LDO": "defi", "FXS": "defi", "LQTY": "defi", "ONDO": "defi",
    "ENA": "defi", "AERO": "defi", "SUPER": "defi",
    
    # AI / Compute
    "FET": "ai", "RNDR": "ai", "TAO": "ai", "AGIX": "ai",
    
    # Meme (risky, but can pump together)
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "FARTCOIN": "meme",
    "FLOKI": "meme", "MEME": "meme", "PENGU": "meme",
    
    # Gaming / Metaverse
    "AXS": "gaming", "SAND": "gaming", "MANA": "gaming", "IMX": "gaming",
    "GALA": "gaming", "ENJ": "gaming",
    
    # Infrastructure
    "FIL": "infra", "AR": "infra", "STORJ": "infra", "GRT": "infra",
    "QNT": "infra",
    
    # Exchange tokens
    "BNB": "exchange", "OKB": "exchange",
    
    # Privacy
    "ZEC": "privacy", "XMR": "privacy",
    
    # Payments
    "XLM": "payments", "XRP": "payments", "LTC": "payments", "BCH": "payments",
}

# Correlation groups - symbols that move together
CORRELATION_GROUPS = {
    "sol_heavy": ["SOL", "ORCA", "TNSR", "JTO", "BONK", "WIF"],  # SOL ecosystem
    "eth_heavy": ["ETH", "LDO", "AAVE", "UNI", "LINK"],  # ETH ecosystem
    "l1_basket": ["SOL", "AVAX", "ADA", "SUI", "APT", "SEI"],  # Alt L1s
    "meme_basket": ["DOGE", "SHIB", "PEPE", "FARTCOIN", "BONK"],  # Memes
}


@dataclass
class EntryScore:
    """Detailed entry confidence breakdown."""
    symbol: str
    total_score: float = 0.0
    
    # Component scores (0-20 each, max 100)
    trend_score: float = 0.0       # 15m trend positive
    volume_score: float = 0.0      # Volume spike magnitude
    vwap_score: float = 0.0        # Above VWAP
    range_score: float = 0.0       # Range spike
    tier_score: float = 0.0        # Spicy tier bonus
    spread_score: float = 0.0      # Tight spread bonus
    
    # ML/Live scoring (optional boost)
    ml_score: float = 0.0          # -1 to 1 from live scorer
    ml_confidence: float = 0.0     # Model confidence
    ml_boost: float = 0.0          # Score boost from ML (0-10)
    
    # Key indicators for dashboard
    rsi: float = 50.0              # RSI 14
    macd_signal: float = 0.0       # MACD histogram
    bb_position: float = 0.5       # Bollinger band position
    
    # Filters (must pass)
    btc_trend_ok: bool = False     # BTC not dumping
    symbol_trend_ok: bool = False  # Symbol trend positive
    not_overbought: bool = True    # Not too extended
    
    # Reasoning
    reasons: List[str] = field(default_factory=list)
    
    @property
    def should_enter(self) -> bool:
        """
        Check if score meets entry threshold from settings.
        
        Smart BTC regime handling:
        - Normal: score >= entry_score_min
        - Caution: score >= entry_score_min + 10 (stricter)
        - Risk Off: only if alt is diverging with score >= entry_score_min + 15
        """
        from core.config import settings
        from core.profiles import is_test_profile
        base_min = settings.entry_score_min
        
        # Test profile: bypass most gates, just check score
        if is_test_profile(settings.profile):
            return self.total_score >= base_min
        
        # Base requirements (production modes)
        if not self.symbol_trend_ok or not self.not_overbought:
            return False
        if self.volume_score < 5:
            return False
        
        # BTC regime-adjusted thresholds
        if self.btc_trend_ok:
            # Normal market - use configured threshold
            return self.total_score >= base_min
        elif self.btc_regime == "caution":
            # Caution mode - higher bar but still allow good setups
            return self.total_score >= base_min + 10
        else:
            # Risk off - only allow strong divergence plays
            # Alt popping while BTC dumps = strength signal
            return self.total_score >= base_min + 15 and self.trend_score >= 15
    
    # For regime tracking in should_enter
    btc_regime: str = "normal"


@dataclass
class PositionLimits:
    """Position limit configuration - PLAY-BASED, not time-based."""
    max_per_symbol: float = 10.0      # Max USD per symbol ($5 × 2 entries max)
    max_per_sector: int = 4           # Max 4 per sector (allow L1 stacking)
    max_per_corr_group: int = 2       # Max 2 in any correlation group
    max_total_positions: int = 10     # Max active plays (not time-based!)
    max_weak_plays: int = 5           # Max weak plays before pausing new entries
    global_cooldown_sec: int = 30     # Reduced: 30s between trades (play quality matters more)
    symbol_cooldown_sec: int = 300    # 5 min per symbol (prevent same-symbol spam)
    daily_loss_limit_pct: float = 5.0 # Stop trading after -5% daily drawdown
    daily_loss_limit_usd: float = 25.0  # Or after $25 loss (whichever first)


class IntelligenceLayer:
    """
    Smart decision layer for entry filtering and position management.
    
    Single source of truth for:
    - Live indicators (updated per candle)
    - ML scores (computed once per candle update)
    - Entry scoring with clean gate order
    """
    
    # Market regime thresholds
    BTC_DUMP_THRESHOLD = -1.5  # BTC down 1.5% = don't trade
    BTC_CRASH_THRESHOLD = -3.0  # BTC down 3% = emergency mode
    
    def __init__(self):
        self.limits = PositionLimits()
        self._last_trade_time: Optional[datetime] = None
        self._btc_trend_1h: float = 0.0  # BTC trend for regime
        self._btc_trend_15m: float = 0.0  # Short-term BTC trend
        self._btc_price: float = 0.0
        self._btc_last_update: Optional[datetime] = None
        self._sector_positions: Dict[str, int] = {}  # Sector -> count
        self._market_regime: str = "normal"  # normal, caution, risk_off
        
        # Live indicators + ML (single source of truth)
        self.live_indicators: Dict[str, 'LiveIndicators'] = {}
        self.live_ml: Dict[str, 'LiveMLResult'] = {}
        
        # Daily loss tracking
        self._daily_realized_pnl: float = 0.0
        self._daily_reset_date: Optional[date] = None
        self._trading_halted: bool = False
        
        # Strategy performance tracking
        self._strategy_stats: Dict[str, Dict[str, int]] = {}  # strategy_id -> {wins, losses, total_pnl}
        
    def get_sector(self, symbol: str) -> str:
        """Get sector for a symbol."""
        base = symbol.split("-")[0] if "-" in symbol else symbol
        return SECTOR_MAP.get(base, "other")
    
    def update_btc_trend(self, trend_1h: float, trend_15m: float = 0.0, price: float = 0.0):
        """Update BTC trend for regime detection."""
        self._btc_trend_1h = trend_1h
        self._btc_trend_15m = trend_15m
        self._btc_price = price
        self._btc_last_update = datetime.now(timezone.utc)
        
        # Determine market regime
        if trend_1h <= self.BTC_CRASH_THRESHOLD:
            self._market_regime = "risk_off"
        elif trend_1h <= self.BTC_DUMP_THRESHOLD:
            self._market_regime = "caution"
        else:
            self._market_regime = "normal"
    
    def fetch_btc_trend(self) -> bool:
        """Fetch BTC trend from exchange. Call periodically."""
        try:
            from coinbase.rest import RESTClient
            import os
            
            client = RESTClient(
                api_key=os.getenv("COINBASE_API_KEY"),
                api_secret=os.getenv("COINBASE_API_SECRET")
            )
            
            # Get BTC candles for last hour
            from datetime import timedelta
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=1)
            
            candles = client.get_public_candles(
                product_id="BTC-USD",
                start=str(int(start.timestamp())),
                end=str(int(end.timestamp())),
                granularity="FIVE_MINUTE"
            )
            
            candle_list = getattr(candles, 'candles', [])
            if len(candle_list) >= 2:
                # Sort by time
                candle_list = sorted(candle_list, key=lambda x: int(getattr(x, 'start', 0)))
                
                first_close = float(getattr(candle_list[0], 'close', 0))
                last_close = float(getattr(candle_list[-1], 'close', 0))
                
                if first_close > 0:
                    trend_1h = ((last_close / first_close) - 1) * 100
                    
                    # 15m trend (last 3 candles)
                    if len(candle_list) >= 4:
                        mid_close = float(getattr(candle_list[-4], 'close', first_close))
                        trend_15m = ((last_close / mid_close) - 1) * 100
                    else:
                        trend_15m = trend_1h / 4
                    
                    self.update_btc_trend(trend_1h, trend_15m, last_close)
                    return True
            
            return False
        except Exception as e:
            print(f"[INTEL] Failed to fetch BTC trend: {e}")
            return False
    
    @property
    def is_safe_to_trade(self) -> bool:
        """Check if market regime allows new trades."""
        return self._market_regime == "normal"
    
    @property
    def regime_status(self) -> str:
        """Get human-readable regime status."""
        fg_str = f" F&G:{self._fear_greed}" if hasattr(self, '_fear_greed') and self._fear_greed else ""
        if self._market_regime == "risk_off":
            return f"🔴 RISK OFF (BTC {self._btc_trend_1h:+.1f}%{fg_str})"
        elif self._market_regime == "caution":
            return f"🟡 CAUTION (BTC {self._btc_trend_1h:+.1f}%{fg_str})"
        elif self._market_regime == "bullish":
            return f"🟢 BULLISH (BTC {self._btc_trend_1h:+.1f}%{fg_str})"
        else:
            return f"🟢 NORMAL (BTC {self._btc_trend_1h:+.1f}%{fg_str})"
    
    def fetch_fear_greed(self) -> Optional[int]:
        """Fetch Fear & Greed index from alternative.me (free API)."""
        try:
            import urllib.request
            import json
            
            url = "https://api.alternative.me/fng/?limit=1"
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read().decode())
                if data and "data" in data and len(data["data"]) > 0:
                    value = int(data["data"][0]["value"])
                    classification = data["data"][0]["value_classification"]
                    self._fear_greed = value
                    self._fear_greed_class = classification
                    self._fear_greed_updated = datetime.now(timezone.utc)
                    
                    # Adjust regime based on extreme fear/greed
                    if value <= 20:  # Extreme fear
                        # Could be buying opportunity OR more downside
                        pass  # Don't change regime, just track
                    elif value >= 80:  # Extreme greed
                        # Potential top, be cautious
                        if self._market_regime == "normal":
                            self._market_regime = "caution"
                    
                    return value
            return None
        except Exception as e:
            # Silent fail - this is optional data
            return None
    
    def get_fear_greed(self) -> Optional[dict]:
        """Get cached Fear & Greed data."""
        if not hasattr(self, '_fear_greed') or self._fear_greed is None:
            return None
        return {
            "value": self._fear_greed,
            "classification": getattr(self, '_fear_greed_class', 'Unknown'),
            "updated": getattr(self, '_fear_greed_updated', None),
        }
    
    # ─────────────────────────────────────────────────────────────────────────
    # Time-of-Day Awareness
    # ─────────────────────────────────────────────────────────────────────────
    
    def get_session_info(self) -> dict:
        """
        Get current trading session info.
        
        Sessions (UTC):
        - Asia:       00:00-08:00 (good liquidity)
        - Europe:     08:00-14:00 (good liquidity)
        - US:         14:00-21:00 (best liquidity)
        - Dead zone:  21:00-00:00 (low liquidity)
        """
        now = datetime.now(timezone.utc)
        hour = now.hour
        
        if 0 <= hour < 8:
            session = "asia"
            multiplier = 1.0  # Full trading
        elif 8 <= hour < 14:
            session = "europe"
            multiplier = 1.0  # Full trading
        elif 14 <= hour < 21:
            session = "us"
            multiplier = 1.0  # Best liquidity
        else:  # 21-24
            session = "dead_zone"
            multiplier = 0.6  # Reduce size in dead zone
        
        return {
            "session": session,
            "hour_utc": hour,
            "size_multiplier": multiplier,
            "is_active": multiplier >= 1.0,
        }
    
    def get_size_multiplier(self) -> float:
        """Get position size multiplier based on time of day."""
        return self.get_session_info()["size_multiplier"]
    
    def update_live_indicators(self, symbol: str, indicators):
        """
        Update cached live indicators for a symbol.
        Called once per candle from ws_collector.
        Also computes ML score (once, not repeated).
        """
        if indicators is None:
            return
        
        self.live_indicators[symbol] = indicators
        
        if indicators.is_ready:
            try:
                from logic.live_features import live_scorer
                self.live_ml[symbol] = live_scorer.score_from_indicators(indicators)
            except Exception as e:
                print(f"[ML] Error scoring {symbol}: {e}")
    
    def get_live_ml(self, symbol: str, max_stale_seconds: float = 180):
        """
        Get cached ML result for symbol.
        Returns None if stale (older than max_stale_seconds).
        """
        ml = self.live_ml.get(symbol)
        if ml and ml.is_stale(max_stale_seconds):
            return None  # Stale ML is ignored
        return ml
    
    def get_live_indicators(self, symbol: str, max_stale_seconds: float = 120):
        """Get cached indicators for symbol.
        
        Returns None if stale (older than max_stale_seconds).
        """
        ind = self.live_indicators.get(symbol)
        if ind and ind.is_stale(max_stale_seconds):
            return None  # Stale indicators are ignored
        return ind
    
    def update_sector_counts(self, positions: dict):
        """Update sector position counts from current positions."""
        self._sector_positions = {}
        for symbol in positions.keys():
            sector = self.get_sector(symbol)
            self._sector_positions[sector] = self._sector_positions.get(sector, 0) + 1
    
    def score_entry(
        self,
        signal: Signal,
        burst_metrics: dict,  # vol_spike, range_spike, trend_15m, vwap_distance
        current_positions: dict,
    ) -> EntryScore:
        """
        Calculate entry confidence score with CANONICAL GATE ORDER.
        
        Gate order (DO NOT MODIFY ORDER):
        1) Warmth gate - is_symbol_warm()
        2) BTC regime gate - block if risk_off (allow existing position mgmt)
        3) Pattern edge score - burst/flag base score
        4) R:R gate - geometry check
        5) Execution reality gate - spread, liquidity
        6) ML gate/boost - LAST, uses cached ML only
        
        Returns EntryScore with breakdown and should_enter decision.
        """
        score = EntryScore(symbol=signal.symbol)
        
        # Use strategy's pre-computed score if available (from orchestrator)
        strategy_confidence = getattr(signal, "confidence", 0.0)
        strategy_id = getattr(signal, "strategy_id", "")
        if strategy_confidence > 0 and strategy_id:
            # Strategy already scored this - use that score directly
            score.total_score = strategy_confidence * 100  # Convert 0-1 to 0-100
            score.reasons.append(f"{strategy_id}: {strategy_confidence:.0%}")
            
            # Set all gates to pass for strategy signals (strategy already validated)
            score.btc_regime = self._market_regime
            score.btc_trend_ok = (self._market_regime == "normal")
            score.symbol_trend_ok = True
            score.not_overbought = True
            score.volume_score = 10  # Ensure volume gate passes
            
            # Apply ML boost if available
            ml = self.get_live_ml(signal.symbol)
            if ml and not ml.is_stale():
                score.ml_score = ml.raw_score
                if ml.raw_score > 0.6:
                    score.ml_boost = (ml.raw_score - 0.5) * 20
                    score.total_score += score.ml_boost
                    score.reasons.append(f"ML +{score.ml_boost:.0f}")
            
            return score
        
        # Extract metrics
        vol_spike = burst_metrics.get("vol_spike", 1.0)
        range_spike = burst_metrics.get("range_spike", 1.0)
        trend_15m = burst_metrics.get("trend_15m", 0.0)
        vwap_dist = burst_metrics.get("vwap_distance", 0.0)
        spread_bps = burst_metrics.get("spread_bps", 50.0)
        tier = burst_metrics.get("tier", "unknown")
        
        # 1. Trend score (0-20)
        if trend_15m >= 2.0:
            score.trend_score = 20
            score.reasons.append(f"Strong trend +{trend_15m:.1f}%")
        elif trend_15m >= 1.0:
            score.trend_score = 15
            score.reasons.append(f"Good trend +{trend_15m:.1f}%")
        elif trend_15m >= 0.5:
            score.trend_score = 10
            score.reasons.append(f"Mild trend +{trend_15m:.1f}%")
        elif trend_15m > 0:
            score.trend_score = 5
            score.reasons.append(f"Weak trend +{trend_15m:.1f}%")
        else:
            score.reasons.append(f"Flat/down trend {trend_15m:+.1f}%")
        
        # 2. Volume score (0-20)
        if vol_spike >= 5.0:
            score.volume_score = 20
            score.reasons.append(f"Massive volume {vol_spike:.1f}x")
        elif vol_spike >= 3.0:
            score.volume_score = 15
            score.reasons.append(f"Strong volume {vol_spike:.1f}x")
        elif vol_spike >= 2.0:
            score.volume_score = 10
        elif vol_spike >= 1.5:
            score.volume_score = 5
            score.reasons.append(f"Low volume {vol_spike:.1f}x")
        else:
            score.reasons.append(f"Weak volume {vol_spike:.1f}x (need 1.5x)")
        
        # 3. VWAP score (0-20)
        if vwap_dist > 0.5:
            score.vwap_score = 20
            score.reasons.append("Above VWAP ✓")
        elif vwap_dist > 0:
            score.vwap_score = 15
        elif vwap_dist > -0.3:
            score.vwap_score = 10
        else:
            score.vwap_score = 0
            score.reasons.append("Below VWAP ✗")
        
        # 4. Range score (0-15)
        if range_spike >= 3.0:
            score.range_score = 15
        elif range_spike >= 2.0:
            score.range_score = 10
        elif range_spike >= 1.5:
            score.range_score = 5
        
        # 5. Tier score (0-20) - Prefer volatile micro/small caps for bigger moves
        if tier == "micro":
            score.tier_score = 20
            score.reasons.append("Micro cap volatility 🚀")
        elif tier == "small":
            score.tier_score = 15
            score.reasons.append("Spicy smallcap 🌶️")
        elif tier == "mid":
            score.tier_score = 8
        elif tier == "large":
            score.tier_score = 3  # BTC/ETH move slow, hard to hit TPs
        
        # 6. Spread score (0-15) - CRITICAL for profitability!
        if spread_bps < 5:
            score.spread_score = 15
            score.reasons.append("Tight spread ✓")
        elif spread_bps < 10:
            score.spread_score = 10
        elif spread_bps < 15:
            score.spread_score = 5
        else:
            score.spread_score = 0
            score.reasons.append(f"Wide spread {spread_bps:.0f}bps ⚠️")
        
        # 7. Price volatility bonus - Low price coins move more % wise!
        price = getattr(signal, 'price', 0) or burst_metrics.get('price', 0)
        price_bonus = 0
        if 0 < price < 0.10:
            price_bonus = 15
            score.reasons.append("Ultra-low price = high % moves 🚀")
        elif price < 1.0:
            price_bonus = 10
            score.reasons.append("Low price = good volatility")
        elif price < 10.0:
            price_bonus = 5
        elif price > 1000:
            price_bonus = -5  # Slow movers like BTC
            score.reasons.append("High price = slow mover")
        
        # Calculate base total
        score.total_score = (
            score.trend_score +
            score.volume_score +
            score.vwap_score +
            score.range_score +
            score.tier_score +
            score.spread_score +
            price_bonus  # Low price = high volatility bonus
        )
        
        # ================================================================
        # QUALITY FILTERS: Use live indicators for smarter decisions
        # These can add bonuses or penalties based on indicator quality
        # ================================================================
        ind = self.get_live_indicators(signal.symbol)
        
        if ind and ind.is_ready:
            quality_adjust = 0
            
            # --- RSI Filters ---
            # Avoid buying overbought (RSI > 75) - likely to reverse
            if ind.rsi_14 > 75:
                quality_adjust -= 15
                score.not_overbought = False
                score.reasons.append(f"⚠️ RSI overbought {ind.rsi_14:.0f}")
            elif ind.rsi_14 > 70:
                quality_adjust -= 8
                score.reasons.append(f"RSI extended {ind.rsi_14:.0f}")
            # Bonus for RSI in sweet spot (50-65) - momentum but not exhausted
            elif 50 <= ind.rsi_14 <= 65:
                quality_adjust += 5
                score.reasons.append(f"RSI healthy {ind.rsi_14:.0f} ✓")
            
            # --- MACD Momentum Confirmation ---
            # Bonus for positive and increasing MACD histogram
            if ind.macd_histogram > 0:
                quality_adjust += 5
                score.reasons.append("MACD bullish ✓")
            elif ind.macd_histogram < -0.001:  # Negative momentum
                quality_adjust -= 5
                score.reasons.append("MACD bearish ✗")
            
            # --- EMA Trend Confirmation ---
            # Price above EMAs = trend confirmation
            if ind.price > ind.ema9 > ind.ema21:
                quality_adjust += 5
                score.reasons.append("EMA stack bullish ✓")
            elif ind.price < ind.ema9 < ind.ema21:
                quality_adjust -= 10
                score.reasons.append("EMA stack bearish ✗")
            
            # --- Bollinger Position ---
            # Avoid chasing when already at upper band (bb_position > 0.9)
            if ind.bb_position > 0.9:
                quality_adjust -= 10
                score.reasons.append(f"At Bollinger top - don't chase")
            # Bonus for entries in middle zone (less risky)
            elif 0.4 <= ind.bb_position <= 0.7:
                quality_adjust += 3
            
            # --- Chop Detection ---
            # Heavy penalty for choppy price action - hard to profit
            if ind.is_choppy or ind.chop_score > 0.6:
                quality_adjust -= 15
                score.reasons.append(f"🌀 Choppy action ({ind.chop_score:.1f}) - skip")
            elif ind.chop_score > 0.4:
                quality_adjust -= 5
                score.reasons.append("Some chop detected")
            
            # --- Volume Quality ---
            # Bonus for volume confirming (buy pressure > 0.6)
            if ind.buy_pressure > 0.65:
                quality_adjust += 5
                score.reasons.append(f"Strong buy pressure {ind.buy_pressure:.0%} ✓")
            elif ind.buy_pressure < 0.4:
                quality_adjust -= 5
                score.reasons.append(f"Weak buy pressure {ind.buy_pressure:.0%}")
            
            # --- OBV Trend ---
            # Volume should confirm price moves
            if ind.obv_slope > 0 and trend_15m > 0:
                quality_adjust += 3  # Volume confirms trend
            elif ind.obv_slope < 0 and trend_15m > 0:
                quality_adjust -= 5  # Divergence warning
                score.reasons.append("OBV divergence ⚠️")
            
            # --- Multi-Timeframe Alignment ---
            # 5m and 15m trends should agree for higher conviction
            trend_5m = ind.trend_5m if hasattr(ind, 'trend_5m') else 0
            if trend_5m > 0 and trend_15m > 0:
                quality_adjust += 5
                score.reasons.append("MTF aligned ✓")
            elif (trend_5m > 0 and trend_15m < 0) or (trend_5m < 0 and trend_15m > 0):
                quality_adjust -= 5
                score.reasons.append("MTF conflict ⚠️")
            
            # --- Momentum Acceleration ---
            # Bonus if momentum is increasing (MACD histogram growing)
            if hasattr(ind, 'momentum_10') and ind.momentum_10 > 0.5:
                quality_adjust += 3
                score.reasons.append(f"Momentum +{ind.momentum_10:.1f}% ✓")
            elif hasattr(ind, 'momentum_10') and ind.momentum_10 < -0.5:
                quality_adjust -= 3
                score.reasons.append("Momentum fading")
            
            # --- ATR Expansion (Breakout Confirmation) ---
            # If ATR is expanding, it's a real move not noise
            if ind.atr_pct > 0 and ind.bb_width > 0:
                # Normalized volatility check
                if vol_spike >= 2.0 and ind.atr_pct > 0.01:  # Vol spike + expanding range
                    quality_adjust += 5
                    score.reasons.append("Breakout confirmed ✓")
            
            # --- Time of Day Filter ---
            # Crypto volume peaks at certain hours (US market hours)
            from datetime import datetime, timezone
            hour_utc = datetime.now(timezone.utc).hour
            # Low volume hours: 2-6 UTC (late night US)
            if 2 <= hour_utc <= 6:
                quality_adjust -= 5
                score.reasons.append("Low volume hours ⚠️")
            # High volume hours: 13-21 UTC (US market hours)
            elif 13 <= hour_utc <= 21:
                quality_adjust += 3
                score.reasons.append("Peak hours ✓")
            
            # --- Recent High/Low Test ---
            # Bonus for bouncing off support, penalty for failing at resistance
            if ind.bb_position < 0.15 and trend_5m > 0:
                quality_adjust += 5
                score.reasons.append("Bouncing off support ✓")
            elif ind.bb_position > 0.85 and trend_5m < 0:
                quality_adjust -= 5
                score.reasons.append("Rejected at resistance ⚠️")
            
            # === PRO FEATURES ===
            
            # --- Order Flow Imbalance ---
            # Positive imbalance = more bids = bullish
            imbalance = getattr(ind, 'bid_ask_imbalance', 0)
            if imbalance > 0.3:
                quality_adjust += 5
                score.reasons.append(f"Order flow bullish +{imbalance:.0%} ✓")
            elif imbalance < -0.3:
                quality_adjust -= 5
                score.reasons.append(f"Order flow bearish {imbalance:.0%} ⚠️")
            
            # --- Volatility Regime ---
            # Bonus for expanding volatility on breakout, penalty for contracting
            is_vol_expanding = getattr(ind, 'is_volatility_expanding', False)
            vol_pct = getattr(ind, 'volatility_percentile', 0.5)
            if is_vol_expanding and vol_spike >= 1.5:
                quality_adjust += 5
                score.reasons.append("Vol expanding + spike ✓")
            elif vol_pct < 0.2:
                quality_adjust -= 3
                score.reasons.append("Low volatility regime")
            
            # --- RSI Divergence ---
            # Bullish divergence = price lower but RSI higher (reversal signal)
            rsi_div = getattr(ind, 'rsi_divergence', 0)
            if rsi_div == 1:  # Bullish divergence
                quality_adjust += 8
                score.reasons.append("Bullish divergence ✓✓")
            elif rsi_div == -1:  # Bearish divergence
                quality_adjust -= 8
                score.reasons.append("Bearish divergence ⚠️⚠️")
            
            # --- Momentum Alignment ---
            # Price and momentum should agree for high conviction
            momentum_align = getattr(ind, 'price_momentum_align', True)
            if not momentum_align:
                quality_adjust -= 5
                score.reasons.append("Price/momentum misaligned")
            
            # === HIGHER TIMEFRAME ANALYSIS ===
            
            # --- Daily Trend Alignment ---
            # Trading WITH the daily trend is much higher probability
            trend_1d = getattr(ind, 'trend_1d', 0)
            trend_7d = getattr(ind, 'trend_7d', 0)
            if trend_1d > 2.0:  # Strong daily uptrend (>2%)
                quality_adjust += 8
                score.reasons.append(f"Daily trend +{trend_1d:.1f}% ✓✓")
            elif trend_1d > 0.5:  # Moderate daily uptrend
                quality_adjust += 4
                score.reasons.append(f"Daily trend +{trend_1d:.1f}% ✓")
            elif trend_1d < -2.0:  # Strong daily downtrend
                quality_adjust -= 8
                score.reasons.append(f"Daily downtrend {trend_1d:.1f}% ⚠️⚠️")
            elif trend_1d < -0.5:  # Moderate daily downtrend
                quality_adjust -= 4
                score.reasons.append(f"Daily trend {trend_1d:.1f}% ⚠️")
            
            # --- Weekly Context ---
            if trend_7d > 5.0:  # Strong weekly uptrend
                quality_adjust += 5
                score.reasons.append(f"Weekly +{trend_7d:.1f}% ✓")
            elif trend_7d < -5.0:  # Strong weekly downtrend
                quality_adjust -= 5
                score.reasons.append(f"Weekly {trend_7d:.1f}% ⚠️")
            
            # --- Daily Range Position ---
            # Buying near daily lows is better
            daily_range_pos = getattr(ind, 'daily_range_position', 0.5)
            if daily_range_pos < 0.2:  # Near daily low
                quality_adjust += 5
                score.reasons.append("Near daily low ✓")
            elif daily_range_pos > 0.8:  # Near daily high
                quality_adjust -= 5
                score.reasons.append("Near daily high ⚠️")
            
            # --- Weekly Range Position ---
            week_range_pos = getattr(ind, 'week_range_position', 0.5)
            if week_range_pos < 0.3:  # Near weekly low
                quality_adjust += 5
                score.reasons.append("Near weekly low ✓")
            elif week_range_pos > 0.9:  # At weekly high
                quality_adjust -= 5
                score.reasons.append("At weekly high ⚠️")
            
            # --- Hourly RSI ---
            rsi_1h = getattr(ind, 'rsi_1h', 50)
            if rsi_1h < 30:  # Hourly oversold
                quality_adjust += 5
                score.reasons.append(f"1H RSI oversold {rsi_1h:.0f} ✓")
            elif rsi_1h > 70:  # Hourly overbought
                quality_adjust -= 5
                score.reasons.append(f"1H RSI overbought {rsi_1h:.0f} ⚠️")
            
            # === 30-DAY SMART TRADING ===
            # Don't buy at 30-day resistance, prefer buying at 30-day support
            
            # Calculate 30-day range position from candles_1d if available
            # (This uses the weekly data as proxy for now - actual 30d data is in candles_1d)
            trend_30d = getattr(ind, 'trend_7d', 0) * 4  # Approximate 30d from 7d
            
            # If we're at the top of the monthly range and trend is weak, BLOCK
            if week_range_pos > 0.95 and trend_30d < 5:
                quality_adjust -= 15
                score.reasons.append("At 30d high - DON'T BUY ⛔")
            
            # If we're near 30-day low with positive momentum, STRONG BUY signal
            elif week_range_pos < 0.15 and trend_7d > -3:
                quality_adjust += 10
                score.reasons.append("Near 30d low + holding ✓✓")
            
            # Strong 30-day uptrend = trade with confidence
            if trend_30d > 20:
                quality_adjust += 8
                score.reasons.append(f"Strong 30d uptrend +{trend_30d:.0f}% ✓✓")
            elif trend_30d < -20:
                quality_adjust -= 8
                score.reasons.append(f"Strong 30d downtrend {trend_30d:.0f}% ⚠️⚠️")
            
            # Apply quality adjustment
            score.total_score += quality_adjust
            
            # Log quality score breakdown
            from core.logger import log_quality_score, utc_iso_str
            log_quality_score({
                "ts": utc_iso_str(),
                "symbol": signal.symbol,
                "base_score": score.total_score - quality_adjust,
                "quality_adjust": quality_adjust,
                "final_score": score.total_score,
                "rsi": ind.rsi_14,
                "macd_histogram": ind.macd_histogram,
                "bb_position": ind.bb_position,
                "chop_score": ind.chop_score,
                "buy_pressure": ind.buy_pressure,
                "ema9": ind.ema9,
                "ema21": ind.ema21,
                "price": ind.price,
                "should_enter": score.total_score >= settings.entry_score_min,
            })
            
            # Store indicators for dashboard
            score.rsi = ind.rsi_14
            score.macd_signal = ind.macd_histogram
            score.bb_position = ind.bb_position
        
        # Regime filter: BTC trend (use market regime)
        # Store regime for smart threshold adjustment in should_enter
        score.btc_regime = self._market_regime
        
        if self._market_regime == "normal":
            score.btc_trend_ok = True
        elif self._market_regime == "caution":
            score.btc_trend_ok = False
            # But still allow if alt is strong (handled in should_enter)
            score.reasons.append(f"🟡 BTC caution {self._btc_trend_1h:+.1f}% (need score 70+)")
        else:  # risk_off
            score.btc_trend_ok = False
            # Allow divergence plays (alt up while BTC down)
            if trend_15m >= 2.0:
                score.reasons.append(f"🔴 BTC dump but ALT diverging +{trend_15m:.1f}% 🚀")
            else:
                score.reasons.append(f"🔴 BTC dumping {self._btc_trend_1h:+.1f}% (need score 75+ & strong trend)")
        
        # Symbol trend filter
        if trend_15m >= 0:
            score.symbol_trend_ok = True
        else:
            score.symbol_trend_ok = False
        
        # Overbought filter
        if trend_15m > 5.0:
            score.not_overbought = False
            score.reasons.append("Too extended, may reverse")
        
        # ================================================================
        # GATE 6: ML GATE/BOOST (LAST - uses cached ML only, no recompute)
        # ================================================================
        # ML never "creates" trades - base strategy is the edge
        # ML can only boost or block after all other gates pass
        
        ml = self.get_live_ml(signal.symbol)  # Returns None if stale
        
        if ml and not ml.is_stale():
            score.ml_score = ml.raw_score
            score.ml_confidence = ml.confidence
            
            # ML gating rules:
            # - Low confidence: neutral (no boost, no block)
            # - Bearish + high confidence + low base score: block
            # - Bullish + confidence: bounded boost
            
            if ml.confidence >= settings.ml_min_confidence:
                if ml.bearish and score.total_score < settings.base_score_strict_cutoff:
                    # Block: bearish ML on weak setup
                    score.reasons.append(f"📉 ML bearish blocks weak setup ({ml.raw_score:+.2f})")
                    score.ml_boost = -10  # Strong penalty
                    score.total_score += score.ml_boost
                elif ml.bullish:
                    # Boost: capped positive adjustment
                    raw_boost = ml.raw_score * settings.ml_boost_scale
                    score.ml_boost = max(settings.ml_boost_min, min(settings.ml_boost_max, raw_boost))
                    score.total_score += score.ml_boost
                    score.reasons.append(f"📈 ML boost +{score.ml_boost:.1f} ({ml.raw_score:+.2f}, {ml.confidence:.0%})")
                elif ml.bearish:
                    # Moderate penalty for bearish on decent setup
                    score.ml_boost = settings.ml_boost_min
                    score.total_score += score.ml_boost
                    score.reasons.append(f"📉 ML bearish ({ml.raw_score:+.2f})")
            else:
                # Low confidence: log but don't affect score
                score.reasons.append(f"🔸 ML low conf ({ml.confidence:.0%})")
        else:
            # Stale or missing ML: slight cautionary penalty
            score.total_score -= 3
            score.reasons.append("ML stale/none - cautious (-3)")
        
        return score
    
    def check_position_limits(
        self,
        symbol: str,
        size_usd: float,
        current_positions: dict,
    ) -> tuple[bool, str]:
        """
        Check if new position passes all limits.
        
        Returns (allowed, reason).
        """
        from core.config import settings
        from core.profiles import is_test_profile
        
        # Test profile: bypass all limits
        if is_test_profile(settings.profile):
            return True, "OK (test profile)"
        
        # 1. Check total position count
        if len(current_positions) >= self.limits.max_total_positions:
            return False, f"Max {self.limits.max_total_positions} positions reached"
        
        # 2. Check per-symbol exposure
        current_exposure = sum(
            p.size_usd for p in current_positions.values()
            if p.symbol == symbol
        )
        if current_exposure >= self.limits.max_per_symbol:
            return False, f"Max ${self.limits.max_per_symbol} per symbol"
        
        # 3. Check sector limit
        sector = self.get_sector(symbol)
        sector_count = self._sector_positions.get(sector, 0)
        if sector_count >= self.limits.max_per_sector:
            return False, f"Max {self.limits.max_per_sector} positions in {sector}"
        
        # 4. Check play quality - don't add more if too many weak plays
        weak_plays = sum(
            1 for p in current_positions.values()
            if getattr(p, 'play_quality', 'neutral') == 'weak'
        )
        if weak_plays >= self.limits.max_weak_plays:
            return False, f"Too many weak plays ({weak_plays}), fix existing before adding"
        
        # 5. Check global cooldown
        if self._last_trade_time:
            elapsed = (datetime.now(timezone.utc) - self._last_trade_time).total_seconds()
            if elapsed < self.limits.global_cooldown_sec:
                remaining = int(self.limits.global_cooldown_sec - elapsed)
                return False, f"Global cooldown: {remaining}s remaining"
        
        return True, "OK"
    
    def record_trade(self):
        """Record that a trade was made (for cooldown)."""
        self._last_trade_time = datetime.now(timezone.utc)
    
    def get_position_size(
        self,
        base_size: float,
        score: EntryScore,
    ) -> float:
        """
        Adjust position size based on confidence.
        
        AGGRESSIVE on high conviction (1.5x)
        SMART by scaling down on weak setups
        PATIENT by regime-aware reduction
        """
        # More aggressive tiers for better setups
        if score.total_score >= 85:
            multiplier = 1.5  # A+ setup - max size
        elif score.total_score >= 80:
            multiplier = 1.3  # High conviction
        elif score.total_score >= 70:
            multiplier = 1.1  # Good setup
        elif score.total_score >= 60:
            multiplier = 0.9  # Decent setup
        elif score.total_score >= 50:
            multiplier = 0.7  # Borderline
        else:
            multiplier = 0.5  # Minimum (shouldn't reach here)
        
        # Regime-aware sizing: downshift in caution/risk_off
        if self._market_regime == "caution":
            multiplier *= 0.85
        elif self._market_regime == "risk_off":
            multiplier *= 0.65
        elif self._market_regime == "bullish":
            multiplier *= 1.1  # Slight boost in bull regime
        
        return base_size * multiplier
    
    def log_trade_entry(self, symbol: str, score: EntryScore, burst_metrics: dict):
        """Log entry features for ML training. Call when opening position."""
        from core.logger import log_trade, utc_iso_str
        
        log_trade({
            "ts": utc_iso_str(),
            "type": "ml_entry",
            "symbol": symbol,
            "score_total": score.total_score,
            "score_trend": score.trend_score,
            "score_volume": score.volume_score,
            "score_vwap": score.vwap_score,
            "score_range": score.range_score,
            "score_tier": score.tier_score,
            "vol_spike": burst_metrics.get("vol_spike", 0),
            "range_spike": burst_metrics.get("range_spike", 0),
            "trend_15m": burst_metrics.get("trend_15m", 0),
            "vwap_distance": burst_metrics.get("vwap_distance", 0),
            "btc_trend": self._btc_trend_1h,
            "market_regime": self._market_regime,
            "sector": self.get_sector(symbol),
        })
    
    def log_trade_exit(self, symbol: str, pnl: float, pnl_pct: float, 
                       exit_reason: str, hold_minutes: float, strategy_id: str = "unknown"):
        """Log exit outcome for ML training. Call when closing position."""
        from core.logger import log_trade, utc_iso_str
        
        # Determine if this was a win
        is_win = pnl > 0
        hit_tp = exit_reason in ["tp1", "tp2"]
        hit_stop = exit_reason == "stop"
        
        log_trade({
            "ts": utc_iso_str(),
            "type": "ml_exit",
            "symbol": symbol,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "hold_minutes": hold_minutes,
            "is_win": is_win,
            "hit_tp": hit_tp,
            "hit_stop": hit_stop,
            "btc_trend_at_exit": self._btc_trend_1h,
            "strategy_id": strategy_id,
        })
        
        # Update daily PnL and strategy stats
        self.record_trade_result(pnl, strategy_id, is_win)
    
    # ─────────────────────────────────────────────────────────────────────────
    # Daily Loss Limit
    # ─────────────────────────────────────────────────────────────────────────
    
    def _check_daily_reset(self):
        """Reset daily PnL if it's a new day."""
        today = date.today()
        if self._daily_reset_date != today:
            self._daily_realized_pnl = 0.0
            self._daily_reset_date = today
            self._trading_halted = False
    
    def record_trade_result(self, pnl: float, strategy_id: str = "unknown", is_win: bool = False):
        """Record trade result for daily tracking and strategy stats."""
        self._check_daily_reset()
        
        # Update daily realized PnL
        self._daily_realized_pnl += pnl
        
        # Check if we hit daily loss limit
        if self._daily_realized_pnl <= -self.limits.daily_loss_limit_usd:
            self._trading_halted = True
        
        # Update strategy stats
        if strategy_id not in self._strategy_stats:
            self._strategy_stats[strategy_id] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        
        stats = self._strategy_stats[strategy_id]
        if is_win:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        stats["total_pnl"] += pnl
    
    def is_trading_halted(self) -> tuple[bool, str]:
        """Check if trading is halted due to daily loss limit."""
        self._check_daily_reset()
        
        if self._trading_halted:
            return True, f"Daily loss limit hit: ${self._daily_realized_pnl:.2f}"
        
        return False, ""
    
    def get_daily_pnl(self) -> float:
        """Get today's realized PnL."""
        self._check_daily_reset()
        return self._daily_realized_pnl
    
    # ─────────────────────────────────────────────────────────────────────────
    # Strategy Performance
    # ─────────────────────────────────────────────────────────────────────────
    
    def get_strategy_stats(self) -> Dict[str, Dict]:
        """Get performance stats per strategy."""
        result = {}
        for strategy_id, stats in self._strategy_stats.items():
            wins = stats["wins"]
            losses = stats["losses"]
            total = wins + losses
            win_rate = (wins / total * 100) if total > 0 else 0
            result[strategy_id] = {
                "wins": wins,
                "losses": losses,
                "total": total,
                "win_rate": win_rate,
                "total_pnl": stats["total_pnl"],
            }
        return result
    
    def get_strategy_summary(self) -> str:
        """Get formatted strategy performance summary."""
        stats = self.get_strategy_stats()
        if not stats:
            return "No trades recorded yet"
        
        lines = ["Strategy Performance:"]
        for strat, data in sorted(stats.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
            pnl_str = f"+${data['total_pnl']:.2f}" if data['total_pnl'] >= 0 else f"-${abs(data['total_pnl']):.2f}"
            lines.append(f"  {strat}: {data['wins']}W/{data['losses']}L ({data['win_rate']:.0f}%) {pnl_str}")
        return "\n".join(lines)


# Singleton instance
intelligence = IntelligenceLayer()
