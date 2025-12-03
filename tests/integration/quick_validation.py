"""
Quick Validation Test - Safe Live API Testing

This runs basic validation without any purchases:
1. API connectivity 
2. Portfolio sync accuracy
3. Price feed reliability
4. PnL calculation correctness 
5. Configuration validation

NO TRADING - Pure read-only validation first.
"""

import asyncio
import logging
from datetime import datetime, timezone

from core.config import settings
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.pnl_engine import PnLEngine
from core.position_registry import PositionRegistry
from core.portfolio import Portfolio


async def validate_api_connectivity():
    """Test basic API connectivity without trading."""
    print("📡 Testing API Connectivity...")
    
    try:
        portfolio = Portfolio()
        snapshot = portfolio.get_snapshot()
        
        if snapshot:
            print(f"✅ API Connected")
            print(f"   Portfolio Value: ${snapshot.total_value:.2f}")
            print(f"   Cash Balance: ${snapshot.total_cash:.2f}")
            print(f"   Crypto Holdings: ${snapshot.total_crypto:.2f}")
            print(f"   Positions: {len(snapshot.positions)}")
            return True
        else:
            print("❌ Failed to get portfolio snapshot")
            return False
            
    except Exception as e:
        print(f"❌ API Error: {e}")
        return False


async def validate_price_feeds():
    """Test price feed reliability."""
    print("\n💰 Testing Price Feeds...")
    
    try:
        from datafeeds.price_feeds import get_price_sync
        
        test_symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]
        prices = {}
        
        for symbol in test_symbols:
            price = get_price_sync(symbol)
            prices[symbol] = price
            
            if price and price > 0:
                print(f"✅ {symbol}: ${price:,.2f}")
            else:
                print(f"❌ {symbol}: Failed to get price")
                return False
        
        # Test price consistency (get same symbol twice)
        btc_price_2 = get_price_sync("BTC-USD")
        price_diff = abs(prices["BTC-USD"] - btc_price_2)
        price_diff_pct = (price_diff / prices["BTC-USD"]) * 100
        
        if price_diff_pct < 0.1:  # Less than 0.1% difference
            print(f"✅ Price feed consistency: {price_diff_pct:.3f}% difference")
        else:
            print(f"❌ Price feed inconsistency: {price_diff_pct:.3f}% difference")
            return False
            
        return True
        
    except Exception as e:
        print(f"❌ Price feed error: {e}")
        return False


async def validate_pnl_calculations():
    """Test PnL calculation accuracy with known values."""
    print("\n💸 Testing PnL Calculations...")
    
    try:
        config = ConfigurationManager.get_config_for_mode(TradingMode.LIVE)
        pnl_engine = PnLEngine(config)
        
        # Test case 1: Profitable trade
        pnl_1 = pnl_engine.calculate_trade_pnl(
            entry_price=50000.0,
            exit_price=51000.0,
            qty=0.001  # $50 position
        )
        
        expected_gross = 1.0  # $1 profit
        expected_net = expected_gross - (50.0 * 0.018)  # Subtract ~1.8% fees
        
        print(f"✅ Test Trade 1:")
        print(f"   Entry: $50,000 x 0.001 = $50 position")
        print(f"   Exit: $51,000 (+2% move)")
        print(f"   Gross PnL: ${pnl_1.gross_pnl:.4f} (expected ${expected_gross:.4f})")
        print(f"   Net PnL: ${pnl_1.net_pnl:.4f} (after ${pnl_1.total_fees:.4f} fees)")
        print(f"   Fee %: {pnl_1.fee_pct:.2f}%")
        
        # Validate calculation
        gross_correct = abs(pnl_1.gross_pnl - expected_gross) < 0.001
        fees_reasonable = 0.015 <= pnl_1.fee_pct/100 <= 0.025  # 1.5% - 2.5%
        
        if gross_correct and fees_reasonable:
            print("✅ PnL calculations accurate")
            return True
        else:
            print(f"❌ PnL calculation error - gross_correct: {gross_correct}, fees_reasonable: {fees_reasonable}")
            return False
            
    except Exception as e:
        print(f"❌ PnL calculation error: {e}")
        return False


