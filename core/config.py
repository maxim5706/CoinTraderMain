"""Bot configuration."""

import logging
import secrets
import time
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
load_dotenv()

try:
    import jwt
    from cryptography.hazmat.primitives import serialization
    HAS_JWT = True
except ImportError:
    HAS_JWT = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    
    # API
    coinbase_api_key: str = Field(default="", alias="COINBASE_API_KEY")
    coinbase_api_secret: str = Field(default="", alias="COINBASE_API_SECRET")
    
    # Mode
    trading_mode: Literal["paper", "live"] = Field(default="paper", alias="TRADING_MODE")
    profile: str = Field(default="prod", alias="PROFILE")
    paper_start_balance_usd: float = Field(default=1000.0, alias="PAPER_START_BALANCE")
    
    # Risk - Exposure-based (no hard position count limit)
    portfolio_max_exposure_pct: float = 0.70  # 70% max in positions
    position_base_pct: float = 0.03           # 3% base size per trade
    position_min_pct: float = 0.02            # 2% minimum
    position_max_pct: float = 0.08            # 8% maximum (was 6%)
    max_trade_usd: float = Field(default=15.0, alias="MAX_TRADE_USD")  # Default $15
    daily_max_loss_usd: float = Field(default=25.0, alias="DAILY_MAX_LOSS_USD")
    max_positions: int = 10                   # Focus on fewer, bigger positions
    
    # Tiered sizing - bet big on best setups
    whale_trade_usd: float = 30.0             # A+ setups
    whale_score_min: int = 90                 # Min score for whale
    whale_confluence_min: int = 2             # Min confluence for whale
    strong_trade_usd: float = 15.0            # A setups
    strong_score_min: int = 80                # Min score for strong (OR confluence)
    normal_trade_usd: float = 8.0             # B setups
    whale_max_positions: int = 2              # Max whale bets at once
    strong_max_positions: int = 4             # Max strong bets
    
    # Fees (Intro tier)
    taker_fee_pct: float = 0.012
    maker_fee_pct: float = 0.006
    use_limit_orders: bool = True
    limit_buffer_pct: float = 0.003
    
    # Watchlist
    watch_coins: str = Field(
        default="BTC-USD,ETH-USD,SOL-USD,AVAX-USD,LINK-USD,DOGE-USD",
        alias="WATCH_COINS"
    )
    
    # Entry
    vol_spike_threshold: float = 1.5
    range_spike_threshold: float = 1.2
    impulse_min_pct: float = 0.5
    flag_retrace_min: float = 0.15
    flag_retrace_max: float = 0.55
    flag_vol_decay: float = 0.8
    breakout_buffer_pct: float = 0.1
    breakout_vol_mult: float = 1.3
    
    # Stops/TPs (after fees: 1.8% round-trip with limit orders)
    # R:R = tp1_pct / fixed_stop_pct must be >= min_rr_ratio
    fixed_stop_pct: float = 0.035  # 3.5% stop
    tp1_pct: float = 0.07          # 7% TP1 → R:R = 2.0
    tp2_pct: float = 0.10          # 10% TP2
    tp1_partial_pct: float = 0.5
    stop_atr_mult: float = 1.2  # Tighter ATR stops (was 1.5)
    tp2_impulse_mult: float = 0.5
    max_hold_minutes: int = 120  # Reduce from 180 to 120 min
    time_stop_enabled: bool = True  # Enable time stops
    min_rr_ratio: float = 1.5  # R:R minimum (7/3.5 = 2.0 passes)
    
    # Trailing
    trail_be_trigger_pct: float = 0.025
    trail_start_pct: float = 0.035
    trail_lock_pct: float = 0.50
    
    # Patterns
    triple_top_tolerance_pct: float = 0.5
    hs_shoulder_tolerance_pct: float = 1.0
    
    # Whitelist
    symbol_whitelist: str = "BCH-USD,ORCA-USD,TIA-USD,TNSR-USD,QNT-USD,LINK-USD,BAT-USD,UNI-USD,ONDO-USD,COMP-USD,SOL-USD,ETH-USD,BTC-USD"
    use_whitelist: bool = False
    
    # Fast mode
    fast_mode_enabled: bool = True
    fast_confidence_min: float = 0.65
    fast_spread_max_bps: float = 18.0
    fast_stop_pct: float = 2.5
    fast_tp1_pct: float = 4.0
    fast_tp2_pct: float = 7.0
    fast_time_stop_min: int = 60
    
    # ML
    ml_min_confidence: float = 0.55
    ml_boost_scale: float = 10.0
    ml_boost_min: float = -5.0
    ml_boost_max: float = 10.0
    base_score_strict_cutoff: float = 60
    entry_score_min: float = 70
    
    # Thesis invalidation - tighter to prevent big losses
    thesis_trend_flip_5m: float = -0.3  # Tighter from -0.5
    thesis_trend_flip_15m: float = -0.2  # Tighter from -0.3
    thesis_vwap_distance: float = -0.8  # Tighter from -1.0
    
    # Liquidity
    spread_max_bps: float = 25.0
    min_24h_volume_usd: float = 100000
    
    # Order management
    order_cooldown_seconds: int = 1800  # 30 min cooldown per symbol
    order_cooldown_min_seconds: int = 300  # 5 min hard cooldown after any order
    
    # Circuit breaker
    circuit_breaker_max_failures: int = 5  # Consecutive failures to trip breaker
    circuit_breaker_reset_seconds: int = 300  # Time to wait before retry (5 min)
    
    # Stop order health check
    stop_health_check_interval: int = 300  # Seconds between re-arming checks (5 min)
    
    # Position thresholds
    position_min_usd: float = 1.0  # Minimum USD value to consider a position
    position_dust_usd: float = 0.0001  # Holdings below this are dust
    position_qty_drift_tolerance: float = 0.00001  # Qty difference to flag as drift
    position_verify_tolerance: float = 0.95  # 5% tolerance for position verification
    
    # Ignored symbols (delisted, problematic, or dust to skip)
    ignored_symbols: str = "SNX-USD,CLV-USD,CGLD-USD,MANA-USD,NU-USD,BOND-USD"
    
    @property
    def ignored_symbol_set(self) -> set[str]:
        return {s.strip() for s in self.ignored_symbols.split(",") if s.strip()}
    
    @property
    def coins(self) -> list[str]:
        return [c.strip() for c in self.watch_coins.split(",") if c.strip()]
    
    @property
    def is_paper(self) -> bool:
        return self.trading_mode == "paper"
    
    @property
    def is_configured(self) -> bool:
        return bool(self.coinbase_api_key and self.coinbase_api_secret)
    
    def get_ws_jwt(self) -> str:
        if not HAS_JWT or not self.is_configured:
            return ""
        
        api_key = self.coinbase_api_key
        api_secret = self.coinbase_api_secret.replace("\\n", "\n")
        
        try:
            private_key = serialization.load_pem_private_key(
                api_secret.encode('utf-8'), password=None
            )
            now = int(time.time())
            payload = {
                "sub": api_key,
                "iss": "coinbase-cloud",
                "nbf": now,
                "exp": now + 120,
                "aud": ["public_websocket_api"],
            }
            headers = {"kid": api_key, "nonce": secrets.token_hex(16)}
            return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
        except Exception as e:
            logger.warning("[CONFIG] JWT failed: %s", e, exc_info=True)
            return ""


settings = Settings()

try:
    from core.profiles import apply_profile, default_profile_for_mode
    chosen_profile = settings.profile or default_profile_for_mode(settings.trading_mode)
    apply_profile(chosen_profile, settings)
except Exception as e:
    logger.debug("Profile application skipped: %s", e, exc_info=True)
