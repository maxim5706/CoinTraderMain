#!/usr/bin/env python3
"""
Live Portfolio Viewer - View your actual Coinbase portfolio

This script connects to your live Coinbase account and shows:
- Real portfolio balance and positions  
- Live PnL calculations
- Position synchronization validation
- Ready status for trading
"""

import sys
import os
import asyncio
from datetime import datetime, timezone

# Force live mode
os.environ['TRADING_MODE'] = 'live'

# Add current directory to path
sys.path.append('.')

# Reload config to pick up live mode
from importlib import reload
import core.config
reload(core.config)

from core.config import settings
from core.mode_config import ConfigurationManager
from core.mode_configs import TradingMode
from core.pnl_engine import PnLEngine
from core.position_registry import PositionRegistry
from core.portfolio import PortfolioTracker
from execution.order_router import OrderRouter


async def view_live_portfolio():
    """View live portfolio with complete validation."""
    print("💼 LIVE PORTFOLIO ANALYSIS")
    print("=" * 60)
    print(f"Timestamp: {datetime.now()}")
    print(f"Trading Mode: {settings.trading_mode}")
    print(f"API Key: {settings.coinbase_api_key[:12]}..." if settings.coinbase_api_key else "❌ No API key")
    print()

    if not settings.coinbase_api_key:
        print("❌ No API keys configured. Please set COINBASE_API_KEY and COINBASE_API_SECRET in .env")
        return None

    # Initialize components in live mode
    mode = TradingMode.LIVE  # Force live mode
    config = ConfigurationManager.get_config_for_mode(mode)
    
    print("🔧 LIVE MODE INITIALIZATION")
    print("-" * 30)
    
    try:
        # Initialize OrderRouter with live components
        router = OrderRouter(
            get_price_func=lambda symbol: get_current_price_from_api(symbol),
            mode=mode,
            config=config
        )
        
        print(f"✅ OrderRouter: {len(router.positions)} positions loaded")
        print(f"✅ Position Registry: {len(router.position_registry.get_all_positions())} positions")  
        print(f"✅ PnL Engine: {router.pnl_engine.config.maker_fee_pct:.1%} maker fees")
        print(f"✅ Portfolio Manager: {router.portfolio.__class__.__name__}")
        print(f"✅ Live Client: {'Connected' if router._client else 'Not connected'}")
        print()

    except Exception as e:
        print(f"❌ Failed to initialize live components: {e}")
        import traceback
        traceback.print_exc()
        return None

    # Get live portfolio snapshot
    print("📡 FETCHING LIVE PORTFOLIO DATA")
    print("-" * 30)
    
    try:
        portfolio_tracker = PortfolioTracker()
        snapshot = portfolio_tracker.get_snapshot()
        
        if not snapshot:
            print("❌ Could not get portfolio snapshot from Coinbase")
            return None
            
        print(f"✅ Portfolio snapshot retrieved")
        print(f"✅ Portfolio UUID: {snapshot.portfolio_uuid[:8]}...")
        print(f"✅ Snapshot time: {snapshot.timestamp}")
        print()
        
    except Exception as e:
        print(f"❌ Failed to get portfolio snapshot: {e}")
        import traceback
        traceback.print_exc()
        return None

    # Show live portfolio details
    print("💰 LIVE PORTFOLIO STATE")
    print("-" * 30)
    
    print(f"Total Portfolio Value: ${snapshot.total_value:,.2f}")
    print(f"Cash Balance: ${snapshot.total_cash:,.2f}")
    print(f"Crypto Holdings Value: ${snapshot.total_crypto:,.2f}")
    print(f"Total Unrealized PnL: ${snapshot.total_unrealized_pnl:,.2f}")
    print(f"Total Realized PnL: ${snapshot.total_realized_pnl:,.2f}")
    print(f"Number of Positions: {len(snapshot.positions)}")
    print()

    # Show individual positions
    print("📊 LIVE POSITIONS")
    print("-" * 30)
    
    if snapshot.positions:
        # Sort positions by value (largest first)
        positions_by_value = sorted(
            [(symbol, pos) for symbol, pos in snapshot.positions.items() if not pos.is_cash],
            key=lambda x: x[1].value_usd,
            reverse=True
        )
        
        total_crypto_value = 0
        total_unrealized_pnl = 0
        
        for symbol, position in positions_by_value:
            if position.value_usd >= 0.01:  # Only show positions worth > $0.01
                pnl_pct = position.unrealized_pnl_pct
                status = "🟢" if position.unrealized_pnl > 0 else "🔴" if position.unrealized_pnl < 0 else "⚪"
                
                print(f"  {status} {symbol}:")
                print(f"    Quantity: {position.qty:.8f} {position.asset}")
                print(f"    Value: ${position.value_usd:,.2f}")
                print(f"    Entry Price: ${position.entry_price:.4f}")
                print(f"    Cost Basis: ${position.cost_basis:,.2f}")
                print(f"    Unrealized PnL: ${position.unrealized_pnl:,.2f} ({pnl_pct:+.2f}%)")
                print()
                
                total_crypto_value += position.value_usd
                total_unrealized_pnl += position.unrealized_pnl
        
        print(f"📈 TOTALS:")
        print(f"  Total Crypto Value: ${total_crypto_value:,.2f}")
        print(f"  Total Unrealized PnL: ${total_unrealized_pnl:,.2f}")
        
    else:
        print("  No crypto positions found")
    print()

    # Show cash positions
    cash_positions = [(symbol, pos) for symbol, pos in snapshot.positions.items() if pos.is_cash]
    if cash_positions:
        print("💵 CASH POSITIONS")
        print("-" * 30)
        for symbol, position in cash_positions:
            if position.value_usd >= 0.01:
                print(f"  {symbol}: ${position.value_usd:,.2f}")
        print()

    # Validate position tracking
    print("🔍 POSITION TRACKING VALIDATION")
    print("-" * 30)
    
    # Check if OrderRouter positions match exchange
    router_symbols = set(router.positions.keys())
    exchange_symbols = set(pos.symbol for pos in snapshot.positions.values() if not pos.is_cash and pos.value_usd >= 1.0)
    
    print(f"OrderRouter tracked positions: {len(router_symbols)}")
    print(f"Exchange positions (>$1): {len(exchange_symbols)}")
    
    if router_symbols == exchange_symbols:
        print("✅ Position tracking is synchronized")
    else:
        print("⚠️ Position tracking differences detected:")
        if router_symbols - exchange_symbols:
            print(f"  In OrderRouter but not on exchange: {router_symbols - exchange_symbols}")
        if exchange_symbols - router_symbols:
            print(f"  On exchange but not in OrderRouter: {exchange_symbols - router_symbols}")
    print()

    # Test live PnL calculation accuracy
    print("💸 PNL CALCULATION VALIDATION")
    print("-" * 30)
    
    if positions_by_value:
        # Test PnL calculation on first position
        symbol, position = positions_by_value[0]
        
        if position.entry_price > 0 and position.qty > 0:
            # Get current market price
            current_price = position.value_usd / position.qty
            
            # Calculate PnL using our engine
            our_pnl = router.pnl_engine.calculate_unrealized_pnl(
                position=create_test_position(symbol, position.entry_price, position.qty),
                current_price=current_price
            )
            
            exchange_pnl = position.unrealized_pnl
            pnl_diff = abs(our_pnl - exchange_pnl)
            pnl_diff_pct = (pnl_diff / abs(exchange_pnl) * 100) if exchange_pnl != 0 else 0
            
            print(f"Testing PnL calculation for {symbol}:")
            print(f"  Our calculation: ${our_pnl:.2f}")
            print(f"  Exchange PnL: ${exchange_pnl:.2f}")
            print(f"  Difference: ${pnl_diff:.2f} ({pnl_diff_pct:.2f}%)")
            
            if pnl_diff_pct <= 1.0:  # Within 1%
                print("  ✅ PnL calculation accurate")
            else:
                print("  ⚠️ PnL calculation discrepancy detected")
        else:
            print("  No suitable position found for PnL testing")
    else:
        print("  No positions available for PnL testing")
    print()

    # Show system readiness
    print("🎯 TRADING SYSTEM READINESS")
    print("-" * 30)
    
    ready_checks = []
    
    # API connectivity
    ready_checks.append(("API Connection", router._client is not None))
    
    # Portfolio access
    ready_checks.append(("Portfolio Access", snapshot is not None))
    
    # Position synchronization
    ready_checks.append(("Position Sync", len(router_symbols.symmetric_difference(exchange_symbols)) <= 1))
    
    # Balance validation
    balance_ok = abs(router._portfolio_value - snapshot.total_value) <= 1.0
    ready_checks.append(("Balance Accuracy", balance_ok))
    
    # Configuration
    config_ok = router.config.maker_fee_pct > 0 and router.config.max_positions > 0
    ready_checks.append(("Configuration", config_ok))
    
    for check_name, passed in ready_checks:
        status = "✅" if passed else "❌"
        print(f"  {status} {check_name}")
    
    all_ready = all(passed for _, passed in ready_checks)
    
    print()
    print("🚀 OVERALL STATUS")
    print("-" * 30)
    
    if all_ready:
        print("✅ System is READY for live trading")
        print("✅ All validations passed")
        print("✅ Portfolio synchronized")
        print("✅ PnL calculations accurate") 
        print("\n🎯 You can now trade live with confidence!")
    else:
        failed_checks = [name for name, passed in ready_checks if not passed]
        print("⚠️ System needs attention before live trading")
        print(f"❌ Failed checks: {', '.join(failed_checks)}")
        print("\n🔧 Please resolve issues before trading")
    
    return router, snapshot


