#!/usr/bin/env python3
"""
TUI & Bot State Validator
Run: python3 tools/validate_tui.py

Checks all data flows and identifies issues.
"""

import sys
sys.path.insert(0, ".")

from datetime import datetime, timezone

def check(name: str, condition: bool, detail: str = ""):
    status = "✅" if condition else "❌"
    print(f"{status} {name}: {detail if detail else ('OK' if condition else 'FAIL')}")
    return condition

def main():
    print("=" * 60)
    print("TUI & BOT STATE VALIDATION")
    print("=" * 60)
    
    errors = []
    
    # 1. Check imports
    print("\n📦 IMPORTS:")
    try:
        from core.state import BotState, CurrentSignal, PositionDisplay, FocusCoinState
        check("BotState import", True)
    except Exception as e:
        check("BotState import", False, str(e))
        errors.append(f"Import: {e}")
    
    try:
        from apps.dashboard.tui_live import (
            LiveStatusBar, LiveScanner, LivePositions, LiveSignal,
            LiveStats, LiveHealth, LiveLogs, LiveVerdicts, LiveActivity
        )
        check("TUI panels import", True)
    except Exception as e:
        check("TUI panels import", False, str(e))
        errors.append(f"TUI import: {e}")
    
    # 2. Check BotState fields
    print("\n📊 BOTSTATE FIELDS:")
    state = BotState()
    
    required_fields = [
        "portfolio_value", "cash_balance", "holdings_value",
        "positions", "burst_leaderboard", "current_signal", "focus_coin",
        "ws_ok", "ws_last_age", "portfolio_snapshot_age_s",
        "trades_today", "wins_today", "losses_today", "daily_pnl",
        "rejections_spread", "rejections_score", "rejections_rr", "rejections_limits",
        "ticks_last_5s", "candles_last_5s", "events_last_5s",
        "live_log", "universe"
    ]
    
    for field in required_fields:
        exists = hasattr(state, field)
        check(f"state.{field}", exists, f"type={type(getattr(state, field, None)).__name__}" if exists else "MISSING")
        if not exists:
            errors.append(f"Missing field: {field}")
    
    # 3. Check CurrentSignal has symbol
    print("\n🎯 CURRENT SIGNAL:")
    sig = state.current_signal
    check("current_signal.symbol exists", hasattr(sig, "symbol"), f"value='{getattr(sig, 'symbol', 'N/A')}'")
    check("current_signal.action exists", hasattr(sig, "action"), f"value='{getattr(sig, 'action', 'N/A')}'")
    check("current_signal.entry_price exists", hasattr(sig, "entry_price"))
    
    # 4. Check TUI panels render without error
    print("\n🖥️ TUI PANEL RENDERING:")
    panels = [
        ("LiveStatusBar", LiveStatusBar),
        ("LiveScanner", LiveScanner),
        ("LivePositions", LivePositions),
        ("LiveSignal", LiveSignal),
        ("LiveStats", LiveStats),
        ("LiveHealth", LiveHealth),
        ("LiveLogs", LiveLogs),
        ("LiveVerdicts", LiveVerdicts),
        ("LiveActivity", LiveActivity),
    ]
    
    for name, panel_class in panels:
        try:
            panel = panel_class(state)
            output = panel.render()
            check(f"{name}.render()", True, f"len={len(output)}")
        except Exception as e:
            check(f"{name}.render()", False, str(e)[:50])
            errors.append(f"{name}: {e}")
    
    # 5. Check logging suppression
    print("\n🔇 LOGGING SUPPRESSION:")
    try:
        from core.logging_utils import suppress_console_logging
        import logging
        
        suppress_console_logging(True)
        coinbase_logger = logging.getLogger("coinbase.RESTClient")
        level = coinbase_logger.level
        check("coinbase logger suppressed", level > logging.ERROR, f"level={level}")
        suppress_console_logging(False)
    except Exception as e:
        check("logging suppression", False, str(e))
        errors.append(f"Logging: {e}")
    
    # 6. Check counter reset timing
    print("\n⏱️ COUNTER TIMING:")
    check("ticks_last_5s type", isinstance(state.ticks_last_5s, int))
    check("candles_last_5s type", isinstance(state.candles_last_5s, int))
    
    # 7. Check strategies
    print("\n🧠 STRATEGY ORCHESTRATOR:")
    try:
        from logic.strategies.orchestrator import StrategyOrchestrator, OrchestratorConfig
        orch = StrategyOrchestrator()
        check("orchestrator init", True, f"{len(orch.strategies)} strategies")
        for strat in orch.strategies:
            check(f"  - {strat.strategy_id}", True)
    except Exception as e:
        check("orchestrator", False, str(e))
        errors.append(f"Orchestrator: {e}")
    
    # 8. Check candle store
    print("\n📈 CANDLE STORE:")
    try:
        from services.candle_store import candle_store
        symbols = candle_store.list_symbols()
        check("candle_store.list_symbols()", True, f"{len(symbols)} symbols")
    except Exception as e:
        check("candle_store", False, str(e)[:50])
    
    # Summary
    print("\n" + "=" * 60)
    if errors:
        print(f"❌ VALIDATION FAILED: {len(errors)} errors")
        for err in errors:
            print(f"   - {err}")
    else:
        print("✅ ALL VALIDATIONS PASSED")
    print("=" * 60)
    
    return len(errors) == 0

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
