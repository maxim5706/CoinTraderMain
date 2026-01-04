#!/usr/bin/env python3
"""
CoinTrader Launcher - Dashboard-controlled bot supervisor.

This script acts as a supervisor that:
1. Starts the web dashboard (always running)
2. Watches control.json for commands from the dashboard
3. Starts/stops/restarts the trading bot based on dashboard commands
4. Handles mode switching via restart

Usage:
    python launcher.py              # Start launcher (dashboard controls bot)
    python launcher.py --autostart  # Start launcher and immediately run bot

The dashboard becomes the source of truth - use it to:
- Switch between paper/live modes
- Start/stop the bot
- Restart when needed

Hardened for production:
- Graceful error handling
- Process monitoring and recovery
- Clean shutdown on signals
- Startup validation
"""

import asyncio
import argparse
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from core.bot_controller import get_controller, BotStatus
from core.logging_utils import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)


class BotLauncher:
    """
    Supervisor that manages bot lifecycle based on dashboard commands.
    
    Architecture:
    - Web server runs in background thread (always on)
    - Main loop polls control state
    - Bot process spawned/killed based on commands
    
    Hardened:
    - Validates environment before starting
    - Monitors process health
    - Handles unexpected crashes
    - Clean signal handling
    """
    
    def __init__(self, autostart: bool = False):
        self.controller = get_controller()
        self.bot_process: subprocess.Popen | None = None
        self.web_server_task: asyncio.Task | None = None
        self._running = True
        self._autostart = autostart
        self._restart_count = 0
        self._max_restarts = 5  # Max restarts within window
        self._restart_window = 300  # 5 minute window
        self._restart_times: list[float] = []
        self._last_error: str | None = None
    
    async def start(self):
        """Start the launcher."""
        logger.info("[LAUNCHER] Starting CoinTrader Launcher")
        logger.info("[LAUNCHER] Dashboard will be source of truth for bot control")
        
        # Validate environment
        if not self._validate_environment():
            logger.error("[LAUNCHER] Environment validation failed")
            return
        
        # Set initial status
        self.controller.set_status("stopped")
        
        # NOTE: Web server is started BY THE BOT (run_v2.py), not here
        # This launcher just manages the bot process lifecycle
        
        # If autostart requested, set command to run
        if self._autostart:
            logger.info("[LAUNCHER] Autostart enabled - starting bot")
            self.controller.send_command("run")
        
        # Main control loop
        await self._control_loop()
    
    def _validate_environment(self) -> bool:
        """Validate environment before starting."""
        project_root = Path(__file__).parent
        
        # Check required files exist
        required_files = [
            project_root / "run_v2.py",
            project_root / "core" / "bot_controller.py",
            project_root / "ui" / "web_server.py",
        ]
        
        for f in required_files:
            if not f.exists():
                logger.error("[LAUNCHER] Required file missing: %s", f)
                return False
        
        # Ensure data directory exists
        data_dir = project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        
        # Check Python version
        if sys.version_info < (3, 10):
            logger.error("[LAUNCHER] Python 3.10+ required, got %s", sys.version)
            return False
        
        logger.info("[LAUNCHER] Environment validation passed")
        return True
    
    def _check_restart_limit(self) -> bool:
        """Check if we've exceeded restart limits (crash loop protection)."""
        now = time.time()
        # Remove old restart times outside window
        self._restart_times = [t for t in self._restart_times if now - t < self._restart_window]
        
        if len(self._restart_times) >= self._max_restarts:
            logger.error(
                "[LAUNCHER] Restart limit exceeded (%d restarts in %ds) - stopping",
                self._max_restarts, self._restart_window
            )
            self.controller.set_status("error", error="Restart limit exceeded (crash loop detected)")
            return False
        
        self._restart_times.append(now)
        return True
    
    def _print_status(self):
        """Print current status to console."""
        state = self.controller.get_state()
        status_icon = {
            "running": "🟢",
            "stopped": "🔴",
            "starting": "🟡",
            "stopping": "🟡",
            "error": "❌",
        }.get(state.status, "⚪")
        print(f"\r{status_icon} Status: {state.status} | Mode: {state.mode} | Command: {state.command}    ", end="", flush=True)
    
    async def _control_loop(self):
        """Main loop - watch control state and manage bot."""
        logger.info("[LAUNCHER] Control loop started - watching for commands")
        print("\n" + "="*60)
        print("  CoinTrader Launcher Active")
        print("  Dashboard: http://localhost:8080 (when bot is running)")
        print("  Use dashboard OR edit data/control.json to control bot")
        print("="*60 + "\n")
        
        last_command = None
        
        while self._running:
            try:
                state = self.controller.get_state()
                command = state.command
                mode = state.mode
                
                # Only act on command changes or if we need to sync state
                if command != last_command:
                    logger.info("[LAUNCHER] Command changed: %s -> %s", last_command, command)
                    last_command = command
                    
                    if command == "run":
                        if not self._is_bot_running():
                            if self._check_restart_limit():
                                await self._start_bot(mode)
                            else:
                                # Reset command to stop to prevent retry loop
                                self.controller.send_command("stop")
                    
                    elif command == "stop":
                        if self._is_bot_running():
                            await self._stop_bot()
                        self.controller.set_status("stopped")
                        self._last_error = None  # Clear error on manual stop
                    
                    elif command == "restart":
                        logger.info("[LAUNCHER] Restart requested (mode: %s)", mode)
                        if not self._check_restart_limit():
                            self.controller.send_command("stop")
                            continue
                        if self._is_bot_running():
                            await self._stop_bot()
                        await asyncio.sleep(2)  # Brief pause
                        await self._start_bot(mode)
                        self.controller.acknowledge_command()
                    
                    elif command == "pause":
                        # Pause just sets kill_switch, bot keeps running
                        self.controller.set_status("paused")
                
                # Check if bot died unexpectedly
                if self._bot_process_died() and state.status == "running":
                    exit_code = self.bot_process.returncode if self.bot_process else -1
                    error_msg = f"Process died (exit code: {exit_code})"
                    logger.warning("[LAUNCHER] Bot process died unexpectedly: %s", error_msg)
                    self._last_error = error_msg
                    self.controller.set_status("stopped", error=error_msg)
                    self.bot_process = None
                    
                    # Auto-restart if command is still 'run' and within limits
                    if state.command == "run" and self._check_restart_limit():
                        logger.info("[LAUNCHER] Auto-restarting bot...")
                        await asyncio.sleep(3)
                        await self._start_bot(mode)
                
                await asyncio.sleep(1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[LAUNCHER] Control loop error: %s", e)
                await asyncio.sleep(5)
        
        # Cleanup
        await self._stop_bot()
        logger.info("[LAUNCHER] Shutdown complete")
    
    def _is_bot_running(self) -> bool:
        """Check if bot process is running."""
        if self.bot_process is None:
            return False
        return self.bot_process.poll() is None
    
    def _bot_process_died(self) -> bool:
        """Check if bot process died (was running but now isn't)."""
        if self.bot_process is None:
            return False
        return self.bot_process.poll() is not None
    
    async def _start_bot(self, mode: str):
        """Start the trading bot in specified mode."""
        if self._is_bot_running():
            logger.warning("[LAUNCHER] Bot already running, ignoring start")
            return
        
        logger.info("[LAUNCHER] Starting bot in %s mode", mode.upper())
        self.controller.set_status("starting")
        
        try:
            # Build command
            cmd = [
                sys.executable,
                str(Path(__file__).parent / "run_v2.py"),
                f"--mode={mode}",
                "--launcher"  # Flag to indicate launched by supervisor
            ]
            
            # Set environment
            env = os.environ.copy()
            env["TRADING_MODE"] = mode
            
            # Start process
            self.bot_process = subprocess.Popen(
                cmd,
                env=env,
                cwd=str(Path(__file__).parent),
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
            
            # Wait briefly to check if it started successfully
            await asyncio.sleep(3)
            
            if self._is_bot_running():
                self.controller.set_status("running")
                logger.info("[LAUNCHER] Bot started successfully (PID: %s)", self.bot_process.pid)
            else:
                exit_code = self.bot_process.returncode
                self.controller.set_status("error", error=f"Failed to start (exit: {exit_code})")
                logger.error("[LAUNCHER] Bot failed to start (exit code: %s)", exit_code)
                
        except Exception as e:
            self.controller.set_status("error", error=str(e))
            logger.error("[LAUNCHER] Failed to start bot: %s", e)
    
    async def _stop_bot(self):
        """Stop the trading bot gracefully."""
        if not self._is_bot_running():
            return
        
        logger.info("[LAUNCHER] Stopping bot (PID: %s)", self.bot_process.pid)
        self.controller.set_status("stopping")
        
        try:
            # Send SIGTERM for graceful shutdown
            self.bot_process.terminate()
            
            # Wait up to 30 seconds for graceful shutdown
            for _ in range(30):
                if self.bot_process.poll() is not None:
                    break
                await asyncio.sleep(1)
            
            # Force kill if still running
            if self._is_bot_running():
                logger.warning("[LAUNCHER] Bot didn't stop gracefully, forcing kill")
                self.bot_process.kill()
                await asyncio.sleep(1)
            
            self.controller.set_status("stopped")
            logger.info("[LAUNCHER] Bot stopped")
            
        except Exception as e:
            logger.error("[LAUNCHER] Error stopping bot: %s", e)
            self.controller.set_status("error", error=str(e))
        finally:
            self.bot_process = None
    
    def handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("[LAUNCHER] Received signal %s, shutting down", signum)
        self._running = False


async def main():
    parser = argparse.ArgumentParser(description="CoinTrader Launcher")
    parser.add_argument(
        "--autostart",
        action="store_true",
        help="Automatically start the bot on launch"
    )
    args = parser.parse_args()
    
    launcher = BotLauncher(autostart=args.autostart)
    
    # Handle signals
    signal.signal(signal.SIGINT, launcher.handle_signal)
    signal.signal(signal.SIGTERM, launcher.handle_signal)
    
    await launcher.start()


if __name__ == "__main__":
    asyncio.run(main())
