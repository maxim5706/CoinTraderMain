#!/usr/bin/env python3
"""
Pre-Live Trading Checklist
Run this before starting live trading to verify everything is ready.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import settings
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode


def check_mode():
    """Verify trading mode is set correctly."""
    mode = settings.trading_mode
    print(f"\n1️⃣  TRADING MODE: {mode.upper()}")
    if mode == "live":
        print("   ⚠️  WARNING: You are in LIVE mode - real money at risk!")
    else:
        print("   ✅ Paper mode - no real trades")
    return mode


def check_api():
    """Verify API connectivity."""
    print("\n2️⃣  API CONNECTIVITY:")
    if not settings.coinbase_api_key or not settings.coinbase_api_secret:
        print("   ❌ API keys not configured")
        return False
    
    try:
        from coinbase.rest import RESTClient
        client = RESTClient(
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret
        )
        accounts = client.get_accounts()
        count = len(getattr(accounts, 'accounts', []))
        print(f"   ✅ Authenticated - {count} accounts visible")
        return True
    except Exception as e:
        print(f"   ❌ API error: {e}")
        return False


def check_balance():
    """Check available balance."""
    print("\n3️⃣  BALANCE CHECK:")
    try:
        from coinbase.rest import RESTClient
        client = RESTClient(
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret
        )
        accounts = client.get_accounts()
        
        usd = usdc = 0.0
        for acct in getattr(accounts, 'accounts', []):
            currency = getattr(acct, 'currency', '')
            bal = getattr(acct, 'available_balance', {})
            value = float(bal.get('value', 0) if isinstance(bal, dict) else getattr(bal, 'value', 0))
            if currency == 'USD':
                usd = value
            elif currency == 'USDC':
                usdc = value
        
        total = usd + usdc
        print(f"   USD:  ${usd:.2f}")
        print(f"   USDC: ${usdc:.2f}")
        print(f"   Total: ${total:.2f}")
        
        if total < 50:
            print("   ⚠️  Low balance - recommend at least $50 for live trading")
        return total
    except Exception as e:
        print(f"   ❌ Balance check failed: {e}")
        return 0


def check_risk_settings():
    """Display risk settings."""
    print("\n4️⃣  RISK SETTINGS:")
    print(f"   Max trade size:    ${settings.max_trade_usd}")
    print(f"   Daily loss limit:  ${settings.daily_max_loss_usd}")
    print(f"   Max positions:     {settings.max_positions}")
    print(f"   Stop loss:         {settings.fixed_stop_pct * 100:.1f}%")
    print(f"   Take profit 1:     {settings.tp1_pct * 100:.1f}%")
    print(f"   Take profit 2:     {settings.tp2_pct * 100:.1f}%")
    print(f"   Min R:R ratio:     {settings.min_rr_ratio}")


def check_positions():
    """Check for existing positions."""
    print("\n5️⃣  EXISTING POSITIONS:")
    
    live_file = Path("data/live_positions.json")
    paper_file = Path("data/paper_positions.json")
    
    for name, path in [("Live", live_file), ("Paper", paper_file)]:
        if path.exists():
            import json
            with open(path) as f:
                positions = json.load(f)
            if positions:
                print(f"   {name}: {len(positions)} positions")
                for sym in list(positions.keys())[:5]:
                    p = positions[sym]
                    print(f"      - {sym}: ${p.get('size_usd', 0):.2f}")
            else:
                print(f"   {name}: No positions")
        else:
            print(f"   {name}: No position file")


def check_circuit_breaker():
    """Verify circuit breaker settings."""
    print("\n6️⃣  CIRCUIT BREAKER:")
    print(f"   Max consecutive failures: {settings.circuit_breaker_max_failures}")
    print(f"   Reset after: {settings.circuit_breaker_reset_seconds}s")


def main():
    print("=" * 50)
    print("     PRE-LIVE TRADING CHECKLIST")
    print("=" * 50)
    
    mode = check_mode()
    api_ok = check_api()
    balance = check_balance()
    check_risk_settings()
    check_positions()
    check_circuit_breaker()
    
    print("\n" + "=" * 50)
    print("     SUMMARY")
    print("=" * 50)
    
    issues = []
    if not api_ok:
        issues.append("API not connected")
    if balance < 50:
        issues.append("Low balance")
    
    if issues:
        print(f"\n⚠️  Issues found: {', '.join(issues)}")
        print("   Fix these before going live!")
    else:
        print("\n✅ All checks passed!")
        if mode == "live":
            print("\n🚀 Ready for LIVE trading")
            print("   Start with: python run_v2.py")
        else:
            print("\n📝 Currently in PAPER mode")
            print("   To go live, set TRADING_MODE=live in .env")


if __name__ == "__main__":
    main()
