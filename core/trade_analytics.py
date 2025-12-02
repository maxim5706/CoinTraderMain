"""
Trade Analytics - Capture full context for every trade to enable ML training.

This creates a rich dataset for:
1. Understanding what makes winning vs losing trades
2. Training ML models on successful patterns
3. Backtesting strategy changes
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

ANALYTICS_DIR = Path(__file__).parent.parent / "data" / "analytics"


@dataclass
class TradeContext:
    """Full context snapshot at trade entry."""
    
    # Trade identifiers
    trade_id: str
    symbol: str
    strategy_id: str
    timestamp: str
    
    # Entry details
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    size_usd: float
    
    # Score breakdown
    total_score: int
    trend_score: int
    volume_score: int
    vwap_score: int
    range_score: int
    tier_score: int
    spread_score: int
    quality_adjust: int
    
    # Indicators at entry
    rsi_7: float
    rsi_14: float
    macd_histogram: float
    bb_position: float
    ema9: float
    ema21: float
    atr_pct: float
    
    # Volume metrics
    volume_ratio: float
    buy_pressure: float
    obv_slope: float
    
    # Market context
    btc_regime: str
    btc_trend_1h: float
    vol_regime: str
    spread_bps: float
    vwap_distance: float
    
    # Pattern metrics
    trend_5m: float
    trend_15m: float
    chop_score: float
    momentum_10: float
    
    # Time context
    hour_utc: int
    day_of_week: int
    
    # Outcome (filled after close)
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    hold_minutes: Optional[int] = None
    is_win: Optional[bool] = None
    hit_tp1: bool = False
    hit_tp2: bool = False
    hit_stop: bool = False


def log_trade_entry(context: TradeContext):
    """Log trade entry with full context."""
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = ANALYTICS_DIR / f"trades_{date_str}.jsonl"
    
    record = asdict(context)
    record["event"] = "entry"
    
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    
    return context.trade_id


def log_trade_exit(
    trade_id: str,
    exit_price: float,
    exit_reason: str,
    pnl_usd: float,
    pnl_pct: float,
    hold_minutes: int,
    hit_tp1: bool = False,
    hit_tp2: bool = False,
    hit_stop: bool = False,
):
    """Log trade exit to complete the record."""
    ANALYTICS_DIR.mkdir(parents=True, exist_ok=True)
    
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = ANALYTICS_DIR / f"trades_{date_str}.jsonl"
    
    record = {
        "event": "exit",
        "trade_id": trade_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "hold_minutes": hold_minutes,
        "is_win": pnl_usd > 0,
        "hit_tp1": hit_tp1,
        "hit_tp2": hit_tp2,
        "hit_stop": hit_stop,
    }
    
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def load_trade_history(days: int = 30) -> list[dict]:
    """Load recent trade history for analysis."""
    trades = []
    
    if not ANALYTICS_DIR.exists():
        return trades
    
    for path in sorted(ANALYTICS_DIR.glob("trades_*.jsonl"))[-days:]:
        with open(path) as f:
            for line in f:
                try:
                    trades.append(json.loads(line))
                except:
                    pass
    
    return trades


def analyze_win_patterns(trades: list[dict]) -> Dict[str, Any]:
    """Analyze what patterns lead to wins vs losses."""
    entries = [t for t in trades if t.get("event") == "entry"]
    exits = {t["trade_id"]: t for t in trades if t.get("event") == "exit"}
    
    wins = []
    losses = []
    
    for entry in entries:
        exit_data = exits.get(entry["trade_id"])
        if exit_data and exit_data.get("is_win") is not None:
            if exit_data["is_win"]:
                wins.append({**entry, **exit_data})
            else:
                losses.append({**entry, **exit_data})
    
    if not wins or not losses:
        return {"error": "Not enough data"}
    
    # Compare averages
    def avg(lst, key):
        vals = [x.get(key, 0) for x in lst if x.get(key) is not None]
        return sum(vals) / len(vals) if vals else 0
    
    indicators = [
        "rsi_14", "bb_position", "volume_ratio", "buy_pressure",
        "trend_15m", "chop_score", "total_score", "quality_adjust"
    ]
    
    analysis = {
        "total_trades": len(entries),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / (len(wins) + len(losses)) * 100,
    }
    
    for ind in indicators:
        analysis[f"win_avg_{ind}"] = avg(wins, ind)
        analysis[f"loss_avg_{ind}"] = avg(losses, ind)
        analysis[f"diff_{ind}"] = avg(wins, ind) - avg(losses, ind)
    
    return analysis


def get_optimal_thresholds(trades: list[dict]) -> Dict[str, float]:
    """Suggest optimal thresholds based on win/loss patterns."""
    analysis = analyze_win_patterns(trades)
    
    if "error" in analysis:
        return {}
    
    suggestions = {}
    
    # If winning trades have higher RSI, suggest RSI floor
    if analysis.get("diff_rsi_14", 0) > 5:
        suggestions["min_rsi"] = analysis["win_avg_rsi_14"] - 10
    
    # If winning trades have lower chop, suggest chop ceiling
    if analysis.get("diff_chop_score", 0) < -0.1:
        suggestions["max_chop"] = analysis["win_avg_chop_score"] + 0.1
    
    # If winning trades have higher volume, suggest volume floor
    if analysis.get("diff_volume_ratio", 0) > 0.5:
        suggestions["min_volume_ratio"] = analysis["win_avg_volume_ratio"] - 0.5
    
    return suggestions
