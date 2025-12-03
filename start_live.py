#!/usr/bin/env python3
"""
Live Mode Starter - Properly sets live mode before any imports

This ensures live mode is set before any configuration is loaded.
"""

import os
import sys

# MUST set mode before any imports
os.environ['TRADING_MODE'] = 'live'

# Now import and run
sys.path.append('.')

if __name__ == "__main__":
    import asyncio
    
    # Import run_v2's TradingBotV2 after mode is set
    from run_v2 import TradingBotV2
    
    print("🚀 Starting in LIVE mode...")
    
    bot = TradingBotV2()
    
    # Set up signal handlers
    import signal as sig
    
    def handle_interrupt(signum, frame):
        print("\n[BOT] Shutting down gracefully...")
        bot._running = False
    
    sig.signal(sig.SIGINT, handle_interrupt)
    sig.signal(sig.SIGTERM, handle_interrupt)
    
    try:
        asyncio.run(bot.start())
    except KeyboardInterrupt:
        print("[BOT] Exiting...")
    except Exception as e:
        print(f"[BOT] Error: {e}")
        import traceback
        traceback.print_exc()
