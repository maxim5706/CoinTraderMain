"""
Dynamic Configuration Manager - Live parameter updates from web dashboard.

Allows real-time adjustment of trading parameters without restart.
Changes are validated, applied to settings, and persisted to disk.
"""

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Callable
import fcntl

from core.logging_utils import get_logger
from core.config import settings

logger = get_logger(__name__)


@dataclass
class RuntimeConfig:
    """Runtime-adjustable configuration parameters."""
    
    # Risk Controls
    max_exposure_pct: float = 80.0
    daily_loss_limit_usd: float = 25.0
    position_base_pct: float = 3.0
    position_min_pct: float = 2.0
    position_max_pct: float = 8.0
    
    # Position Sizing Tiers
    whale_trade_usd: float = 30.0
    strong_trade_usd: float = 15.0
    normal_trade_usd: float = 10.0
    scout_trade_usd: float = 5.0
    
    # Entry Filters
    entry_score_min: float = 55.0
    spread_max_bps: float = 50.0
    min_rr_ratio: float = 1.5
    vol_spike_threshold: float = 1.5
    
    # Stop/TP Settings
    fixed_stop_pct: float = 5.0
    tp1_pct: float = 8.0
    tp2_pct: float = 10.0
    tp1_partial_pct: float = 50.0
    max_hold_minutes: int = 120
    
    # Fast Mode
    fast_mode_enabled: bool = True
    fast_confidence_min: float = 65.0
    fast_spread_max_bps: float = 18.0
    fast_stop_pct: float = 2.5
    fast_tp1_pct: float = 4.0
    
    # Trading Controls
    pause_new_entries: bool = False
    
    # Timestamps
    updated_at: Optional[str] = None
    updated_by: str = "system"
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeConfig":
        # Filter to only known fields
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)
    
    @classmethod
    def from_settings(cls) -> "RuntimeConfig":
        """Initialize from current settings."""
        return cls(
            max_exposure_pct=settings.portfolio_max_exposure_pct * 100,
            daily_loss_limit_usd=settings.daily_max_loss_usd,
            position_base_pct=settings.position_base_pct * 100,
            position_min_pct=settings.position_min_pct * 100,
            position_max_pct=settings.position_max_pct * 100,
            whale_trade_usd=settings.whale_trade_usd,
            strong_trade_usd=settings.strong_trade_usd,
            normal_trade_usd=settings.normal_trade_usd,
            scout_trade_usd=settings.scout_trade_usd,
            entry_score_min=settings.entry_score_min,
            spread_max_bps=settings.spread_max_bps,
            min_rr_ratio=settings.min_rr_ratio,
            vol_spike_threshold=settings.vol_spike_threshold,
            fixed_stop_pct=settings.fixed_stop_pct * 100,
            tp1_pct=settings.tp1_pct * 100,
            tp2_pct=settings.tp2_pct * 100,
            tp1_partial_pct=settings.tp1_partial_pct * 100,
            max_hold_minutes=settings.max_hold_minutes,
            fast_mode_enabled=settings.fast_mode_enabled,
            fast_confidence_min=settings.fast_confidence_min * 100,
            fast_spread_max_bps=settings.fast_spread_max_bps,
            fast_stop_pct=settings.fast_stop_pct,
            fast_tp1_pct=settings.fast_tp1_pct,
            pause_new_entries=False,
        )


# Mapping of runtime config params to settings attributes.
PARAM_SETTINGS_MAP = {
    "max_exposure_pct": ("portfolio_max_exposure_pct", lambda v: v / 100),
    "daily_loss_limit_usd": ("daily_max_loss_usd", lambda v: v),
    "position_base_pct": ("position_base_pct", lambda v: v / 100),
    "position_min_pct": ("position_min_pct", lambda v: v / 100),
    "position_max_pct": ("position_max_pct", lambda v: v / 100),
    "whale_trade_usd": ("whale_trade_usd", lambda v: v),
    "strong_trade_usd": ("strong_trade_usd", lambda v: v),
    "normal_trade_usd": ("normal_trade_usd", lambda v: v),
    "scout_trade_usd": ("scout_trade_usd", lambda v: v),
    "entry_score_min": ("entry_score_min", lambda v: v),
    "spread_max_bps": ("spread_max_bps", lambda v: v),
    "min_rr_ratio": ("min_rr_ratio", lambda v: v),
    "vol_spike_threshold": ("vol_spike_threshold", lambda v: v),
    "fixed_stop_pct": ("fixed_stop_pct", lambda v: v / 100),
    "tp1_pct": ("tp1_pct", lambda v: v / 100),
    "tp2_pct": ("tp2_pct", lambda v: v / 100),
    "tp1_partial_pct": ("tp1_partial_pct", lambda v: v / 100),
    "max_hold_minutes": ("max_hold_minutes", lambda v: int(v)),
    "fast_mode_enabled": ("fast_mode_enabled", lambda v: bool(v)),
    "fast_confidence_min": ("fast_confidence_min", lambda v: v / 100),
    "fast_spread_max_bps": ("fast_spread_max_bps", lambda v: v),
    "fast_stop_pct": ("fast_stop_pct", lambda v: v),
    "fast_tp1_pct": ("fast_tp1_pct", lambda v: v),
}


