#!/usr/bin/env python3
"""Quick tool to check real positions on Coinbase vs local tracking."""

import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import settings

# Delisted/unsupported coins to skip
DELISTED_COINS = {'BOND', 'NU', 'CLV', 'SNX'}
DUST_THRESHOLD = 0.50  # Skip positions below this value

def main():
    print("=" * 70)
    print("           COINBASE vs LOCAL POSITION TRACKER")
    print("=" * 70)
    
    # Load local positions
    pos_file = Path("data/live_positions.json")
    local = {}
    if pos_file.exists():
        with open(pos_file) as f:
            local = json.load(f)
    
    # Check Coinbase
    if not settings.coinbase_api_key:
        print("No API keys configured")
        return
    
    try:
        from coinbase.rest import RESTClient
        client = RESTClient(
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret
        )
        
        # Get portfolio breakdown for REAL entry prices
        portfolios = client.get_portfolios()
        portfolio_list = getattr(portfolios, 'portfolios', [])
        
        portfolio_uuid = None
        for p in portfolio_list:
            ptype = getattr(p, 'type', '') or p.get('type', '')
            if ptype == 'DEFAULT':
                portfolio_uuid = getattr(p, 'uuid', None) or p.get('uuid')
                break
        
        if not portfolio_uuid and portfolio_list:
            portfolio_uuid = getattr(portfolio_list[0], 'uuid', None) or portfolio_list[0].get('uuid')
        
        breakdown_resp = client.get_portfolio_breakdown(portfolio_uuid)
        breakdown = getattr(breakdown_resp, 'breakdown', breakdown_resp)
        spot_positions = getattr(breakdown, 'spot_positions', [])
        
        # Categorize positions
        active_positions = []
        dust_positions = []
        skipped_positions = []
        
        for pos in spot_positions:
            if isinstance(pos, dict):
                asset = pos.get('asset', '')
                qty = float(pos.get('total_balance_crypto', 0) or 0)
                value = float(pos.get('total_balance_fiat', 0) or 0)
                entry_obj = pos.get('average_entry_price', {})
                pnl = float(pos.get('unrealized_pnl', 0) or 0)
                is_cash = pos.get('is_cash', False)
            else:
                asset = getattr(pos, 'asset', '')
                qty = float(getattr(pos, 'total_balance_crypto', 0) or 0)
                value = float(getattr(pos, 'total_balance_fiat', 0) or 0)
                entry_obj = getattr(pos, 'average_entry_price', {})
                pnl = float(getattr(pos, 'unrealized_pnl', 0) or 0)
                is_cash = getattr(pos, 'is_cash', False)
            
            if is_cash or asset in ['USD', 'USDC']:
                continue
            
            entry = float(entry_obj.get('value', 0) if isinstance(entry_obj, dict) else getattr(entry_obj, 'value', 0))
            symbol = f"{asset}-USD"
            
            pos_data = {
                'asset': asset,
                'symbol': symbol,
                'qty': qty,
                'value': value,
                'entry': entry,
                'pnl': pnl,
            }
            
            if asset in DELISTED_COINS:
                skipped_positions.append(pos_data)
            elif value < DUST_THRESHOLD:
                dust_positions.append(pos_data)
            else:
                active_positions.append(pos_data)
        
        # Print active positions
        print(f"\n📊 ACTIVE POSITIONS ({len(active_positions)} tracked by bot)")
        print("-" * 70)
        print(f"{'Symbol':<10} {'Value':>10} {'Entry':>12} {'Qty':>14} {'P&L':>10} {'Status'}")
        print("-" * 70)
        
        total_value = 0
        total_pnl = 0
        issues = []
        
        for p in sorted(active_positions, key=lambda x: -x['value']):
            symbol = p['symbol']
            local_data = local.get(symbol, {})
            local_entry = local_data.get('entry_price', 0)
            local_qty = local_data.get('size_qty', 0)
            
            # Check for mismatches
            status_parts = []
            # Flag if entry is wrong OR if it's zero/missing
            if local_entry == 0:
                status_parts.append(f"entry $0→${p['entry']:.2f}")
                issues.append(f"{p['asset']}: entry $0.00 should be ${p['entry']:.4f}")
            elif abs(local_entry - p['entry']) / max(p['entry'], 0.01) > 0.1:
                status_parts.append(f"entry ${local_entry:.2f}→${p['entry']:.2f}")
                issues.append(f"{p['asset']}: entry ${local_entry:.2f} should be ${p['entry']:.4f}")
            if local_qty > 0 and abs(local_qty - p['qty']) / max(p['qty'], 0.01) > 0.1:
                status_parts.append(f"qty {local_qty:.2f}→{p['qty']:.2f}")
                issues.append(f"{p['asset']}: qty {local_qty:.2f} should be {p['qty']:.4f}")
            if symbol not in local:
                status_parts.append("NOT TRACKED")
                issues.append(f"{p['asset']}: Not in local tracking!")
            
            status = "🟢" if p['pnl'] > 0 else "🔴"
            fix_needed = " ⚠️ " + ", ".join(status_parts) if status_parts else " ✓"
            
            print(f"{p['asset']:<10} ${p['value']:>9.2f} ${p['entry']:>11.4f} {p['qty']:>14.6f} ${p['pnl']:>+8.2f} {status}{fix_needed}")
            total_value += p['value']
            total_pnl += p['pnl']
        
        print("-" * 70)
        print(f"{'TOTAL':<10} ${total_value:>9.2f} {'':>12} {'':>14} ${total_pnl:>+8.2f}")
        
        # Print issues summary
        if issues:
            print(f"\n⚠️  SYNC ISSUES ({len(issues)} to fix on restart)")
            print("-" * 70)
            for issue in issues:
                print(f"  • {issue}")
            print("\n💡 These will be FIXED automatically when bot restarts and syncs with Coinbase.")
        else:
            print("\n✅ All positions correctly synced!")
        
        # Print dust/skipped
        if dust_positions or skipped_positions:
            print(f"\n📦 IGNORED POSITIONS (dust < ${DUST_THRESHOLD} or delisted)")
            print("-" * 70)
            for p in dust_positions + skipped_positions:
                reason = "DELISTED" if p['asset'] in DELISTED_COINS else "DUST"
                print(f"  {p['asset']:<8}: ${p['value']:.2f} ({reason})")
        
        # Expected stops/TPs after sync
        print(f"\n📐 EXPECTED STOPS/TPs AFTER SYNC (2.5% stop, 4% TP1, 7% TP2)")
        print("-" * 70)
        print(f"{'Symbol':<10} {'Entry':>10} {'Stop':>10} {'TP1':>10} {'TP2':>10}")
        print("-" * 70)
        for p in sorted(active_positions, key=lambda x: -x['value']):
            entry = p['entry']
            stop = entry * 0.975
            tp1 = entry * 1.04
            tp2 = entry * 1.07
            print(f"{p['asset']:<10} ${entry:>9.4f} ${stop:>9.4f} ${tp1:>9.4f} ${tp2:>9.4f}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
