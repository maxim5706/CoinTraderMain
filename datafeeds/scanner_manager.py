"""Scanner Manager - Clean separation of leaderboard logic."""

import logging
from collections import defaultdict
from typing import List, Optional
from datetime import datetime

from core.state import BurstCandidate

logger = logging.getLogger(__name__)


class ScannerManager:
    """Manages the scanner leaderboard by pulling from multiple sources."""
    
    def __init__(self, state, scanner, get_price_func):
        self.state = state
        self.scanner = scanner
        self.get_price = get_price_func
    
    def update_leaderboard(self, recent_signal_symbols: List[str] = None) -> List[BurstCandidate]:
        """
        Update burst_leaderboard from scanner hot_list (burst metrics) + recent signals.
        
        Scanner shows LIVE MARKET ACTIVITY:
        - Volume spikes (from burst metrics)
        - Range spikes (from burst metrics)
        - Trending coins (from burst metrics)
        - Strategy-signaling coins (from recent_signal_symbols)
        
        Priority:
        1. Recent signal symbols (actively trading) - HIGHEST
        2. Hot list (scanner burst metrics) - PRIMARY
        3. Warm symbols (fallback) - SECONDARY
        
        Returns list of BurstCandidate objects sorted by burst_score.
        """
        candidates = []
        seen_symbols = set()
        recent_signal_symbols = recent_signal_symbols or []
        
        # SOURCE 0: Recent signal symbols (HIGHEST PRIORITY)
        for symbol in recent_signal_symbols[:10]:  # Top 10 recent signals
            if symbol not in seen_symbols:
                info = self.scanner.universe.get(symbol)
                price = self.get_price(symbol)
                
                if price and price > 0:
                    # Get burst metrics if available
                    burst_metrics = self.scanner.burst_metrics.get(symbol)
                    if burst_metrics:
                        vol_spike = burst_metrics.vol_spike
                        trend_5m = burst_metrics.trend_15m
                        burst_score = max(burst_metrics.burst_score, 5.0)  # Boost for signals
                    else:
                        vol_spike = 1.5
                        trend_5m = 0.5
                        burst_score = 5.0  # Default for signaling symbols
                    
                    candidates.append(BurstCandidate(
                        symbol=symbol,
                        price=price,
                        burst_score=burst_score,
                        vol_spike=vol_spike,
                        range_spike=0,
                        trend_5m=trend_5m,
                        trend_slope=0,
                        vwap_dist=0,
                        daily_move=0,
                        tier=info.tier if info else "unknown",
                        rank=len(candidates) + 1,
                        entry_score=85  # High score for active signals
                    ))
                    seen_symbols.add(symbol)
        
        # SOURCE 1: Hot list (burst metrics from scanner) - PRIMARY
        hot_candidates = self._get_hot_list_candidates()
        for candidate in hot_candidates:
            if candidate.symbol not in seen_symbols:
                candidates.append(candidate)
                seen_symbols.add(candidate.symbol)
        
        # SOURCE 2: Fallback to warm symbols if needed
        if len(candidates) < 5:
            warm_candidates = self._get_warm_candidates(limit=10 - len(candidates))
            for candidate in warm_candidates:
                if candidate.symbol not in seen_symbols:
                    candidates.append(candidate)
                    seen_symbols.add(candidate.symbol)
        
        # Sort by burst_score (market activity), not entry_score
        if candidates:
            candidates.sort(key=lambda x: x.burst_score, reverse=True)
            for i, c in enumerate(candidates):
                c.rank = i + 1
            
            # Log summary
            if len(candidates) >= 3:
                top3 = ", ".join([
                    f"{c.symbol.replace('-USD', '')}(burst:{c.burst_score:.1f})" 
                    for c in candidates[:3]
                ])
                logger.info("[SCANNER] Leaderboard: %s (total: %d)", top3, len(candidates))
            
            # Update state
            self.state.burst_leaderboard = candidates[:15]
            return candidates[:15]
        
        return []
    
    def _parse_recent_signals(self) -> List[BurstCandidate]:
        """Parse recent strategy signals from live_log."""
        candidates = []
        
        try:
            # Get last 30 log events
            recent_signals = list(getattr(self.state, "live_log", []))[-30:]
            signal_data = {}  # symbol -> [count, max_score, strategy]
            
            logger.debug("[SCANNER] Parsing %d recent log events", len(recent_signals))
            
            for ts, lvl, msg in recent_signals:
                if lvl == "STRAT" and " score=" in msg:
                    # Parse message like: "ERA daily_momentum score=86"
                    parts = msg.split()
                    if len(parts) < 3:
                        continue
                    
                    sym_name = parts[0]
                    symbol = sym_name if "-USD" in sym_name else sym_name + "-USD"
                    strategy = parts[1] if len(parts) > 1 else "unknown"
                    
                    # Extract score
                    try:
                        score_part = [p for p in parts if "score=" in p][0]
                        score = int(score_part.split("=")[1])
                    except:
                        score = 50
                    
                    # Track symbol data
                    if symbol not in signal_data:
                        signal_data[symbol] = [0, score, strategy]
                    signal_data[symbol][0] += 1  # Increment count
                    signal_data[symbol][1] = max(signal_data[symbol][1], score)  # Best score
            
            logger.debug("[SCANNER] Found %d unique symbols with signals", len(signal_data))
            
            # Convert to BurstCandidate objects
            for symbol, (count, score, strategy) in sorted(
                signal_data.items(), 
                key=lambda x: (x[1][1], x[1][0]),  # Sort by score, then count
                reverse=True
            ):
                info = self.scanner.universe.get(symbol)
                price = self.get_price(symbol)
                
                if price and price > 0:
                    candidates.append(BurstCandidate(
                        symbol=symbol,
                        price=price,
                        burst_score=score / 10.0,  # Scale for display
                        vol_spike=min(count * 0.5, 5.0),  # Signal frequency
                        range_spike=0,
                        trend_5m=0.5 if score > 70 else 0.0,
                        trend_slope=0,
                        vwap_dist=0,
                        daily_move=0,
                        tier=info.tier if info else "unknown",
                        rank=len(candidates) + 1,
                        entry_score=score
                    ))
                    
                    if len(candidates) >= 10:  # Limit to top 10
                        break
            
            logger.debug("[SCANNER] Created %d candidates from signals", len(candidates))
            
        except Exception as e:
            logger.warning("[SCANNER] Failed to parse live_log: %s", e, exc_info=True)
        
        return candidates
    
    def _get_hot_list_candidates(self) -> List[BurstCandidate]:
        """Get candidates from scanner hot_list (burst metrics)."""
        candidates = []
        
        try:
            # Get ALL hot_list symbols (up to 10)
            for m in self.scanner.hot_list.symbols[:10]:
                info = self.scanner.universe.get(m.symbol)
                
                # Calculate entry score from burst metrics
                entry_score = 40
                if m.vol_spike > 10:
                    entry_score += 20
                elif m.vol_spike > 5:
                    entry_score += 15
                elif m.vol_spike > 2:
                    entry_score += 10
                
                candidates.append(BurstCandidate(
                    symbol=m.symbol,
                    price=m.price,
                    burst_score=m.burst_score,
                    vol_spike=m.vol_spike,
                    range_spike=m.range_spike,
                    trend_5m=m.trend_15m,
                    trend_slope=m.trend_slope,
                    vwap_dist=m.vwap_distance,
                    daily_move=m.daily_move,
                    tier=info.tier if info else "unknown",
                    rank=len(candidates) + 1,
                    entry_score=entry_score
                ))
            
            logger.debug("[SCANNER] Got %d candidates from hot_list", len(candidates))
            
        except Exception as e:
            logger.warning("[SCANNER] Failed to get hot_list: %s", e)
        
        return candidates
    
    def _get_warm_candidates(self, limit: int = 5) -> List[BurstCandidate]:
        """Get warm symbols as fallback."""
        candidates = []
        
        try:
            from core.candle_store import candle_store
            warm_symbols = candle_store.list_symbols()
            
            for symbol in warm_symbols[:limit]:
                info = self.scanner.universe.get(symbol)
                price = self.get_price(symbol)
                
                if price and price > 0:
                    candidates.append(BurstCandidate(
                        symbol=symbol,
                        price=price,
                        burst_score=0,
                        vol_spike=1.0,
                        range_spike=0,
                        trend_5m=0,
                        trend_slope=0,
                        vwap_dist=0,
                        daily_move=0,
                        tier=info.tier if info else "unknown",
                        rank=len(candidates) + 1,
                        entry_score=45
                    ))
        except Exception as e:
            logger.warning("[SCANNER] Failed to get warm symbols: %s", e)
        
        return candidates