# Validation rules for each parameter
PARAM_VALIDATORS = {
    "max_exposure_pct": lambda v: 10.0 <= v <= 100.0,
    "daily_loss_limit_usd": lambda v: 5.0 <= v <= 500.0,
    "position_base_pct": lambda v: 1.0 <= v <= 20.0,
    "position_min_pct": lambda v: 0.5 <= v <= 10.0,
    "position_max_pct": lambda v: 2.0 <= v <= 25.0,
    "whale_trade_usd": lambda v: 5.0 <= v <= 200.0,
    "strong_trade_usd": lambda v: 5.0 <= v <= 100.0,
    "normal_trade_usd": lambda v: 2.0 <= v <= 50.0,
    "scout_trade_usd": lambda v: 1.0 <= v <= 25.0,
    "entry_score_min": lambda v: 20.0 <= v <= 90.0,
    "spread_max_bps": lambda v: 5.0 <= v <= 100.0,
    "min_rr_ratio": lambda v: 1.0 <= v <= 5.0,
    "vol_spike_threshold": lambda v: 1.0 <= v <= 10.0,
    "fixed_stop_pct": lambda v: 1.0 <= v <= 10.0,
    "tp1_pct": lambda v: 2.0 <= v <= 20.0,
    "tp2_pct": lambda v: 3.0 <= v <= 30.0,
    "tp1_partial_pct": lambda v: 20.0 <= v <= 80.0,
    "max_hold_minutes": lambda v: 15 <= v <= 480,
    "fast_confidence_min": lambda v: 50.0 <= v <= 95.0,
    "fast_spread_max_bps": lambda v: 5.0 <= v <= 50.0,
    "fast_stop_pct": lambda v: 1.0 <= v <= 5.0,
    "fast_tp1_pct": lambda v: 2.0 <= v <= 10.0,
}


