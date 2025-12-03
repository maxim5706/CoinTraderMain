"""Mode-specific configuration structures."""

from dataclasses import dataclass
from enum import Enum


class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass
class BaseTradingConfig:
    """Base trading configuration with sensible defaults."""
    
    # Core risk parameters 
    max_trade_usd: float = 5.0
    daily_max_loss_usd: float = 25.0
    max_positions: int = 10
    fixed_stop_pct: float = 0.035
    tp1_pct: float = 0.04
    tp2_pct: float = 0.07
    min_rr_ratio: float = 1.8
    
    # Advanced parameters
    portfolio_max_exposure_pct: float = 0.50
    tp1_partial_pct: float = 0.5
    max_hold_minutes: int = 120
    time_stop_enabled: bool = True
    
    # Trailing stops
    trail_be_trigger_pct: float = 0.025
    trail_start_pct: float = 0.035
    trail_lock_pct: float = 0.50
    
    # Entry thresholds
    vol_spike_threshold: float = 1.5
    range_spike_threshold: float = 1.2
    ml_min_confidence: float = 0.55
    
    # Feature flags
    fast_mode_enabled: bool = False
    use_whitelist: bool = False
    
    # Position Management
    min_position_usd: float = 1.0       # Minimum position size
    dust_threshold_usd: float = 0.50    # Below this = dust
    max_positions_per_strategy: int = 3  # Per strategy limit
    min_hold_seconds: int = 30          # Minimum hold time
    
    # Fee Structure (configurable per mode)
    maker_fee_pct: float = 0.006        # Limit order fee
    taker_fee_pct: float = 0.012        # Market order fee
    
    # Strategy: Burst Flag Scoring Thresholds
    bf_base_score: int = 10
    bf_impulse_strong_pct: float = 5.0      # >= 5% move = 15 pts
    bf_impulse_medium_pct: float = 3.0      # >= 3% move = 10 pts  
    bf_impulse_weak_pct: float = 2.0        # >= 2% move = 5 pts
    bf_green_candles_strong: int = 5        # >= 5 candles = 10 pts
    bf_green_candles_medium: int = 3        # >= 3 candles = 5 pts
    bf_volume_threshold: float = 2.0        # > 2x volume = 5 pts
    bf_retrace_ideal_min: float = 0.3       # 30% retrace minimum for ideal
    bf_retrace_ideal_max: float = 0.5       # 50% retrace maximum for ideal  
    bf_retrace_good_min: float = 0.2        # 20% retrace minimum for good
    bf_retrace_good_max: float = 0.6        # 60% retrace maximum for good
    bf_retrace_acceptable: float = 0.7      # < 70% retrace still acceptable
    bf_volume_decay_strong: float = 0.5     # Flag vol < 50% impulse = 10 pts
    bf_volume_decay_medium: float = 0.7     # Flag vol < 70% impulse = 5 pts
    bf_trend_strong: float = 0.5            # > 50% trend = 15 pts
    bf_trend_medium: float = 0.2            # > 20% trend = 10 pts
    bf_vol_ratio_strong: float = 3.0        # >= 3x volume = strong confirmation


@dataclass  
class PaperModeConfig(BaseTradingConfig):
    """Paper trading - More aggressive for testing."""
    paper_start_balance: float = 1000.0
    enable_slippage: bool = True
    slippage_bps: float = 2.0
    
    # Paper can afford more risk for learning
    portfolio_max_exposure_pct: float = 0.75  # Higher than live
    max_positions: int = 15  # More positions for testing
    
    # Tighter stops to test strategy resilience  
    fixed_stop_pct: float = 0.025  # 2.5% vs 3.5% in live
    min_rr_ratio: float = 1.5  # Lower requirement for testing
    
    # Faster execution for rapid testing
    max_hold_minutes: int = 90  # Shorter holds
    fast_mode_enabled: bool = True  # Enable experimental features
    
    # Lower confidence bar for testing edge cases
    ml_min_confidence: float = 0.50
    
    # More lenient strategy thresholds for testing
    bf_impulse_strong_pct: float = 4.0      # Lower bar (4% vs 5%)
    bf_impulse_medium_pct: float = 2.5      # Lower bar (2.5% vs 3%)
    bf_retrace_good_min: float = 0.15       # Accept wider range (15-70%)
    bf_retrace_acceptable: float = 0.75     # Accept deeper retracements
    bf_trend_medium: float = 0.1            # Accept weaker trends (10% vs 20%)
    
    # Paper mode: More lenient position management
    dust_threshold_usd: float = 0.10        # Lower dust threshold for testing small positions
    max_positions_per_strategy: int = 5     # More positions per strategy for testing
    min_hold_seconds: int = 10              # Faster testing cycles


@dataclass
class LiveModeConfig(BaseTradingConfig):
    """Live trading - Conservative and proven."""
    api_key: str = ""
    api_secret: str = ""
    use_limit_orders: bool = True
    limit_buffer_pct: float = 0.003
    
    # Conservative risk for real money
    portfolio_max_exposure_pct: float = 0.40  # Lower than paper
    max_positions: int = 8  # Fewer positions, more focus
    
    # Wider stops to account for slippage/volatility
    fixed_stop_pct: float = 0.035  # 3.5% - proven level  
    min_rr_ratio: float = 1.8  # Higher requirement for real trades
    
    # Longer holds for better fills
    max_hold_minutes: int = 180  # More patient
    fast_mode_enabled: bool = False  # Stick to proven strategies
    
    # Higher confidence for real money
    ml_min_confidence: float = 0.65
    
    # Stricter strategy thresholds for real money
    bf_impulse_strong_pct: float = 6.0      # Higher bar (6% vs 5%)
    bf_impulse_medium_pct: float = 4.0      # Higher bar (4% vs 3%)
    bf_retrace_good_min: float = 0.25       # Tighter range (25-55%)
    bf_retrace_good_max: float = 0.55       # Tighter range
    bf_retrace_acceptable: float = 0.65     # Less accepting of deep retracements
    bf_trend_strong: float = 0.6            # Require stronger trends (60% vs 50%)
    bf_vol_ratio_strong: float = 3.5        # Require stronger volume (3.5x vs 3x)
    
    # Live mode: Conservative position management
    dust_threshold_usd: float = 1.00        # Higher dust threshold (avoid micro positions)
    max_positions_per_strategy: int = 2     # Fewer positions per strategy for focus
    min_hold_seconds: int = 60              # Longer minimum hold for stability
