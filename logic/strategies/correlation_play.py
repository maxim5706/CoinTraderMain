"""
Correlation Play Strategy - Follow the leader.

When BTC/ETH pump, their ecosystems follow with a lag.
When meme coins pump, other memes follow.

Entry: Leader pumps → enter followers early
Exit: Normal targets

This catches "sympathy moves" and sector rotation.
"""

from logic.strategies.base import BaseStrategy, StrategySignal, SignalDirection
from typing import Optional


# Sector correlations
ECOSYSTEMS = {
    'btc': ['STX', 'ORDI', 'SATS'],
    'eth': ['LDO', 'AAVE', 'UNI', 'LINK', 'ENS', 'COMP'],
    'sol': ['ORCA', 'TNSR', 'JTO', 'BONK', 'WIF', 'PYTH'],
    'meme': ['DOGE', 'SHIB', 'PEPE', 'FARTCOIN', 'BONK', 'WIF', 'FLOKI', 'PENGU', 'MOODENG'],
    'ai': ['FET', 'RNDR', 'TAO', 'AGIX', 'OCEAN'],
    'gaming': ['IMX', 'GALA', 'SAND', 'AXS', 'ENJ'],
    'defi': ['AAVE', 'CRV', 'UNI', 'COMP', 'SNX', 'MKR']
}


class CorrelationPlayStrategy(BaseStrategy):
    """
    Trade sympathy moves when sector leaders pump.
    
    Example: SOL up 10% → enter ORCA, JTO, TNSR
    """
    
    strategy_id = "correlation_play"
    
    # Track sector momentum
    _sector_momentum: dict = {}  # sector -> {leader_move, detected_at}
    
    def analyze(
        self,
        symbol: str,
        buffer,
        features: dict,
        market_context: dict
    ) -> Optional[StrategySignal]:
        """Find correlation opportunities."""
        
        price = features.get('price', 0)
        if price <= 0:
            return None
        
        # Get symbol's base coin (remove -USD)
        base = symbol.replace('-USD', '')
        
        # Find which sector this coin belongs to
        coin_sector = None
        for sector, coins in ECOSYSTEMS.items():
            if base in coins:
                coin_sector = sector
                break
        
        if not coin_sector:
            return None  # Not in a tracked ecosystem
        
        # Check if sector leader is pumping
        leader_move = self._check_sector_momentum(coin_sector, market_context)
        if leader_move < 3.0:  # Need 3%+ leader move
            return None
        
        # Check if this follower is lagging (hasn't moved yet)
        trend_15m = features.get('trend_15m', 0)
        trend_1h = features.get('trend_1h', 0)
        
        # Want coin that hasn't moved much yet (catching early)
        if trend_15m > 2.0 or trend_1h > 4.0:
            return None  # Already moved, too late
        
        # Check relative strength
        if trend_15m < -1.0:  # Moving down while sector up = weak
            return None
        
        # Volume should be building
        vol_spike = features.get('vol_spike_5m', 0)
        if vol_spike < 0.8:  # Too quiet
            return None
        
        # Calculate score
        score = 65  # Base for correlation
        
        # Stronger leader move = better
        score += min(20, int(leader_move * 2))  # +0-20
        
        # Volume building = early entry
        if vol_spike > 1.2:
            score += 10
        
        # Price near VWAP = healthy
        vwap_dist = features.get('vwap_distance', -999)
        if -0.5 < vwap_dist < 0.5:
            score += 10
        
        return StrategySignal(
            symbol=symbol,
            strategy_id=self.strategy_id,
            direction=SignalDirection.LONG,
            edge_score_base=min(90, score),
            trend_score=50 + int(leader_move),
            setup_quality=leader_move / 10,  # Strength of leader
            reasons=[
                f"{coin_sector}_rotation",
                f"leader_+{leader_move:.1f}%",
                "follower_lagging",
                "vol_building"
            ],
            is_valid=True
        )
    
    def _check_sector_momentum(self, sector: str, market_context: dict) -> float:
        """
        Check if sector leader is pumping.
        
        Returns: Leader's % move in last hour
        """
        # Map sector to leader symbol
        leaders = {
            'btc': 'BTC-USD',
            'eth': 'ETH-USD',
            'sol': 'SOL-USD',
            'meme': 'DOGE-USD',  # DOGE often leads memes
            'ai': 'FET-USD',
            'gaming': 'IMX-USD',
            'defi': 'AAVE-USD'
        }
        
        leader_symbol = leaders.get(sector)
        if not leader_symbol:
            return 0.0
        
        # Get leader's trend from market context
        # (This would be populated from real data in production)
        btc_trend = market_context.get('btc_trend_1h', 0)
        
        if sector == 'btc':
            return btc_trend
        elif sector == 'eth':
            return market_context.get('eth_trend_1h', 0)
        
        # For other sectors, estimate from BTC (correlated)
        # In production, you'd track each leader separately
        return btc_trend * 0.7  # Alt sectors move ~70% of BTC
    
    def reset(self, symbol: str):
        """Clear state."""
        pass  # Stateless strategy
