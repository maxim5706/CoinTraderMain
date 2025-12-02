"""
Portfolio tracking using Coinbase Advanced Trade API.

Uses the proper Portfolio Breakdown endpoint which provides:
- Real entry prices (average_entry_price)
- Real unrealized P&L (computed by Coinbase)
- Cost basis
- Position sizes

This is the source of truth for positions and P&L.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, List
from coinbase.rest import RESTClient

from core.config import settings


@dataclass
class SpotPosition:
    """A spot position from Coinbase portfolio breakdown."""
    symbol: str              # e.g., "BTC-USD"
    asset: str               # e.g., "BTC"
    
    # Quantities
    qty: float               # Total balance in crypto
    value_usd: float         # Current value in USD
    
    # Cost basis
    entry_price: float       # Average entry price
    cost_basis: float        # Total cost in USD
    
    # P&L
    unrealized_pnl: float    # $ P&L (from Coinbase)
    unrealized_pnl_pct: float  # % P&L (computed)
    
    # Metadata
    is_cash: bool = False    # USD/USDC


@dataclass 
class PortfolioSnapshot:
    """Complete portfolio snapshot from Coinbase."""
    timestamp: datetime
    portfolio_uuid: str
    
    # Totals
    total_value: float       # Total portfolio value
    total_cash: float        # USD + USDC
    total_crypto: float      # All crypto value
    
    # P&L
    total_unrealized_pnl: float
    total_realized_pnl: float  # Computed from fills
    
    # Positions
    positions: Dict[str, SpotPosition] = field(default_factory=dict)
    
    @property
    def position_count(self) -> int:
        return len([p for p in self.positions.values() if not p.is_cash])


class PortfolioTracker:
    """
    Track portfolio using Coinbase Advanced Trade API.
    
    Uses Portfolio Breakdown endpoint for accurate P&L.
    """
    
    def __init__(self):
        self._client: Optional[RESTClient] = None
        self._portfolio_uuid: Optional[str] = None
        self._last_snapshot: Optional[PortfolioSnapshot] = None
        self._realized_pnl: float = 0.0  # Computed from fills
        
    def _init_client(self) -> bool:
        """Initialize Coinbase client."""
        if self._client is not None:
            return True
            
        try:
            self._client = RESTClient(
                api_key=settings.coinbase_api_key,
                api_secret=settings.coinbase_api_secret
            )
            return True
        except Exception as e:
            print(f"[PORTFOLIO] Failed to init client: {e}")
            return False
    
    def _get_portfolio_uuid(self) -> Optional[str]:
        """Get the default portfolio UUID."""
        if self._portfolio_uuid:
            return self._portfolio_uuid
            
        if not self._init_client():
            return None
            
        try:
            portfolios = self._client.get_portfolios()
            portfolio_list = getattr(portfolios, 'portfolios', [])
            
            for p in portfolio_list:
                ptype = getattr(p, 'type', '') or p.get('type', '')
                if ptype == 'DEFAULT':
                    self._portfolio_uuid = getattr(p, 'uuid', None) or p.get('uuid')
                    print(f"[PORTFOLIO] Using portfolio: {self._portfolio_uuid}")
                    return self._portfolio_uuid
            
            # Use first one if no DEFAULT
            if portfolio_list:
                p = portfolio_list[0]
                self._portfolio_uuid = getattr(p, 'uuid', None) or p.get('uuid')
                return self._portfolio_uuid
                
        except Exception as e:
            print(f"[PORTFOLIO] Failed to get portfolios: {e}")
            
        return None
    
    def get_snapshot(self) -> Optional[PortfolioSnapshot]:
        """
        Get current portfolio snapshot with all positions and P&L.
        
        This is the source of truth for positions.
        """
        if not self._init_client():
            return None
            
        uuid = self._get_portfolio_uuid()
        if not uuid:
            return None
            
        try:
            breakdown_resp = self._client.get_portfolio_breakdown(uuid)
            breakdown = getattr(breakdown_resp, 'breakdown', breakdown_resp)
            
            # Parse spot positions
            positions = {}
            total_crypto = 0.0
            total_cash = 0.0
            total_pnl = 0.0
            
            spot_positions = getattr(breakdown, 'spot_positions', [])
            
            for pos in spot_positions:
                # Handle both dict and object responses
                if isinstance(pos, dict):
                    asset = pos.get('asset', '')
                    qty = float(pos.get('total_balance_crypto', 0) or 0)
                    value_usd = float(pos.get('total_balance_fiat', 0) or 0)
                    entry_obj = pos.get('average_entry_price', {})
                    cost_obj = pos.get('cost_basis', {})
                    pnl_val = pos.get('unrealized_pnl', 0)
                    is_cash_flag = pos.get('is_cash', False)
                else:
                    asset = getattr(pos, 'asset', '')
                    qty = float(getattr(pos, 'total_balance_crypto', 0) or 0)
                    value_usd = float(getattr(pos, 'total_balance_fiat', 0) or 0)
                    entry_obj = getattr(pos, 'average_entry_price', {})
                    cost_obj = getattr(pos, 'cost_basis', {})
                    pnl_val = getattr(pos, 'unrealized_pnl', 0)
                    is_cash_flag = getattr(pos, 'is_cash', False)
                
                if not asset:
                    continue
                
                symbol = f"{asset}-USD"
                
                # Extract nested values (entry/cost come as {'value': '...', 'currency': 'USD'})
                if isinstance(entry_obj, dict):
                    entry_price = float(entry_obj.get('value', 0) or 0)
                else:
                    entry_price = float(getattr(entry_obj, 'value', 0) or 0)
                    
                if isinstance(cost_obj, dict):
                    cost_basis = float(cost_obj.get('value', 0) or 0)
                else:
                    cost_basis = float(getattr(cost_obj, 'value', 0) or 0)
                    
                unrealized_pnl = float(pnl_val) if pnl_val else 0.0
                
                # Calculate % P&L
                if cost_basis > 0:
                    pnl_pct = (unrealized_pnl / cost_basis) * 100
                else:
                    pnl_pct = 0.0
                
                # Is this cash? Use API flag or check asset name
                is_cash = is_cash_flag or asset in ['USD', 'USDC']
                
                if is_cash:
                    total_cash += value_usd
                else:
                    total_crypto += value_usd
                    total_pnl += unrealized_pnl
                
                # Only track positions with value
                if value_usd >= 0.01:
                    positions[symbol] = SpotPosition(
                        symbol=symbol,
                        asset=asset,
                        qty=qty,
                        value_usd=value_usd,
                        entry_price=entry_price,
                        cost_basis=cost_basis,
                        unrealized_pnl=unrealized_pnl,
                        unrealized_pnl_pct=pnl_pct,
                        is_cash=is_cash
                    )
            
            snapshot = PortfolioSnapshot(
                timestamp=datetime.now(timezone.utc),
                portfolio_uuid=uuid,
                total_value=total_cash + total_crypto,
                total_cash=total_cash,
                total_crypto=total_crypto,
                total_unrealized_pnl=total_pnl,
                total_realized_pnl=self._realized_pnl,
                positions=positions
            )
            
            self._last_snapshot = snapshot
            return snapshot
            
        except Exception as e:
            print(f"[PORTFOLIO] Failed to get breakdown: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def get_position(self, symbol: str) -> Optional[SpotPosition]:
        """Get a specific position."""
        if not self._last_snapshot:
            self.get_snapshot()
        
        if self._last_snapshot:
            return self._last_snapshot.positions.get(symbol)
        return None
    
    def has_position(self, symbol: str) -> bool:
        """Check if we have a position in a symbol."""
        pos = self.get_position(symbol)
        return pos is not None and not pos.is_cash and pos.value_usd >= 1.0
    
    def get_position_value(self, symbol: str) -> float:
        """Get position value in USD."""
        pos = self.get_position(symbol)
        return pos.value_usd if pos else 0.0
    
    def print_summary(self):
        """Print portfolio summary."""
        snapshot = self.get_snapshot()
        if not snapshot:
            print("[PORTFOLIO] No data")
            return
        
        print(f"\n{'='*60}")
        print(f"PORTFOLIO SNAPSHOT @ {snapshot.timestamp.strftime('%H:%M:%S')}")
        print(f"{'='*60}")
        print(f"Total Value: ${snapshot.total_value:.2f}")
        print(f"  Cash:      ${snapshot.total_cash:.2f}")
        print(f"  Crypto:    ${snapshot.total_crypto:.2f}")
        print(f"Unrealized:  ${snapshot.total_unrealized_pnl:+.2f}")
        print(f"Positions:   {snapshot.position_count}")
        print(f"{'='*60}")
        
        # Sort by value
        crypto_positions = [
            p for p in snapshot.positions.values() 
            if not p.is_cash and p.value_usd >= 1.0
        ]
        crypto_positions.sort(key=lambda x: -x.value_usd)
        
        print(f"{'Symbol':<12} {'Value':>10} {'Entry':>10} {'P&L $':>10} {'P&L %':>8}")
        print("-" * 52)
        
        for pos in crypto_positions[:15]:
            pnl_color = "+" if pos.unrealized_pnl >= 0 else ""
            print(
                f"{pos.asset:<12} "
                f"${pos.value_usd:>9.2f} "
                f"${pos.entry_price:>9.4f} "
                f"${pnl_color}{pos.unrealized_pnl:>8.2f} "
                f"{pos.unrealized_pnl_pct:>+7.2f}%"
            )
        
        print(f"{'='*60}\n")


# Singleton instance
portfolio_tracker = PortfolioTracker()