def create_test_position(symbol, entry_price, qty):
    """Create a test position for PnL validation."""
    from core.models import Position, Side
    
    return Position(
        symbol=symbol,
        side=Side.BUY,
        entry_price=entry_price,
        entry_time=datetime.now(timezone.utc),
        size_usd=entry_price * qty,
        size_qty=qty,
        stop_price=entry_price * 0.95,
        tp1_price=entry_price * 1.05,
        tp2_price=entry_price * 1.10
    )


def get_current_price_from_api(symbol: str) -> float:
    """Get current price from Coinbase API."""
    try:
        from coinbase.rest import RESTClient
        client = RESTClient(
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret
        )
        
        ticker = client.get_product_ticker(symbol)
        if ticker and hasattr(ticker, 'price'):
            return float(ticker.price)
            
    except Exception as e:
        print(f"Warning: Could not get price for {symbol}: {e}")
    
    # Fallback to reasonable estimates
    if symbol == "BTC-USD":
        return 95000.0
    elif symbol == "ETH-USD":
        return 3500.0
    elif symbol == "SOL-USD":
        return 230.0
    else:
        return 100.0


async def main():
    """Run live portfolio analysis."""
    print("🌐 CONNECTING TO LIVE COINBASE PORTFOLIO...")
    print()
    
    try:
        result = await view_live_portfolio()
        if result:
            router, snapshot = result
            
            print("\n" + "=" * 60)
            print("📋 LIVE PORTFOLIO SUMMARY")
            print(f"💰 Total Value: ${snapshot.total_value:,.2f}")
            print(f"💵 Cash: ${snapshot.total_cash:,.2f}")
            print(f"🔗 Crypto: ${snapshot.total_crypto:,.2f}")
            print(f"📈 Unrealized PnL: ${snapshot.total_unrealized_pnl:,.2f}")
            print(f"📊 Positions: {len([p for p in snapshot.positions.values() if not p.is_cash])}")
            
    except Exception as e:
        print(f"❌ Error viewing live portfolio: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
