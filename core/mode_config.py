"""Mode-specific configuration management."""

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
        
        # Live mode uses proven, conservative settings
        return LiveModeConfig(
            **base_config,
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret,
            use_limit_orders=settings.use_limit_orders,
            limit_buffer_pct=settings.limit_buffer_pct,
            # Live-specific overrides for conservative real trading
            max_positions=8,
            portfolio_max_exposure_pct=0.40,
            fixed_stop_pct=settings.fixed_stop_pct,  # Use proven setting (3.5%)
            min_rr_ratio=settings.min_rr_ratio,      # Use proven R:R (1.8)
            max_hold_minutes=getattr(settings, 'max_hold_minutes', 180),
            fast_mode_enabled=getattr(settings, 'fast_mode_enabled', False),
            ml_min_confidence=getattr(settings, 'ml_min_confidence', 0.65),
            use_whitelist=getattr(settings, 'use_whitelist', False),
            time_stop_enabled=getattr(settings, 'time_stop_enabled', True),
        )
    
    @staticmethod
    def create_container() -> 'TradingContainer':
        """Create properly configured dependency container."""
        from core.trading_container import TradingContainer
        
        mode = ConfigurationManager.get_trading_mode()
        config = ConfigurationManager.get_config_for_mode(mode)
        
        return TradingContainer(mode, config)
