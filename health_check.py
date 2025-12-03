#!/usr/bin/env python3
"""
System Health Check - Quick verification before trading

Run this before starting the bot to ensure everything is working.
"""

import sys
import os
import asyncio
from datetime import datetime, timezone

sys.path.append('.')

def print_header(title: str):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print('='*50)


def check_mark(passed: bool) -> str:
    return "✅" if passed else "❌"


async def run_health_check():
    """Run comprehensive health check."""
    
    print("🏥 COINTRADER HEALTH CHECK")
    print("="*50)
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    
    all_passed = True
    
    # ============================================================
    # 1. CONFIGURATION CHECK
    # ============================================================
    print_header("1. CONFIGURATION")
    
    try:
        from core.config import settings
        
        checks = [
            ("API Keys", bool(settings.coinbase_api_key)),
            ("Trading Mode", settings.trading_mode in ["paper", "live"]),
            ("Max Trade USD", settings.max_trade_usd > 0),
            ("Daily Max Loss", settings.daily_max_loss_usd > 0),
            ("Entry Score Min", 0 <= settings.entry_score_min <= 100),
        ]
        
        for name, passed in checks:
            print(f"  {check_mark(passed)} {name}: {passed}")
            if not passed:
                all_passed = False
                
        print(f"\n  Mode: {settings.trading_mode.upper()}")
        print(f"  Max Trade: ${settings.max_trade_usd}")
        print(f"  Daily Loss Limit: ${settings.daily_max_loss_usd}")
        
    except Exception as e:
        print(f"  ❌ Configuration Error: {e}")
        all_passed = False
    
    # ============================================================
    # 2. CORE COMPONENTS
    # ============================================================
    print_header("2. CORE COMPONENTS")
    
    components = []
    
    try:
        from core.pnl_engine import PnLEngine
        components.append(("PnL Engine", True))
    except Exception as e:
        components.append(("PnL Engine", False))
        
    try:
        from core.position_registry import PositionRegistry
        components.append(("Position Registry", True))
    except Exception as e:
        components.append(("Position Registry", False))
        
    try:
        from execution.order_router import OrderRouter
        components.append(("Order Router", True))
    except Exception as e:
        components.append(("Order Router", False))
        
    try:
        from apps.dashboard.dashboard_v2 import DashboardV2
        components.append(("Dashboard", True))
    except Exception as e:
        components.append(("Dashboard", False))
        
    try:
        from logic.strategies.orchestrator import StrategyOrchestrator
        components.append(("Strategy Orchestrator", True))
    except Exception as e:
        components.append(("Strategy Orchestrator", False))
    
    for name, passed in components:
        print(f"  {check_mark(passed)} {name}")
        if not passed:
            all_passed = False
    
    # ============================================================
    # 3. DASHBOARD RENDERING
    # ============================================================
    print_header("3. DASHBOARD RENDERING")
    
    try:
        from apps.dashboard.dashboard_v2 import DashboardV2
        dashboard = DashboardV2()
        dashboard.state.mode = "paper"
        dashboard.state.warm_symbols = 50
        dashboard.state.cold_symbols = 10
        dashboard.state.startup_time = datetime.now(timezone.utc)
        
        panels = [
            ("Top Bar", dashboard.render_top_bar),
            ("Signal Panel", dashboard.render_signal_panel),
            ("Focus Panel", dashboard.render_focus_panel),
            ("Sanity Panel", dashboard.render_sanity_panel),
            ("Full Dashboard", dashboard.render_full),
        ]
        
        for name, render_func in panels:
            try:
                render_func()
                print(f"  {check_mark(True)} {name}")
            except Exception as e:
                print(f"  {check_mark(False)} {name}: {e}")
                all_passed = False
                
    except Exception as e:
        print(f"  ❌ Dashboard Error: {e}")
        all_passed = False
    
    # ============================================================
    # 4. ORDER ROUTER INTEGRATION
    # ============================================================
    print_header("4. ORDER ROUTER INTEGRATION")
    
    try:
        from execution.order_router import OrderRouter
        
        def mock_price(symbol: str) -> float:
            return 100.0
        
        router = OrderRouter(get_price_func=mock_price)
        
        integrations = [
            ("PnL Engine", hasattr(router, 'pnl_engine') and router.pnl_engine),
            ("Position Registry", hasattr(router, 'position_registry') and router.position_registry),
            ("Truth Sync", hasattr(router, '_verify_exchange_truth')),
            ("Pre-Trade Validation", hasattr(router, '_validate_before_trade')),
            ("Recovery System", hasattr(router, '_recover_from_drift')),
        ]
        
        for name, passed in integrations:
            print(f"  {check_mark(passed)} {name}")
            if not passed:
                all_passed = False
                
    except Exception as e:
        print(f"  ❌ Order Router Error: {e}")
        all_passed = False
    
    # ============================================================
    # 5. EXCHANGE CONNECTIVITY (if live mode)
    # ============================================================
    if settings.trading_mode == "live" and settings.coinbase_api_key:
        print_header("5. EXCHANGE CONNECTIVITY")
        
        try:
            from core.portfolio import PortfolioTracker
            tracker = PortfolioTracker()
            snapshot = tracker.get_snapshot()
            
            if snapshot:
                print(f"  {check_mark(True)} Exchange Connected")
                print(f"  {check_mark(True)} Portfolio Value: ${snapshot.total_value:.2f}")
                print(f"  {check_mark(True)} Positions: {len([p for p in snapshot.positions.values() if not p.is_cash])}")
            else:
                print(f"  {check_mark(False)} Could not get portfolio snapshot")
                all_passed = False
                
        except Exception as e:
            print(f"  ❌ Exchange Error: {e}")
            all_passed = False
    else:
        print_header("5. EXCHANGE CONNECTIVITY")
        print("  ⏭️ Skipped (paper mode or no API keys)")
    
    # ============================================================
    # FINAL SUMMARY
    # ============================================================
    print_header("HEALTH CHECK SUMMARY")
    
    if all_passed:
        print("  🎉 ALL CHECKS PASSED")
        print("  ✅ System is healthy and ready for trading")
        print(f"\n  Start with: python run_v2.py --{settings.trading_mode}")
    else:
        print("  ⚠️ SOME CHECKS FAILED")
        print("  🔧 Review errors above before trading")
    
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(run_health_check())
    sys.exit(0 if success else 1)