async def validate_position_registry():
    """Test position registry functionality."""
    print("\n📊 Testing Position Registry...")
    
    try:
        config = ConfigurationManager.get_config_for_mode(TradingMode.LIVE)
        registry = PositionRegistry(config)
        
        # Test limits
        print(f"✅ Position Registry Config:")
        print(f"   Dust Threshold: ${registry.limits.dust_threshold_usd}")
        print(f"   Max Positions: {registry.limits.max_positions}")
        print(f"   Min Position: ${registry.limits.min_position_usd}")
        print(f"   Min Hold Time: {registry.limits.min_hold_seconds}s")
        
        # Test position capacity checks
        can_open, reason = registry.can_open_position("test_strategy", 5.0)
        print(f"   Can open $5 position: {can_open} ({reason})")
        
        return True
        
    except Exception as e:
        print(f"❌ Position registry error: {e}")
        return False


async def validate_configuration():
    """Test configuration system."""
    print("\n⚙️ Testing Configuration System...")
    
    try:
        # Test both modes
        paper_config = ConfigurationManager.get_config_for_mode(TradingMode.PAPER)
        live_config = ConfigurationManager.get_config_for_mode(TradingMode.LIVE)
        
        print("✅ Configuration Loaded:")
        print(f"   Paper dust threshold: ${paper_config.dust_threshold_usd}")
        print(f"   Live dust threshold: ${live_config.dust_threshold_usd}")
        print(f"   Paper max positions per strategy: {paper_config.max_positions_per_strategy}")
        print(f"   Live max positions per strategy: {live_config.max_positions_per_strategy}")
        
        # Validate differences
        config_different = (
            paper_config.dust_threshold_usd != live_config.dust_threshold_usd or
            paper_config.max_positions_per_strategy != live_config.max_positions_per_strategy
        )
        
        if config_different:
            print("✅ Mode-specific configuration working")
            return True
        else:
            print("❌ Mode configurations are identical (should be different)")
            return False
            
    except Exception as e:
        print(f"❌ Configuration error: {e}")
        return False


async def validate_portfolio_consistency():
    """Test portfolio data consistency over multiple calls."""
    print("\n🔄 Testing Portfolio Consistency...")
    
    try:
        portfolio = Portfolio()
        snapshots = []
        
        # Take 3 snapshots 1 second apart
        for i in range(3):
            snapshot = portfolio.get_snapshot()
            if snapshot:
                snapshots.append(snapshot)
            await asyncio.sleep(1)
        
        if len(snapshots) < 3:
            print("❌ Failed to get consistent snapshots")
            return False
        
        # Check value consistency
        values = [s.total_value for s in snapshots]
        max_diff = max(values) - min(values)
        max_diff_pct = (max_diff / values[0]) * 100 if values[0] > 0 else 0
        
        print(f"✅ Portfolio Consistency Test:")
        print(f"   Values: {[f'${v:.2f}' for v in values]}")
        print(f"   Max Difference: ${max_diff:.4f} ({max_diff_pct:.4f}%)")
        
        if max_diff_pct < 0.01:  # Less than 0.01% difference
            print("✅ Portfolio data consistent")
            return True
        else:
            print("❌ Portfolio data inconsistent")
            return False
            
    except Exception as e:
        print(f"❌ Portfolio consistency error: {e}")
        return False


async def run_quick_validation():
    """Run all validation tests."""
    print("🧪 QUICK VALIDATION TEST - READ ONLY")
    print("="*60)
    
    # Safety check
    if settings.trading_mode != "live":
        print("❌ Must be in live mode to test live APIs")
        return False
    
    if not settings.coinbase_api_key:
        print("❌ No API keys configured")
        return False
    
    print(f"🔑 Using API key: {settings.coinbase_api_key[:10]}...")
    print()
    
    tests = [
        ("API Connectivity", validate_api_connectivity),
        ("Price Feeds", validate_price_feeds), 
        ("PnL Calculations", validate_pnl_calculations),
        ("Position Registry", validate_position_registry),
        ("Configuration", validate_configuration),
        ("Portfolio Consistency", validate_portfolio_consistency),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        try:
            result = await test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ {test_name} failed with exception: {e}")
            results.append((test_name, False))
    
    # Summary
    print("\n" + "="*60)
    print("📋 VALIDATION SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{test_name:<20}: {status}")
    
    print(f"\nOverall: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 ALL TESTS PASSED - System ready for live validation!")
        print("\n📋 Next Steps:")
        print("1. Run full live validation with $5 test trade")
        print("2. Validate order placement and fills")
        print("3. Test position tracking with real data")
        return True
    else:
        print(f"\n⚠️ {total - passed} tests failed - Fix issues before live trading")
        return False


if __name__ == "__main__":
    asyncio.run(run_quick_validation())
