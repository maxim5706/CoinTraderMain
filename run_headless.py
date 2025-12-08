#!/usr/bin/env python3
"""
Headless CoinTrader Bot Runner.

Runs bot without TUI dashboard for production deployment.
Logs all output to files instead of terminal.
"""

import asyncio
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone

# Ensure logs directory exists
Path("logs/headless").mkdir(parents=True, exist_ok=True)

# Redirect stdout/stderr to log files
log_file = Path("logs/headless") / f"bot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"
print(f"Starting headless mode. Logs: {log_file}")

sys.stdout = open(log_file, 'a', buffering=1)
sys.stderr = open(log_file, 'a', buffering=1)

print(f"\n{'='*80}")
print(f"CoinTrader Bot - Headless Mode")
print(f"Started: {datetime.now(timezone.utc).isoformat()}")
print(f"{'='*80}\n")

from run_v2 import BotV2
from core.config import settings
from core.mode_configs import TradingMode


async def run_headless():
    """Run bot in headless mode."""
    
    # Determine mode from env
    mode_str = settings.trading_mode.upper() if hasattr(settings, 'trading_mode') else "PAPER"
    mode = TradingMode.LIVE if mode_str == "LIVE" else TradingMode.PAPER
    
    print(f"[HEADLESS] Mode: {mode.value}")
    print(f"[HEADLESS] Max positions: {settings.max_positions}")
    print(f"[HEADLESS] Daily max loss: ${settings.daily_max_loss_usd}")
    print(f"[HEADLESS] Entry score min: {settings.entry_score_min}")
    print()
    
    # Create bot instance
    bot = BotV2(mode=mode, headless=True)
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        print(f"\n[HEADLESS] Received signal {sig}, shutting down gracefully...")
        bot._running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Start bot
    print("[HEADLESS] Starting bot...")
    try:
        await bot.start()
    except KeyboardInterrupt:
        print("\n[HEADLESS] Keyboard interrupt received")
    except Exception as e:
        print(f"\n[HEADLESS] ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("[HEADLESS] Shutdown complete")
        print(f"[HEADLESS] Stopped: {datetime.now(timezone.utc).isoformat()}")


def main():
    """Main entry point."""
    try:
        asyncio.run(run_headless())
    except Exception as e:
        print(f"[HEADLESS] Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
