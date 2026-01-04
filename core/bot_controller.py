"""
Bot Controller - Dashboard as source of truth for bot lifecycle.

This module provides centralized control of bot state:
- Start/Stop commands
- Mode switching (paper/live)
- Graceful shutdown coordination

The dashboard writes commands, the launcher/bot reads and responds.

Hardened for production:
- File locking for concurrent access
- Atomic writes to prevent corruption
- Validation of all inputs
- Graceful degradation on errors
"""

import json
import os
import tempfile
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Callable
import threading
import time
import fcntl

from core.logging_utils import get_logger

logger = get_logger(__name__)

# Valid commands and modes
VALID_COMMANDS = {"run", "stop", "restart", "pause"}
VALID_MODES = {"paper", "live"}
VALID_STATUSES = {"stopped", "starting", "running", "stopping", "paused", "error"}


class BotCommand(str, Enum):
    """Commands that can be sent to the bot."""
    RUN = "run"
    STOP = "stop"
    RESTART = "restart"
    PAUSE = "pause"  # Pause trading but keep running (like kill switch)


class BotStatus(str, Enum):
    """Current bot status."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class ControlState:
    """Persistent control state - dashboard writes, bot reads."""
    # Desired state (set by dashboard)
    command: str = "run"
    mode: str = "paper"  # "paper" or "live"
    
    # Actual state (set by bot)
    status: str = "stopped"
    pid: Optional[int] = None
    error: Optional[str] = None
    
    # Timestamps
    command_at: Optional[str] = None
    status_at: Optional[str] = None
    started_at: Optional[str] = None
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "ControlState":
        return cls(
            command=data.get("command", "run"),
            mode=data.get("mode", "paper"),
            status=data.get("status", "stopped"),
            pid=data.get("pid"),
            error=data.get("error"),
            command_at=data.get("command_at"),
            status_at=data.get("status_at"),
            started_at=data.get("started_at"),
        )


class BotController:
    """
    Central controller for bot lifecycle management.
    
    Usage by dashboard (web server):
        controller = BotController()
        controller.set_mode("live")     # Request mode change
        controller.send_command("stop") # Request stop
    
    Usage by bot/launcher:
        controller = BotController()
        controller.set_status("running")
        if controller.should_stop():
            # graceful shutdown
    """
    
    _instance: Optional["BotController"] = None
    _lock = threading.Lock()
    
    def __new__(cls) -> "BotController":
        """Singleton to ensure single source of truth."""
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
        self._control_file = self._data_dir / "control.json"
        self._file_lock = threading.Lock()
        self._callbacks: list[Callable[[ControlState], None]] = []
        self._state = self._load_state()
    
    def _load_state(self) -> ControlState:
        """Load control state from file with file locking."""
        try:
            if self._control_file.exists():
                with open(self._control_file, "r") as f:
                    # Shared lock for reading
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        data = json.load(f)
                        # Validate loaded data
                        state = ControlState.from_dict(data)
                        # Sanitize values
                        if state.command not in VALID_COMMANDS:
                            state.command = "stop"
                        if state.mode not in VALID_MODES:
                            state.mode = "paper"
                        if state.status not in VALID_STATUSES:
                            state.status = "stopped"
                        return state
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except json.JSONDecodeError as e:
            logger.error("[CTRL] Corrupted control file, resetting: %s", e)
            # Reset to safe defaults
            return ControlState()
        except Exception as e:
            logger.warning("[CTRL] Failed to load control state: %s", e)
        return ControlState()
    
    def _save_state(self):
        """Persist control state to file atomically with file locking."""
        with self._file_lock:
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                
                # Atomic write: write to temp file, then rename
                temp_file = self._control_file.with_suffix('.tmp')
                with open(temp_file, "w") as f:
                    # Exclusive lock for writing
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        json.dump(self._state.to_dict(), f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                
                # Atomic rename
                shutil.move(str(temp_file), str(self._control_file))
                
            except Exception as e:
                logger.error("[CTRL] Failed to save control state: %s", e)
                # Try to clean up temp file
                try:
                    temp_file = self._control_file.with_suffix('.tmp')
                    if temp_file.exists():
                        temp_file.unlink()
                except Exception:
                    pass
    
    def _notify_callbacks(self):
        """Notify registered callbacks of state change."""
        for cb in self._callbacks:
            try:
                cb(self._state)
            except Exception as e:
                logger.warning("[CTRL] Callback error: %s", e)
    
    # === Dashboard API (write commands) ===
    
    def send_command(self, command: str) -> dict:
        """
        Send a command to the bot.
        
        Args:
            command: "run", "stop", "restart", "pause"
        
        Returns:
            dict with success status and message
        """
        # Validate command
        if not command or not isinstance(command, str):
            return {"success": False, "error": "Command must be a non-empty string"}
        
        command = command.lower().strip()
        if command not in VALID_COMMANDS:
            return {"success": False, "error": f"Invalid command: {command}. Valid: {', '.join(VALID_COMMANDS)}"}
        
        self._state.command = command
        self._state.command_at = datetime.now(timezone.utc).isoformat()
        self._save_state()
        self._notify_callbacks()
        
        logger.info("[CTRL] Command sent: %s", command)
        return {
            "success": True,
            "command": command,
            "message": f"Command '{command}' sent to bot"
        }
    
    def set_mode(self, mode: str) -> dict:
        """
        Request mode change (paper/live).
        
        Note: Mode change requires restart to take effect safely.
        This sets the desired mode - launcher will restart bot.
        """
        # Validate mode
        if not mode or not isinstance(mode, str):
            return {"success": False, "error": "Mode must be a non-empty string"}
        
        mode = mode.lower().strip()
        if mode not in VALID_MODES:
            return {"success": False, "error": f"Invalid mode: {mode}. Valid: {', '.join(VALID_MODES)}"}
        
        current_mode = self._state.mode
        if mode == current_mode:
            return {"success": True, "mode": mode, "message": "Already in this mode"}
        
        self._state.mode = mode
        self._state.command = "restart"  # Mode change requires restart
        self._state.command_at = datetime.now(timezone.utc).isoformat()
        self._save_state()
        self._notify_callbacks()
        
        logger.info("[CTRL] Mode change requested: %s -> %s", current_mode, mode)
        return {
            "success": True,
            "mode": mode,
            "previous_mode": current_mode,
            "message": f"Mode changed to '{mode}' - restart triggered"
        }
    
    # === Bot API (read state, set status) ===
    
    def get_state(self) -> ControlState:
        """Get current control state (bot polls this)."""
        self._state = self._load_state()  # Refresh from file
        return self._state
    
    def get_desired_mode(self) -> str:
        """Get the mode the dashboard wants."""
        self._state = self._load_state()
        return self._state.mode
    
    def get_command(self) -> str:
        """Get the current command."""
        self._state = self._load_state()
        return self._state.command
    
    def should_stop(self) -> bool:
        """Check if bot should stop (dashboard requested stop/restart)."""
        self._state = self._load_state()
        return self._state.command in ("stop", "restart")
    
    def should_pause(self) -> bool:
        """Check if trading should be paused."""
        self._state = self._load_state()
        return self._state.command == "pause"
    
    def set_status(self, status: str, error: Optional[str] = None):
        """Update bot status (called by bot/launcher)."""
        self._state.status = status
        self._state.status_at = datetime.now(timezone.utc).isoformat()
        self._state.error = error
        
        if status == "running":
            self._state.pid = os.getpid()
            self._state.started_at = datetime.now(timezone.utc).isoformat()
        elif status == "stopped":
            self._state.pid = None
        
        self._save_state()
        logger.info("[CTRL] Status updated: %s", status)
    
    def acknowledge_command(self):
        """
        Acknowledge command was processed.
        Called after restart completes or stop is handled.
        """
        if self._state.command == "restart":
            self._state.command = "run"  # Reset to run after restart
            self._state.command_at = datetime.now(timezone.utc).isoformat()
            self._save_state()
    
    def register_callback(self, callback: Callable[[ControlState], None]):
        """Register callback for state changes."""
        self._callbacks.append(callback)
    
    # === Convenience properties ===
    
    @property
    def mode(self) -> str:
        return self._state.mode
    
    @property
    def status(self) -> str:
        return self._state.status
    
    @property
    def command(self) -> str:
        return self._state.command
    
    @property 
    def is_running(self) -> bool:
        return self._state.status in ("running", "starting")
    
    @property
    def is_paused(self) -> bool:
        return self._state.command == "pause" or self._state.status == "paused"


# Singleton accessor
def get_controller() -> BotController:
    """Get the singleton BotController instance."""
    return BotController()
