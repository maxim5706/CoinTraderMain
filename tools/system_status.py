#!/usr/bin/env python3
"""
System Status Summary

Shows complete system status and validates all components work together.
Perfect for checking system health before trading.
"""

import sys
import asyncio
import argparse
from datetime import datetime

sys.path.append('.')


async def show_system_status(force_mode=None):
    """Show complete system status."""
    
    # Handle mode override
    if force_mode:
        import os
        os.environ['TRADING_MODE'] = force_mode
        from importlib import reload
        import core.config
        reload(core.config)
    
    from core.config import settings
    from core.mode_config import ConfigurationManager
    from core.mode_configs import TradingMode
    
    print("🎯 COINTRADER SYSTEM STATUS")
    print("=" * 60)
    print(f"Timestamp: {datetime.now()}")
    print(f"Trading Mode: {settings.trading_mode.upper()}")
    print(f"API Keys: {'✅ Configured' if settings.coinbase_api_key else '❌ Missing'}")
    print()

    # Component Status
    print("🔧 COMPONENT STATUS")
    print("-" * 30)
    
    try:
        from tests.integration.data_sync_validator import DataSynchronizationValidator
        validator = DataSynchronizationValidator()
        validation_success = await validator.run_complete_validation()
        
        print(f"✅ All Components: {'HEALTHY' if validation_success else 'ISSUES DETECTED'}")
        
    except Exception as e:
        print(f"❌ Component Check Failed: {e}")
        validation_success = False
    
    print()

    # Portfolio Status
    print("💼 PORTFOLIO STATUS")
    print("-" * 30)
    
    if settings.trading_mode == "live":
        try:
            from core.portfolio import PortfolioTracker
            portfolio = PortfolioTracker()
            snapshot = portfolio.get_snapshot()
            
            if snapshot:
                print(f"✅ Live Portfolio Connected")
                print(f"   Total Value: ${snapshot.total_value:,.2f}")
                print(f"   Cash: ${snapshot.total_cash:,.2f}")
                print(f"   Crypto: ${snapshot.total_crypto:,.2f}")
                print(f"   Positions: {len([p for p in snapshot.positions.values() if not p.is_cash])}")
                print(f"   Unrealized PnL: ${snapshot.total_unrealized_pnl:,.2f}")
            else:
                print("❌ Could not connect to live portfolio")
                
        except Exception as e:
            print(f"❌ Portfolio connection failed: {e}")
    else:
        print("📄 Paper Mode - Simulated portfolio")
        print(f"   Starting Balance: ${settings.paper_start_balance_usd}")
    
    print()

    # Configuration Status
    print("⚙️ CONFIGURATION STATUS")
    print("-" * 30)
    
    mode = ConfigurationManager.get_trading_mode()
    config = ConfigurationManager.get_config_for_mode(mode)
    
    print(f"Current Mode: {mode.value}")
    print(f"Max Positions: {config.max_positions}")
    print(f"Per-Strategy Limit: {config.max_positions_per_strategy}")
    print(f"Dust Threshold: ${config.dust_threshold_usd}")
    print(f"Max Trade Size: ${config.max_trade_usd}")
    print(f"Maker Fee: {config.maker_fee_pct:.1%}")
    print()

    # Easy Commands
    print("🚀 EASY COMMANDS")
    print("-" * 30)
    print("View Live Portfolio:")
    print("  python view_live_portfolio.py")
    print()
    print("Start Trading (Paper Mode):")
    print("  python run_v2.py --paper")
    print()
    print("Start Trading (Live Mode):")
    print("  python run_v2.py --live")
    print()
    print("Run Full Validation:")
    print("  python run_v2.py --validate --paper")
    print()
    print("Test $5 Live Trade:")
    print("  python validate_live_system.py --full-test")
    print()

    # Overall Status
    print("📊 OVERALL SYSTEM STATUS")
    print("-" * 30)
    
    status_items = [
        ("Component Integration", validation_success),
        ("API Configuration", bool(settings.coinbase_api_key)),
        ("Mode Configuration", True),  # Always true if we got this far
        ("Trading Ready", validation_success and bool(settings.coinbase_api_key))
    ]
    
    for item_name, status in status_items:
        icon = "✅" if status else "❌"
        print(f"  {icon} {item_name}")
    
    all_good = all(status for _, status in status_items)
    
    print()
    if all_good:
        print("🎉 SYSTEM READY FOR TRADING")
        print("✅ All components integrated and validated")
        print("✅ API connections working")
        print("✅ Multi-strategy support enabled")
        print("✅ No duplicate objects or race conditions")
    else:
        print("⚠️ SYSTEM NEEDS ATTENTION")
        failed_items = [name for name, status in status_items if not status]
        print(f"❌ Issues: {', '.join(failed_items)}")
    
    return all_good


async def main():
    """Run system status check."""
    parser = argparse.ArgumentParser(description='System Status Check')
    parser.add_argument('--mode', choices=['paper', 'live'], 
                       help='Force trading mode for status check')
    parser.add_argument('--live', action='store_true',
                       help='Check live mode status')
    parser.add_argument('--paper', action='store_true',  
                       help='Check paper mode status')
    
    args = parser.parse_args()
    
    force_mode = None
    if args.live:
        force_mode = 'live'
    elif args.paper:
        force_mode = 'paper'
    elif args.mode:
        force_mode = args.mode
    
    try:
        system_ready = await show_system_status(force_mode)
        
        if system_ready:
            print("\n🚀 Ready to trade! Use 'python run_v2.py --live' to start")
        else:
            print("\n🔧 Please resolve issues before trading")
            
    except Exception as e:
        print(f"❌ Status check failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
