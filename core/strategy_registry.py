"""
Strategy Registry - Track and control enabled/disabled strategies.

Allows real-time enable/disable of individual trading strategies
and tracks per-strategy performance statistics.
"""

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any
import fcntl

from core.logging_utils import get_logger

logger = get_logger(__name__)


@dataclass
class StrategyStats:
    """Performance statistics for a single strategy."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    gross_profit: float = 0.0   # Sum of all winning trades
    gross_loss: float = 0.0     # Sum of all losing trades (stored as positive)
    avg_pnl: float = 0.0
    avg_hold_minutes: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    last_trade_at: Optional[str] = None
    
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100
    
    @property
    def profit_factor(self) -> float:
        """Profit factor = gross_profit / gross_loss. PF > 1 = profitable."""
        if self.gross_loss <= 0:
            return float('inf') if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d["win_rate"] = self.win_rate
        d["profit_factor"] = self.profit_factor
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> "StrategyStats":
        # Filter to only known fields
        known = {"total_trades", "wins", "losses", "total_pnl", "gross_profit", "gross_loss",
                 "avg_pnl", "avg_hold_minutes", "biggest_win", "biggest_loss", "last_trade_at"}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass 
class StrategyConfig:
    """Configuration for a single strategy."""
    name: str
    enabled: bool = True
    priority: int = 50  # Higher = checked first
    description: str = ""
    
    # Strategy-specific params (can be overridden per strategy)
    min_score: Optional[float] = None
    max_positions: Optional[int] = None
    
    # Runtime stats
    stats: StrategyStats = field(default_factory=StrategyStats)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "priority": self.priority,
            "description": self.description,
            "min_score": self.min_score,
            "max_positions": self.max_positions,
            "stats": self.stats.to_dict(),
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "StrategyConfig":
        stats_data = data.pop("stats", {})
        stats = StrategyStats.from_dict(stats_data) if stats_data else StrategyStats()
        return cls(stats=stats, **{k: v for k, v in data.items() 
                                   if k in {"name", "enabled", "priority", "description", "min_score", "max_positions"}})


# Default strategy definitions
DEFAULT_STRATEGIES = {
    "burst_flag": StrategyConfig(
        name="burst_flag",
        enabled=True,
        priority=90,
        description="High-momentum burst patterns with flag consolidation"
    ),
    "momentum_1h": StrategyConfig(
        name="momentum_1h",
        enabled=True,
        priority=80,
        description="1-hour momentum continuation plays"
    ),
    "vwap_reclaim": StrategyConfig(
        name="vwap_reclaim",
        enabled=True,
        priority=75,
        description="VWAP reclaim entries after dip"
    ),
    "range_breakout": StrategyConfig(
        name="range_breakout",
        enabled=True,
        priority=70,
        description="Range breakout with volume confirmation"
    ),
    "support_bounce": StrategyConfig(
        name="support_bounce",
        enabled=True,
        priority=65,
        description="Bounce plays off key support levels"
    ),
    "bb_expansion": StrategyConfig(
        name="bb_expansion",
        enabled=True,
        priority=60,
        description="Bollinger Band expansion breakouts"
    ),
    "daily_momentum": StrategyConfig(
        name="daily_momentum",
        enabled=True,
        priority=55,
        description="Multi-day momentum continuation"
    ),
    "rsi_momentum": StrategyConfig(
        name="rsi_momentum",
        enabled=True,
        priority=50,
        description="RSI-based momentum entries"
    ),
    "relative_strength": StrategyConfig(
        name="relative_strength",
        enabled=True,
        priority=45,
        description="Relative strength vs BTC/ETH"
    ),
}


class StrategyRegistry:
    """
    Central registry for strategy management.
    
    Features:
    - Enable/disable strategies at runtime
    - Track per-strategy performance
    - Persist state to disk
    - Priority-based strategy ordering
    """
    
    _instance: Optional["StrategyRegistry"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "StrategyRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._data_dir = Path(__file__).parent.parent / "data"
        self._registry_file = self._data_dir / "strategy_registry.json"
        self._file_lock = threading.Lock()
        self._strategies: Dict[str, StrategyConfig] = {}
        self._load_registry()
    
    def _load_registry(self):
        """Load registry from file or initialize with defaults."""
        try:
            if self._registry_file.exists():
                with open(self._registry_file, "r") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        data = json.load(f)
                        for name, cfg_data in data.get("strategies", {}).items():
                            self._strategies[name] = StrategyConfig.from_dict(cfg_data)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                
                # Add any new default strategies not in saved file
                for name, default_cfg in DEFAULT_STRATEGIES.items():
                    if name not in self._strategies:
                        self._strategies[name] = default_cfg
                
                logger.info("[STRAT_REG] Loaded %d strategies from registry", len(self._strategies))
                return
        except Exception as e:
            logger.warning("[STRAT_REG] Failed to load registry: %s", e)
        
        # Initialize with defaults
        self._strategies = {k: v for k, v in DEFAULT_STRATEGIES.items()}
        self._save_registry()
        logger.info("[STRAT_REG] Initialized with %d default strategies", len(self._strategies))
    
    def _save_registry(self):
        """Persist registry to file."""
        with self._file_lock:
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                temp_file = self._registry_file.with_suffix('.tmp')
                
                data = {
                    "strategies": {name: cfg.to_dict() for name, cfg in self._strategies.items()},
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                
                with open(temp_file, "w") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(data, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                
                os.replace(str(temp_file), str(self._registry_file))
                
            except Exception as e:
                logger.error("[STRAT_REG] Failed to save registry: %s", e)
    
    def get_all(self) -> Dict[str, StrategyConfig]:
        """Get all strategy configs."""
        return self._strategies.copy()
    
    def get_enabled(self) -> List[str]:
        """Get list of enabled strategy names, sorted by priority."""
        enabled = [(name, cfg) for name, cfg in self._strategies.items() if cfg.enabled]
        enabled.sort(key=lambda x: x[1].priority, reverse=True)
        return [name for name, _ in enabled]
    
    def get_disabled(self) -> List[str]:
        """Get list of disabled strategy names."""
        return [name for name, cfg in self._strategies.items() if not cfg.enabled]
    
    def is_enabled(self, name: str) -> bool:
        """Check if a strategy is enabled."""
        if name not in self._strategies:
            return True  # Unknown strategies default to enabled
        return self._strategies[name].enabled
    
    def get_strategy(self, name: str) -> Optional[StrategyConfig]:
        """Get a specific strategy config."""
        return self._strategies.get(name)
    
    def toggle(self, name: str) -> dict:
        """Toggle a strategy's enabled state."""
        if name not in self._strategies:
            return {"success": False, "error": f"Unknown strategy: {name}"}
        
        cfg = self._strategies[name]
        cfg.enabled = not cfg.enabled
        self._save_registry()
        
        logger.info("[STRAT_REG] Strategy %s %s", name, "enabled" if cfg.enabled else "disabled")
        return {
            "success": True,
            "name": name,
            "enabled": cfg.enabled,
        }
    
    def set_enabled(self, name: str, enabled: bool) -> dict:
        """Set a strategy's enabled state explicitly."""
        if name not in self._strategies:
            return {"success": False, "error": f"Unknown strategy: {name}"}
        
        cfg = self._strategies[name]
        old_state = cfg.enabled
        cfg.enabled = enabled
        self._save_registry()
        
        logger.info("[STRAT_REG] Strategy %s: %s -> %s", name, old_state, enabled)
        return {
            "success": True,
            "name": name,
            "enabled": enabled,
            "was_enabled": old_state,
        }
    
    def enable_all(self) -> dict:
        """Enable all strategies."""
        for cfg in self._strategies.values():
            cfg.enabled = True
        self._save_registry()
        
        logger.info("[STRAT_REG] All strategies enabled")
        return {"success": True, "message": "All strategies enabled"}
    
    def disable_all(self) -> dict:
        """Disable all strategies."""
        for cfg in self._strategies.values():
            cfg.enabled = False
        self._save_registry()
        
        logger.info("[STRAT_REG] All strategies disabled")
        return {"success": True, "message": "All strategies disabled"}
    
    def update_priority(self, name: str, priority: int) -> dict:
        """Update a strategy's priority."""
        if name not in self._strategies:
            return {"success": False, "error": f"Unknown strategy: {name}"}
        
        if not 1 <= priority <= 100:
            return {"success": False, "error": "Priority must be between 1 and 100"}
        
        self._strategies[name].priority = priority
        self._save_registry()
        
        return {"success": True, "name": name, "priority": priority}
    
    def record_trade(self, name: str, pnl: float, hold_minutes: float):
        """Record a completed trade for stats and check for auto-deprioritization."""
        if name not in self._strategies:
            # Create a new entry for unknown strategies
            self._strategies[name] = StrategyConfig(name=name, enabled=True)
        
        stats = self._strategies[name].stats
        stats.total_trades += 1
        stats.total_pnl += pnl
        
        if pnl > 0:
            stats.wins += 1
            stats.gross_profit += pnl  # Track gross profit
            if pnl > stats.biggest_win:
                stats.biggest_win = pnl
        else:
            stats.losses += 1
            stats.gross_loss += abs(pnl)  # Track gross loss (positive value)
            if pnl < stats.biggest_loss:
                stats.biggest_loss = pnl
        
        # Update averages
        stats.avg_pnl = stats.total_pnl / stats.total_trades
        stats.avg_hold_minutes = (
            (stats.avg_hold_minutes * (stats.total_trades - 1) + hold_minutes) 
            / stats.total_trades
        )
        stats.last_trade_at = datetime.now(timezone.utc).isoformat()
        
        # Auto-deprioritize check after recording
        self._check_auto_deprioritize(name)
        
        self._save_registry()
    
    def _check_auto_deprioritize(self, name: str):
        """
        Auto-disable strategies that are underperforming.
        
        Thresholds:
        - Win rate < 25% after 5+ trades → disable
        - Total PnL < -$10 after 3+ trades → disable  
        - 3+ consecutive losses → reduce priority
        """
        if name not in self._strategies:
            return
        
        cfg = self._strategies[name]
        stats = cfg.stats
        
        # Need minimum trades for evaluation
        if stats.total_trades < 3:
            return
        
        win_rate = stats.win_rate
        
        # Auto-disable if win rate < 25% after 5+ trades
        if stats.total_trades >= 5 and win_rate < 25.0:
            if cfg.enabled:
                cfg.enabled = False
                logger.warning(
                    "[STRAT_REG] Auto-disabled %s: win rate %.1f%% < 25%% (%d trades)",
                    name, win_rate, stats.total_trades
                )
                return
        
        # Auto-disable if bleeding money
        if stats.total_pnl < -10.0 and stats.total_trades >= 3:
            if cfg.enabled:
                cfg.enabled = False
                logger.warning(
                    "[STRAT_REG] Auto-disabled %s: total PnL $%.2f < -$10 (%d trades)",
                    name, stats.total_pnl, stats.total_trades
                )
                return
        
        # Reduce priority if recent losses (check last 3 trades via avg)
        if stats.losses >= 3 and stats.wins == 0:
            if cfg.priority > 20:
                old_priority = cfg.priority
                cfg.priority = max(20, cfg.priority - 20)
                logger.info(
                    "[STRAT_REG] Reduced %s priority %d → %d (0 wins, %d losses)",
                    name, old_priority, cfg.priority, stats.losses
                )
    
    def check_cooldown_recovery(self):
        """
        Re-enable strategies after cooldown period.
        Called periodically (e.g., daily or every few hours).
        """
        now = datetime.now(timezone.utc)
        cooldown_hours = 24  # 24 hour cooldown
        
        for name, cfg in self._strategies.items():
            if cfg.enabled:
                continue
            
            # Check if cooldown has passed
            if cfg.stats.last_trade_at:
                try:
                    last_trade = datetime.fromisoformat(cfg.stats.last_trade_at.replace('Z', '+00:00'))
                    hours_since = (now - last_trade).total_seconds() / 3600
                    
                    if hours_since >= cooldown_hours:
                        # Re-enable with reduced priority
                        cfg.enabled = True
                        cfg.priority = max(30, cfg.priority)  # Cap at 30 for recovery
                        logger.info(
                            "[STRAT_REG] Re-enabled %s after %.1fh cooldown (priority=%d)",
                            name, hours_since, cfg.priority
                        )
                except Exception:
                    pass
        
        self._save_registry()
    
    def get_stats(self, name: str) -> Optional[dict]:
        """Get stats for a specific strategy."""
        if name not in self._strategies:
            return None
        return self._strategies[name].stats.to_dict()
    
    def get_all_stats(self) -> Dict[str, dict]:
        """Get stats for all strategies."""
        return {name: cfg.stats.to_dict() for name, cfg in self._strategies.items()}
    
    def reset_stats(self, name: Optional[str] = None) -> dict:
        """Reset stats for a strategy or all strategies."""
        if name:
            if name not in self._strategies:
                return {"success": False, "error": f"Unknown strategy: {name}"}
            self._strategies[name].stats = StrategyStats()
            logger.info("[STRAT_REG] Reset stats for %s", name)
        else:
            for cfg in self._strategies.values():
                cfg.stats = StrategyStats()
            logger.info("[STRAT_REG] Reset all strategy stats")
        
        self._save_registry()
        return {"success": True}
    
    def to_dict(self) -> dict:
        """Export full registry as dict."""
        return {
            "strategies": {name: cfg.to_dict() for name, cfg in self._strategies.items()},
            "enabled_count": len(self.get_enabled()),
            "disabled_count": len(self.get_disabled()),
        }


# Singleton accessor
def get_strategy_registry() -> StrategyRegistry:
    """Get the singleton StrategyRegistry instance."""
    return StrategyRegistry()
