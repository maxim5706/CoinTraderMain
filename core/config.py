"""Bot configuration."""

import logging
import secrets
import time
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field, field_validator, model_validator
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
    
    # Risk - Scales with portfolio growth
    # 50 positions × ~1.6% avg = 80% exposure target
    portfolio_max_exposure_pct: float = 0.85  # 85% max in positions (allows 50 positions)
    position_base_pct: float = 0.015          # 1.5% base size per trade
    position_min_pct: float = 0.01            # 1% minimum (~$6 at $600, ~$10 at $1000)
    position_max_pct: float = 0.025           # 2.5% maximum (~$15 at $600, ~$25 at $1000)
    risk_per_trade_pct: float = 0.01          # 1% risk per trade
    max_trade_usd: float = Field(default=25.0, alias="MAX_TRADE_USD")  # $25 max (scales up)
    daily_max_loss_pct: float = 0.02          # -2% daily loss limit (~$12)
    daily_max_loss_usd: float = Field(default=12.0, alias="DAILY_MAX_LOSS_USD")  # Fallback
    max_positions: int = 50                   # No hard cap - exposure % is the real limit
    
    # Tiered sizing - Ratio-based (scales with portfolio)
    # Percentages of portfolio value, with USD fallbacks for safety
    whale_trade_pct: float = 0.020            # 2% of portfolio for A+ setups
    whale_trade_usd: float = 12.0             # Fallback USD (used if pct calc fails)
    whale_score_min: int = 85                 # Min score for whale
    whale_confluence_min: int = 2             # Min confluence for whale
    strong_trade_pct: float = 0.016           # 1.6% of portfolio for A setups
    strong_trade_usd: float = 10.0            # Fallback USD
    strong_score_min: int = 70                # Min score for strong
    normal_trade_pct: float = 0.013           # 1.3% of portfolio for B setups
    normal_trade_usd: float = 8.0             # Fallback USD
    scout_trade_pct: float = 0.010            # 1% of portfolio for learning
    scout_trade_usd: float = 6.0              # Fallback USD
    scout_score_min: int = 55                 # Allow some scout trades
    whale_max_positions: int = 1              # Max 1 whale at a time
    strong_max_positions: int = 2             # Max 2 strong
    scout_max_positions: int = 1              # Max 1 scout
    min_trade_usd: float = 5.0                # Absolute minimum to avoid dust
    
    # Stacking (add to winners)
    stacking_enabled: bool = True             # Allow adding to winning positions
    stacking_min_profit_pct: float = 2.0      # Min +2% before stacking allowed
    stacking_max_adds: int = 1                # Max 1 add per position (2x total)
    stacking_green_candles: int = 3           # Require 3 green candles (positive incline)
    
    # Fees (Intro tier)
    taker_fee_pct: float = 0.012
    maker_fee_pct: float = 0.006
    use_limit_orders: bool = True  # Limit orders = 0.6% vs 1.2% taker
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
    
    # Stops/TPs - Structure-based, R-multiple driven
    # Target: 40-55% win rate with 1.5-2.5 R:R
    fixed_stop_pct: float = 0.03   # 3% default stop (structure overrides)
    tp1_pct: float = 0.045         # 4.5% TP1 = 1.5R (take partial)
    tp2_pct: float = 0.075         # 7.5% TP2 = 2.5R (let winners run)
    tp1_partial_pct: float = 0.5   # Take 50% at TP1
    stop_atr_mult: float = 1.5     # ATR-based stops (structure)
    tp2_impulse_mult: float = 0.5
    max_hold_minutes: int = 240    # 4 hours max hold (let trades develop)
    time_stop_enabled: bool = True
    min_rr_ratio: float = 1.5      # Minimum 1.5 R:R required
    
    # Trailing - R-multiple based
    # Activate trailing at +0.75R, move to BE at +1R
    trail_be_trigger_r: float = 1.0   # Move to BE at +1R profit
    trail_start_r: float = 0.75       # Start trailing at +0.75R
    trail_be_trigger_pct: float = 0.03  # 3% = ~1R (fallback)
    trail_start_pct: float = 0.0225    # 2.25% = ~0.75R (fallback)
    trail_lock_pct: float = 0.50       # Lock 50% of gains (let winners run)
    
    # Patterns
    triple_top_tolerance_pct: float = 0.5
    hs_shoulder_tolerance_pct: float = 1.0
    
    # Whitelist
    symbol_whitelist: str = "BCH-USD,ORCA-USD,TIA-USD,TNSR-USD,QNT-USD,LINK-USD,BAT-USD,UNI-USD,ONDO-USD,COMP-USD,SOL-USD,ETH-USD,BTC-USD"
    use_whitelist: bool = False
    
    # Fast mode
    fast_mode_enabled: bool = False
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
    base_score_strict_cutoff: float = 40
    entry_score_min: float = 60.0  # Quality entries only
    
    # Thesis invalidation - tighter to prevent big losses
    thesis_trend_flip_5m: float = -0.3  # Tighter from -0.5
    thesis_trend_flip_15m: float = -0.2  # Tighter from -0.3
    thesis_vwap_distance: float = -0.8  # Tighter from -1.0
    
    # Liquidity (RELAXED for full coverage)
    spread_max_bps: float = 50.0  # Reasonable spread tolerance
    min_24h_volume_usd: float = 100000
    
    # Order management
    order_cooldown_seconds: int = 600  # 10 min cooldown - faster re-entry
    order_cooldown_min_seconds: int = 300  # 5 min hard cooldown after any order
    
    # Circuit breaker
    circuit_breaker_max_failures: int = 5  # Consecutive failures to trip breaker
    circuit_breaker_reset_seconds: int = 300  # Time to wait before retry (5 min)
    
    # Stop order health check
    stop_health_check_interval: int = 60  # Seconds between re-arming checks (1 min - faster recovery)
    
    # Position thresholds
    position_min_usd: float = 1.0  # Minimum USD value to consider a position
    position_dust_usd: float = 0.0001  # Holdings below this are dust
    position_qty_drift_tolerance: float = 0.00001  # Qty difference to flag as drift
    position_verify_tolerance: float = 0.95  # 5% tolerance for position verification
    
    # Ignored symbols (delisted, problematic, or dust to skip)
    ignored_symbols: str = "SNX-USD,CLV-USD,CGLD-USD,MANA-USD,NU-USD,BOND-USD"
    
    @field_validator('portfolio_max_exposure_pct', 'position_base_pct', 'position_min_pct', 
                      'position_max_pct', 'taker_fee_pct', 'maker_fee_pct', 'tp1_partial_pct',
                      'trail_lock_pct', mode='after')
    @classmethod
    def validate_percentage_0_1(cls, v: float) -> float:
        """Validate percentages that should be between 0 and 1."""
        if not 0 <= v <= 1:
            raise ValueError(f'Percentage must be between 0 and 1, got {v}')
        return v
    
    @field_validator('fixed_stop_pct', 'tp1_pct', 'tp2_pct', 'trail_be_trigger_pct', 
                      'trail_start_pct', 'limit_buffer_pct', mode='after')
    @classmethod
    def validate_percentage_positive(cls, v: float) -> float:
        """Validate percentages that should be positive."""
        if v <= 0:
            raise ValueError(f'Percentage must be positive, got {v}')
        return v
    
    @field_validator('max_trade_usd', 'daily_max_loss_usd', 'min_24h_volume_usd', mode='after')
    @classmethod
    def validate_usd_positive(cls, v: float) -> float:
        """Validate USD amounts that should be positive."""
        if v <= 0:
            raise ValueError(f'USD amount must be positive, got {v}')
        return v
    
    @field_validator('max_positions', 'max_hold_minutes', 'order_cooldown_seconds', mode='after')
    @classmethod
    def validate_int_positive(cls, v: int) -> int:
        """Validate integers that should be positive."""
        if v <= 0:
            raise ValueError(f'Value must be positive, got {v}')
        return v
    
    @field_validator('min_rr_ratio', mode='after')
    @classmethod  
    def validate_rr_ratio(cls, v: float) -> float:
        """Validate R:R ratio is reasonable."""
        if v < 1.0:
            raise ValueError(f'R:R ratio must be >= 1.0, got {v}')
        if v > 10.0:
            logger.warning('R:R ratio %.1f is very high, may miss trades', v)
        return v
    
    @model_validator(mode='after')
    def validate_rr_achievable(self) -> 'Settings':
        """Validate that TP1 can achieve the minimum R:R ratio."""
        if self.fixed_stop_pct > 0:
            actual_rr = self.tp1_pct / self.fixed_stop_pct
            if actual_rr < self.min_rr_ratio:
                logger.warning(
                    'TP1 (%.1f%%) / Stop (%.1f%%) = %.2f R:R, below min_rr_ratio %.2f',
                    self.tp1_pct * 100, self.fixed_stop_pct * 100, actual_rr, self.min_rr_ratio
                )
        return self
    
    @model_validator(mode='after')
    def validate_position_sizing(self) -> 'Settings':
        """Validate position sizing makes sense."""
        if self.position_min_pct > self.position_max_pct:
            raise ValueError(
                f'position_min_pct ({self.position_min_pct}) > position_max_pct ({self.position_max_pct})'
            )
        if self.position_base_pct < self.position_min_pct:
            raise ValueError(
                f'position_base_pct ({self.position_base_pct}) < position_min_pct ({self.position_min_pct})'
            )
        if self.position_base_pct > self.position_max_pct:
            raise ValueError(
                f'position_base_pct ({self.position_base_pct}) > position_max_pct ({self.position_max_pct})'
            )
        return self

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
        """Check if API keys are present (not necessarily valid)."""
        return bool(self.coinbase_api_key and self.coinbase_api_secret)
    
    def validate_for_live_mode(self) -> tuple[bool, str]:
        """Validate settings are safe for live trading. Returns (ok, message)."""
        issues = []
        
        if not self.is_configured:
            issues.append('API keys missing - set COINBASE_API_KEY and COINBASE_API_SECRET in .env')
        
        if self.daily_max_loss_usd > 100:
            issues.append(f'daily_max_loss_usd=${self.daily_max_loss_usd} is high - are you sure?')
        
        if self.max_trade_usd > 50:
            issues.append(f'max_trade_usd=${self.max_trade_usd} is high for live trading')
        
        if self.portfolio_max_exposure_pct > 0.80:
            issues.append(f'portfolio_max_exposure_pct={self.portfolio_max_exposure_pct} leaves little cash buffer')
        
        if self.fixed_stop_pct > 0.10:
            issues.append(f'fixed_stop_pct={self.fixed_stop_pct*100}% is very wide')
        
        if issues:
            return False, '; '.join(issues)
        return True, 'OK'
    
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
