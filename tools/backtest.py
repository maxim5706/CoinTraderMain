#!/usr/bin/env python3
"""
Simple Backtesting Tool - Test strategies on historical candle data.

Usage:
    uv run python tools/backtest.py --days 7 --strategy mean_reversion
    uv run python tools/backtest.py --days 30 --symbol SOL-USD
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from typing import Optional
import sys

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import settings

LOGS_DIR = Path(__file__).parent.parent / "logs"
CANDLES_DIR = LOGS_DIR / "candles"


def load_candles(days: int = 7, symbol: Optional[str] = None) -> dict:
    """Load historical 1m candles from logs."""
    candles = defaultdict(list)
    
    # Check both log locations
    sources = [
        LOGS_DIR.glob("candles_1m_*.jsonl"),
        CANDLES_DIR.glob("*_1m.jsonl") if CANDLES_DIR.exists() else [],
    ]
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    for source in sources:
        for path in source:
            try:
                with open(path) as f:
                    for line in f:
                        try:
                            data = json.loads(line)
                            sym = data.get("symbol", "")
                            
                            if symbol and sym != symbol:
                                continue
                            
                            ts_str = data.get("ts", data.get("timestamp", ""))
                            if ts_str:
                                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                if ts >= cutoff:
                                    candles[sym].append({
                                        "ts": ts,
                                        "open": float(data.get("open", 0)),
                                        "high": float(data.get("high", 0)),
                                        "low": float(data.get("low", 0)),
                                        "close": float(data.get("close", 0)),
                                        "volume": float(data.get("volume", 0)),
                                    })
                        except:
                            pass
            except:
                pass
    
    # Sort by timestamp
    for sym in candles:
        candles[sym].sort(key=lambda x: x["ts"])
    
    return dict(candles)


def calculate_indicators(candles: list[dict], lookback: int = 20) -> dict:
    """Calculate basic indicators for backtesting."""
    if len(candles) < lookback:
        return {}
    
    closes = [c["close"] for c in candles[-lookback:]]
    highs = [c["high"] for c in candles[-lookback:]]
    lows = [c["low"] for c in candles[-lookback:]]
    volumes = [c["volume"] for c in candles[-lookback:]]
    
    # SMA
    sma = sum(closes) / len(closes)
    
    # RSI (simplified)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    
    avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else 0
    avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else 0
    
    # Handle division by zero for RSI
    if avg_loss == 0:
        rsi = 100 if avg_gain > 0 else 50  # All gains = overbought, no movement = neutral
    elif avg_gain == 0:
        rsi = 0  # All losses = oversold
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
    
    # Bollinger Bands
    std = (sum((c - sma) ** 2 for c in closes) / len(closes)) ** 0.5
    bb_upper = sma + 2 * std
    bb_lower = sma - 2 * std
    bb_width = bb_upper - bb_lower
    bb_position = (closes[-1] - bb_lower) / bb_width if bb_width > 0 else 0.5
    
    # Volume ratio
    avg_vol = sum(volumes[:-1]) / (len(volumes) - 1) if len(volumes) > 1 else 1
    vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1
    
    # Trend
    trend_5 = (closes[-1] / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
    trend_15 = (closes[-1] / closes[-15] - 1) * 100 if len(closes) >= 15 else 0
    
    return {
        "price": closes[-1],
        "sma": sma,
        "rsi": rsi,
        "bb_position": bb_position,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "vol_ratio": vol_ratio,
        "trend_5": trend_5,
        "trend_15": trend_15,
    }


def simulate_mean_reversion(candles: list[dict], params: dict) -> list[dict]:
    """Simulate mean reversion strategy on historical data."""
    trades = []
    position = None
    
    for i in range(30, len(candles)):
        window = candles[:i+1]
        ind = calculate_indicators(window)
        
        if not ind:
            continue
        
        price = ind["price"]
        
        # Exit logic
        if position:
            pnl_pct = (price / position["entry"] - 1) * 100
            hold_bars = i - position["entry_idx"]
            
            # Check exits
            exit_reason = None
            if price <= position["stop"]:
                exit_reason = "stop"
            elif price >= position["tp1"] and not position.get("tp1_hit"):
                position["tp1_hit"] = True
                # Partial exit - just track
            elif price >= position["tp2"]:
                exit_reason = "tp2"
            elif hold_bars >= 180:  # Time stop at 180 bars (3 hours)
                exit_reason = "time"
            
            if exit_reason:
                trades.append({
                    "symbol": position["symbol"],
                    "entry": position["entry"],
                    "exit": price,
                    "pnl_pct": pnl_pct,
                    "exit_reason": exit_reason,
                    "hold_bars": hold_bars,
                    "rsi_at_entry": position["rsi"],
                    "bb_at_entry": position["bb_position"],
                })
                position = None
        
        # Entry logic (only if no position)
        if position is None:
            # Mean reversion: low BB + low RSI
            if ind["bb_position"] < 0.2 and ind["rsi"] < 35:
                stop_pct = params.get("stop_pct", 0.02)
                tp1_pct = params.get("tp1_pct", 0.03)  # Closer: 3%
                tp2_pct = params.get("tp2_pct", 0.05)  # Closer: 5%
                
                position = {
                    "symbol": "SIM",
                    "entry": price,
                    "entry_idx": i,
                    "stop": price * (1 - stop_pct),
                    "tp1": price * (1 + tp1_pct),
                    "tp2": price * (1 + tp2_pct),
                    "rsi": ind["rsi"],
                    "bb_position": ind["bb_position"],
                }
    
    return trades


def run_backtest(days: int = 7, symbol: Optional[str] = None, strategy: str = "mean_reversion"):
    """Run backtest and print results."""
    print(f"\n{'='*60}")
    print(f"BACKTEST: {strategy} | Days: {days} | Symbol: {symbol or 'ALL'}")
    print(f"{'='*60}\n")
    
    candles = load_candles(days, symbol)
    
    if not candles:
        print("❌ No candle data found!")
        print(f"   Looked in: {LOGS_DIR}")
        return
    
    print(f"📊 Loaded candles for {len(candles)} symbols")
    
    all_trades = []
    params = {
        "stop_pct": settings.fixed_stop_pct,
        "tp1_pct": settings.tp1_pct,
        "tp2_pct": settings.tp2_pct,
    }
    
    for sym, sym_candles in candles.items():
        if len(sym_candles) < 100:
            continue
        
        if strategy == "mean_reversion":
            trades = simulate_mean_reversion(sym_candles, params)
            for t in trades:
                t["symbol"] = sym
            all_trades.extend(trades)
    
    if not all_trades:
        print("❌ No trades generated")
        return
    
    # Calculate stats
    wins = [t for t in all_trades if t["pnl_pct"] > 0]
    losses = [t for t in all_trades if t["pnl_pct"] <= 0]
    
    total_pnl = sum(t["pnl_pct"] for t in all_trades)
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    win_rate = len(wins) / len(all_trades) * 100 if all_trades else 0
    
    # Profit factor
    gross_profit = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))
    pf = gross_profit / gross_loss if gross_loss > 0 else 0
    
    print(f"\n📈 RESULTS:")
    print(f"   Total Trades: {len(all_trades)}")
    print(f"   Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"   Win Rate: {win_rate:.1f}%")
    print(f"   Avg Win: +{avg_win:.2f}% | Avg Loss: {avg_loss:.2f}%")
    print(f"   Total PnL: {total_pnl:+.2f}%")
    print(f"   Profit Factor: {pf:.2f}")
    
    # Exit reasons
    print(f"\n📊 Exit Reasons:")
    reasons = defaultdict(int)
    for t in all_trades:
        reasons[t["exit_reason"]] += 1
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"   {reason}: {count} ({count/len(all_trades)*100:.1f}%)")
    
    # By symbol
    print(f"\n📊 Top Symbols:")
    by_symbol = defaultdict(list)
    for t in all_trades:
        by_symbol[t["symbol"]].append(t)
    
    symbol_stats = []
    for sym, trades in by_symbol.items():
        pnl = sum(t["pnl_pct"] for t in trades)
        wr = len([t for t in trades if t["pnl_pct"] > 0]) / len(trades) * 100
        symbol_stats.append((sym, len(trades), pnl, wr))
    
    symbol_stats.sort(key=lambda x: -x[2])
    for sym, count, pnl, wr in symbol_stats[:10]:
        print(f"   {sym}: {count} trades, {pnl:+.2f}%, {wr:.0f}% win")
    
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest trading strategies")
    parser.add_argument("--days", type=int, default=7, help="Days of history")
    parser.add_argument("--symbol", type=str, help="Specific symbol to test")
    parser.add_argument("--strategy", type=str, default="mean_reversion", help="Strategy to test")
    
    args = parser.parse_args()
    run_backtest(args.days, args.symbol, args.strategy)
