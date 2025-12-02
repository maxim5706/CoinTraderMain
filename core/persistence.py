"""Position persistence - save/load positions across restarts.

Paper and Live modes use SEPARATE position files to avoid interference:
- Paper: data/paper_positions.json
- Live:  data/live_positions.json
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from core.models import Position, Side, PositionState
from core.config import settings


def _get_positions_file() -> Path:
    """Get the positions file based on current trading mode."""
    if settings.is_paper:
        return Path("data/paper_positions.json")
    else:
        return Path("data/live_positions.json")


def ensure_data_dir():
    """Ensure data directory exists."""
    Path("data").mkdir(parents=True, exist_ok=True)


def save_positions(positions: dict[str, Position]):
    """Save positions to disk."""
    ensure_data_dir()
    
    data = {}
    for symbol, pos in positions.items():
        data[symbol] = {
            "symbol": pos.symbol,
            "side": pos.side.value,
            "entry_price": pos.entry_price,
            "entry_time": pos.entry_time.isoformat(),
            "size_usd": pos.size_usd,
            "size_qty": pos.size_qty,
            "stop_price": pos.stop_price,
            "tp1_price": pos.tp1_price,
            "tp2_price": pos.tp2_price,
            "time_stop_min": pos.time_stop_min,
            "state": pos.state.value,
            "realized_pnl": pos.realized_pnl,
            "partial_closed": pos.partial_closed,
            # Play-based confidence tracking
            "entry_confidence": getattr(pos, 'entry_confidence', 0.0),
            "current_confidence": getattr(pos, 'current_confidence', 0.0),
            "peak_confidence": getattr(pos, 'peak_confidence', 0.0),
            "ml_score_entry": getattr(pos, 'ml_score_entry', 0.0),
            "ml_score_current": getattr(pos, 'ml_score_current', 0.0),
        }
    
    positions_file = _get_positions_file()
    with open(positions_file, 'w') as f:
        json.dump(data, f, indent=2)
    
    mode = "PAPER" if settings.is_paper else "LIVE"
    print(f"[PERSIST] Saved {len(positions)} positions ({mode})")


def load_positions() -> dict[str, Position]:
    """Load positions from disk (mode-specific file)."""
    ensure_data_dir()
    
    positions_file = _get_positions_file()
    mode = "PAPER" if settings.is_paper else "LIVE"
    
    if not positions_file.exists():
        print(f"[PERSIST] No {mode} positions file found, starting fresh")
        return {}
    
    try:
        with open(positions_file, 'r') as f:
            data = json.load(f)
        
        positions = {}
        for symbol, pos_data in data.items():
            entry_time = datetime.fromisoformat(pos_data["entry_time"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            
            positions[symbol] = Position(
                symbol=pos_data["symbol"],
                side=Side(pos_data["side"]),
                entry_price=pos_data["entry_price"],
                entry_time=entry_time,
                size_usd=pos_data["size_usd"],
                size_qty=pos_data["size_qty"],
                stop_price=pos_data["stop_price"],
                tp1_price=pos_data["tp1_price"],
                tp2_price=pos_data["tp2_price"],
                time_stop_min=pos_data.get("time_stop_min", 30),
                state=PositionState(pos_data["state"]),
                realized_pnl=pos_data.get("realized_pnl", 0.0),
                partial_closed=pos_data.get("partial_closed", False),
                # Play-based confidence (defaults for old positions)
                entry_confidence=pos_data.get("entry_confidence", 70.0),
                current_confidence=pos_data.get("current_confidence", 70.0),
                peak_confidence=pos_data.get("peak_confidence", 70.0),
                ml_score_entry=pos_data.get("ml_score_entry", 0.0),
                ml_score_current=pos_data.get("ml_score_current", 0.0),
            )
        
        print(f"[PERSIST] Loaded {len(positions)} {mode} positions")
        return positions
    except Exception as e:
        print(f"[PERSIST] Failed to load {mode} positions: {e}")
        return {}


def clear_position(symbol: str):
    """Remove a single position from persistence."""
    positions_file = _get_positions_file()
    
    if not positions_file.exists():
        return
    
    try:
        with open(positions_file, 'r') as f:
            data = json.load(f)
        
        if symbol in data:
            del data[symbol]
            with open(positions_file, 'w') as f:
                json.dump(data, f, indent=2)
    except Exception:
        pass


def sync_with_exchange(client, positions: dict[str, Position], quiet: bool = True) -> dict[str, Position]:
    """
    Sync local positions with actual exchange holdings using Portfolio API.
    Uses real entry prices from Coinbase, not guessed from fills.
    Returns reconciled positions dict.
    
    Args:
        quiet: If True, only print when changes are detected (for periodic sync)
    
    NOTE: Only runs in LIVE mode. Paper mode uses its own separate position tracking.
    """
    # Skip sync in paper mode - paper positions are independent
    if settings.is_paper:
        print("[SYNC] Paper mode - skipping exchange sync (using paper_positions.json)")
        return positions
    
    if client is None:
        print("[SYNC] No client, skipping exchange sync")
        return positions
    
    try:
        # Use Portfolio Breakdown API for REAL entry prices
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
        
        if not portfolio_uuid:
            print("[SYNC] Could not get portfolio UUID")
            return positions
        
        breakdown_resp = client.get_portfolio_breakdown(portfolio_uuid)
        breakdown = getattr(breakdown_resp, 'breakdown', breakdown_resp)
        spot_positions = getattr(breakdown, 'spot_positions', [])
        
        if not quiet:
            print(f"[SYNC] Portfolio has {len(spot_positions)} spot positions")
        
        # Track real positions from exchange
        real_holdings = {}
        
        # Parse portfolio positions and create/update local tracking
        for pos in spot_positions:
            # Handle both dict and object responses
            if isinstance(pos, dict):
                asset = pos.get('asset', '')
                qty = float(pos.get('total_balance_crypto', 0) or 0)
                value_usd = float(pos.get('total_balance_fiat', 0) or 0)
                entry_obj = pos.get('average_entry_price', {})
                cost_obj = pos.get('cost_basis', {})
                pnl_val = pos.get('unrealized_pnl', 0)
                is_cash = pos.get('is_cash', False)
            else:
                asset = getattr(pos, 'asset', '')
                qty = float(getattr(pos, 'total_balance_crypto', 0) or 0)
                value_usd = float(getattr(pos, 'total_balance_fiat', 0) or 0)
                entry_obj = getattr(pos, 'average_entry_price', {})
                cost_obj = getattr(pos, 'cost_basis', {})
                pnl_val = getattr(pos, 'unrealized_pnl', 0)
                is_cash = getattr(pos, 'is_cash', False)
            
            if not asset or is_cash or asset in ['USD', 'USDC']:
                continue
            
            symbol = f"{asset}-USD"
            
            # Skip delisted/unsupported coins (these return 404 on API calls)
            DELISTED_COINS = {'BOND-USD', 'NU-USD', 'CLV-USD', 'SNX-USD'}
            if symbol in DELISTED_COINS:
                continue
            
            # Get real entry price from Coinbase
            if isinstance(entry_obj, dict):
                entry_price = float(entry_obj.get('value', 0) or 0)
            else:
                entry_price = float(getattr(entry_obj, 'value', 0) or 0)
            
            if isinstance(cost_obj, dict):
                cost_basis = float(cost_obj.get('value', 0) or 0)
            else:
                cost_basis = float(getattr(cost_obj, 'value', 0) or 0)
            
            unrealized_pnl = float(pnl_val) if pnl_val else 0.0
            
            # Skip dust (lower threshold to catch small positions)
            if value_usd < 0.50:
                continue
            
            # If entry_price is 0, calculate from value/qty
            if entry_price == 0 and qty > 0:
                entry_price = value_usd / qty
            
            # Store for tracking
            real_holdings[symbol] = {
                'qty': qty,
                'value': value_usd,
                'entry': entry_price,
                'pnl': unrealized_pnl,
                'was_new': symbol not in positions  # Track if this is NEW
            }
            
            # Sync ALL positions - bot manages 100% of portfolio
            if symbol in positions:
                # Update existing position with real qty/value from exchange
                positions[symbol].size_qty = qty
                positions[symbol].size_usd = value_usd
                # Ensure entry_cost_usd is set (for budget calculations)
                if positions[symbol].entry_cost_usd == 0 and cost_basis > 0:
                    positions[symbol].entry_cost_usd = cost_basis
                # FIX: Also update entry_price if it looks wrong (placeholder bug)
                # A position with entry_price <= $5 for a coin worth $100+ is clearly wrong
                if entry_price > 0 and (positions[symbol].entry_price <= 5 or 
                    abs(positions[symbol].entry_price - entry_price) / entry_price > 0.5):
                    old_entry = positions[symbol].entry_price
                    positions[symbol].entry_price = entry_price
                    # Recalculate stops/TPs based on real entry
                    positions[symbol].stop_price = entry_price * 0.975  # 2.5% stop
                    positions[symbol].tp1_price = entry_price * 1.04    # 4% TP1
                    positions[symbol].tp2_price = entry_price * 1.07    # 7% TP2
                    if not quiet:
                        print(f"[SYNC] 🔧 Fixed {symbol} entry: ${old_entry:.2f} → ${entry_price:.2f}")
            else:
                # Position on exchange not in our tracking - add it
                # (happens after restart or if position was opened in previous session)
                # Use cost_basis from Coinbase for accurate budget tracking!
                positions[symbol] = Position(
                    symbol=symbol,
                    side=Side.BUY,
                    entry_price=entry_price,  # Real entry from Coinbase
                    entry_time=datetime.now(timezone.utc),
                    size_usd=value_usd,
                    size_qty=qty,
                    stop_price=entry_price * 0.98,  # 2% stop
                    tp1_price=entry_price * 1.045,  # 4.5% TP1
                    tp2_price=entry_price * 1.08,   # 8% TP2
                    time_stop_min=120,
                    state=PositionState.OPEN,
                    strategy_id="synced",  # Mark as synced (not new trade)
                    entry_cost_usd=cost_basis if cost_basis > 0 else value_usd,  # Use REAL cost basis!
                )
                if not quiet:
                    status = "🟢" if unrealized_pnl > 0 else "🔴"
                    cost_display = cost_basis if cost_basis > 0 else value_usd
                    print(f"[SYNC] {status} Tracking {symbol}: ${value_usd:.0f} (cost ${cost_display:.0f}) @ ${entry_price:.2f}")
        
        # Remove orphaned positions (we track but don't actually hold)
        orphaned = [sym for sym in positions.keys() if sym not in real_holdings]
        for symbol in orphaned:
            print(f"[SYNC] ⚠️ {symbol}: No longer held - removing")
            del positions[symbol]
            clear_position(symbol)
        
        # Save if anything changed (new positions added, positions removed, or values updated)
        new_positions = any(h.get('was_new', False) for h in real_holdings.values())
        if orphaned or new_positions or real_holdings:
            save_positions(positions)
        
        return positions
        
    except Exception as e:
        print(f"[SYNC] Exchange sync failed: {e}")
        import traceback
        traceback.print_exc()
        return positions


def get_real_pnl(client, positions: dict[str, Position]) -> dict:
    """Calculate real P&L based on current prices."""
    if client is None:
        return {"total_pnl": 0, "positions": {}}
    
    results = {"total_pnl": 0.0, "positions": {}}
    
    for symbol, pos in positions.items():
        try:
            # Get current price
            product = client.get_product(symbol)
            current_price = float(getattr(product, 'price', 0))
            
            if current_price > 0:
                pnl = (current_price - pos.entry_price) * pos.size_qty
                pnl_pct = ((current_price / pos.entry_price) - 1) * 100
                
                results["positions"][symbol] = {
                    "entry": pos.entry_price,
                    "current": current_price,
                    "qty": pos.size_qty,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct
                }
                results["total_pnl"] += pnl
        except Exception:
            pass
    
    return results
