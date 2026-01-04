"""Mode-specific configuration management."""

import threading
from dataclasses import asdict, is_dataclass
from typing import Union

from core.config import settings
from core.mode_configs import BaseTradingConfig, LiveModeConfig, PaperModeConfig, TradingMode


class ConfigurationManager:
    """Manages mode-specific configuration loading."""
    
    @staticmethod
    def get_trading_mode() -> TradingMode:
        """Determine trading mode from settings."""
        return TradingMode(settings.trading_mode)
    
    @staticmethod
    def get_config_for_mode(mode: TradingMode) -> Union[PaperModeConfig, LiveModeConfig]:
        """Get appropriate configuration for trading mode."""
        
        # Common base from settings (used as fallbacks)
        base_config = {
            'max_trade_usd': settings.max_trade_usd,
            'daily_max_loss_usd': settings.daily_max_loss_usd,
            'tp1_pct': settings.tp1_pct,
            'tp2_pct': settings.tp2_pct,
            'tp1_partial_pct': getattr(settings, 'tp1_partial_pct', 0.5),
            'vol_spike_threshold': getattr(settings, 'vol_spike_threshold', 1.5),
            'range_spike_threshold': getattr(settings, 'range_spike_threshold', 1.2),
            'trail_be_trigger_pct': getattr(settings, 'trail_be_trigger_pct', 0.025),
            'trail_start_pct': getattr(settings, 'trail_start_pct', 0.035),
            'trail_lock_pct': getattr(settings, 'trail_lock_pct', 0.50),
            'min_position_usd': getattr(settings, 'position_min_usd', 1.0),
            'dust_threshold_usd': getattr(settings, 'position_dust_usd', 0.50),
            'maker_fee_pct': getattr(settings, 'maker_fee_pct', 0.006),
            'taker_fee_pct': getattr(settings, 'taker_fee_pct', 0.012),
        }
        
        if mode == TradingMode.PAPER:
            # Paper mode uses its own optimized defaults (overrides base_config)
            return PaperModeConfig(
                **base_config,
                paper_start_balance=settings.paper_start_balance_usd,
                enable_slippage=True,
                slippage_bps=2.0,
                # Paper-specific overrides for more aggressive testing
                max_positions=15,
                portfolio_max_exposure_pct=0.75,
                fixed_stop_pct=0.025,  # Tighter stops
                min_rr_ratio=1.5,      # Lower R:R for testing
                max_hold_minutes=90,   # Faster turnover
                fast_mode_enabled=True,
                ml_min_confidence=0.50,
                use_whitelist=False,
                time_stop_enabled=True,
            )
        
        # Live mode - aggressive settings for growth
        return LiveModeConfig(
            **base_config,
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret,
            use_limit_orders=settings.use_limit_orders,
            limit_buffer_pct=settings.limit_buffer_pct,
            # Use settings from config.py - no hardcoded overrides
            max_positions=settings.max_positions,  # Hard cap enforced by PositionRegistry
            portfolio_max_exposure_pct=settings.portfolio_max_exposure_pct,  # 80%
            fixed_stop_pct=settings.fixed_stop_pct,  # 10%
            min_rr_ratio=settings.min_rr_ratio,  # 1.2
            max_hold_minutes=settings.max_hold_minutes,  # 120
            fast_mode_enabled=settings.fast_mode_enabled,  # True
            ml_min_confidence=settings.ml_min_confidence,
            use_whitelist=settings.use_whitelist,
            time_stop_enabled=settings.time_stop_enabled,
        )
    
    @staticmethod
    def create_container() -> 'TradingContainer':
        """Create properly configured dependency container."""
        from core.trading_container import TradingContainer
        
        mode = ConfigurationManager.get_trading_mode()
        config = ConfigurationManager.get_config_for_mode(mode)
        
        return TradingContainer(mode, config)


def start_config_for_mode(mode: TradingMode) -> BaseTradingConfig:
    """Build a boot-time config snapshot for a specific mode."""
    return ConfigurationManager.get_config_for_mode(mode)


def start_config_live() -> LiveModeConfig:
    """Build a boot-time config snapshot for live mode."""
    return ConfigurationManager.get_config_for_mode(TradingMode.LIVE)


def start_config_paper() -> PaperModeConfig:
    """Build a boot-time config snapshot for paper mode."""
    return ConfigurationManager.get_config_for_mode(TradingMode.PAPER)


class RuntimeConfigStore:
    """Tracks immutable start config and mutable running config for a mode."""

    def __init__(self, mode: TradingMode):
        self._mode = mode
        self._lock = threading.Lock()
        self._start_config = ConfigurationManager.get_config_for_mode(mode)
        self._running_config = self._start_config

    @property
    def start_config(self) -> BaseTradingConfig:
        return self._start_config

    @property
    def running_config(self) -> BaseTradingConfig:
        return self._running_config

    def refresh(self) -> BaseTradingConfig:
        """Rebuild running config from current settings."""
        with self._lock:
            self._running_config = ConfigurationManager.get_config_for_mode(self._mode)
            return self._running_config


_CONFIG_SECRET_KEYS = {"api_key", "api_secret"}


def sanitize_config_snapshot(config: Union[BaseTradingConfig, dict, None]) -> dict:
    """Return a config snapshot with secrets redacted for UI/export."""
    if config is None:
        return {}
    snapshot = asdict(config) if is_dataclass(config) else dict(config)
    for key in _CONFIG_SECRET_KEYS:
        if key in snapshot and snapshot[key]:
            snapshot[key] = "REDACTED"
    return snapshot