class ConfigManager:
    """
    Manages runtime configuration with persistence.
    
    Features:
    - Load/save config to JSON file
    - Validate parameter changes
    - Apply changes to settings object
    - Audit trail of changes
    """
    
    _instance: Optional["ConfigManager"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "ConfigManager":
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
        self._config_file = self._data_dir / "runtime_config.json"
        self._audit_file = self._data_dir / "config_audit.jsonl"
        self._file_lock = threading.Lock()
        self._callbacks: list[Callable[[RuntimeConfig], None]] = []
        self._last_loaded_mtime: Optional[float] = None
        self._config = self._load_config()
        self._apply_all_to_settings()
    
    def _load_config(self) -> RuntimeConfig:
        """Load config from file, or initialize from settings."""
        try:
            if self._config_file.exists():
                self._last_loaded_mtime = self._config_file.stat().st_mtime
                with open(self._config_file, "r") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        data = json.load(f)
                        return RuntimeConfig.from_dict(data)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logger.warning("[CONFIG] Failed to load config, using defaults: %s", e)
        
        # Initialize from current settings
        return RuntimeConfig.from_settings()
    
    def _save_config(self):
        """Persist config to file atomically."""
        with self._file_lock:
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                temp_file = self._config_file.with_suffix('.tmp')
                
                with open(temp_file, "w") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(self._config.to_dict(), f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                
                os.replace(str(temp_file), str(self._config_file))
                self._last_loaded_mtime = self._config_file.stat().st_mtime
                
            except Exception as e:
                logger.error("[CONFIG] Failed to save config: %s", e)
    
    def _audit_log(self, param: str, old_value: Any, new_value: Any, source: str):
        """Log config change to audit file."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "param": param,
                "old": old_value,
                "new": new_value,
                "source": source,
            }
            with open(self._audit_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning("[CONFIG] Failed to write audit log: %s", e)
    
    def _apply_to_settings(self, param: str, value: Any):
        """Apply a config change to the global settings object."""
        if param in PARAM_SETTINGS_MAP:
            attr, transform = PARAM_SETTINGS_MAP[param]
            try:
                setattr(settings, attr, transform(value))
                logger.info("[CONFIG] Applied %s = %s to settings", attr, transform(value))
            except Exception as e:
                logger.error("[CONFIG] Failed to apply %s: %s", param, e)

    def _apply_all_to_settings(self) -> None:
        """Apply the full runtime config snapshot to settings."""
        for param, (attr, transform) in PARAM_SETTINGS_MAP.items():
            if not hasattr(self._config, param):
                continue
            try:
                value = getattr(self._config, param)
                setattr(settings, attr, transform(value))
            except Exception as e:
                logger.error("[CONFIG] Failed to apply %s: %s", param, e)

    def _notify_callbacks(self) -> None:
        """Notify registered callbacks about config changes."""
        for cb in self._callbacks:
            try:
                cb(self._config)
            except Exception as e:
                logger.warning("[CONFIG] Callback error: %s", e)
    
    def get_config(self) -> RuntimeConfig:
        """Get current runtime config."""
        return self._config
    
    def get_param(self, param: str) -> Any:
        """Get a single parameter value."""
        return getattr(self._config, param, None)
    
    def update_param(self, param: str, value: Any, source: str = "web") -> dict:
        """
        Update a single parameter.
        
        Returns dict with success status and message.
        """
        # Check param exists
        if not hasattr(self._config, param):
            return {"success": False, "error": f"Unknown parameter: {param}"}
        
        # Validate value
        if param in PARAM_VALIDATORS:
            try:
                if not PARAM_VALIDATORS[param](value):
                    return {"success": False, "error": f"Value {value} out of range for {param}"}
            except Exception as e:
                return {"success": False, "error": f"Validation error: {e}"}
        
        # Get old value
        old_value = getattr(self._config, param)
        
        # Update config
        setattr(self._config, param, value)
        self._config.updated_at = datetime.now(timezone.utc).isoformat()
        self._config.updated_by = source
        
        # Apply to settings
        self._apply_to_settings(param, value)
        
        # Save and audit
        self._save_config()
        self._audit_log(param, old_value, value, source)
        
        self._notify_callbacks()
        
        logger.info("[CONFIG] Updated %s: %s -> %s (by %s)", param, old_value, value, source)
        return {
            "success": True,
            "param": param,
            "old_value": old_value,
            "new_value": value,
        }
    
    def update_params(self, updates: dict, source: str = "web") -> dict:
        """
        Update multiple parameters at once.
        
        Returns dict with success status and list of changes.
        """
        results = []
        errors = []
        
        for param, value in updates.items():
            result = self.update_param(param, value, source)
            if result.get("success"):
                results.append(result)
            else:
                errors.append({"param": param, "error": result.get("error")})
        
        return {
            "success": len(errors) == 0,
            "updated": results,
            "errors": errors,
        }
    
    def reset_to_defaults(self, source: str = "web") -> dict:
        """Reset all config to defaults from settings."""
        old_config = self._config.to_dict()
        self._config = RuntimeConfig.from_settings()
        self._config.updated_at = datetime.now(timezone.utc).isoformat()
        self._config.updated_by = source
        self._save_config()
        self._apply_all_to_settings()
        self._audit_log("*all*", old_config, "reset", source)
        
        logger.info("[CONFIG] Reset to defaults by %s", source)
        return {"success": True, "message": "Config reset to defaults"}
    
    def register_callback(self, callback: Callable[[RuntimeConfig], None]):
        """Register callback for config changes."""
        self._callbacks.append(callback)
    
    def get_audit_log(self, limit: int = 50) -> list[dict]:
        """Get recent audit log entries."""
        entries = []
        try:
            if self._audit_file.exists():
                with open(self._audit_file, "r") as f:
                    lines = f.readlines()
                    for line in lines[-limit:]:
                        try:
                            entries.append(json.loads(line.strip()))
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("[CONFIG] Failed to read audit log: %s", e)
        return entries
    
    @property
    def pause_new_entries(self) -> bool:
        """Check if new entries are paused."""
        return self._config.pause_new_entries
    
    def set_pause_new_entries(self, paused: bool, source: str = "web") -> dict:
        """Pause or resume new entries."""
        return self.update_param("pause_new_entries", paused, source)

    def reload_if_changed(self) -> bool:
        """Reload runtime config from disk if the file changed."""
        if not self._config_file.exists():
            return False
        return self.reload_from_disk(force=False)

    def reload_from_disk(self, force: bool = False) -> bool:
        """Reload runtime config from disk regardless of mtime when forced."""
        try:
            current_mtime = None
            if self._config_file.exists():
                current_mtime = self._config_file.stat().st_mtime
                if (
                    not force
                    and self._last_loaded_mtime is not None
                    and current_mtime <= self._last_loaded_mtime
                ):
                    return False
            with self._file_lock:
                new_config = self._load_config()
                self._config = new_config
                self._apply_all_to_settings()
            self._notify_callbacks()
            logger.info(
                "[CONFIG] Reloaded runtime config from disk%s",
                " (forced)" if force else "",
            )
            return True
        except Exception as e:
            logger.warning("[CONFIG] Reload failed: %s", e)
            return False


# Singleton accessor
def get_config_manager() -> ConfigManager:
    """Get the singleton ConfigManager instance."""
    return ConfigManager()
