#!/usr/bin/env python3
"""
Trade Analysis Tool - Analyze historical trades to find patterns.

Usage:
    uv run python tools/analyze_trades.py
    uv run python tools/analyze_trades.py --days 30
"""

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

LOGS_DIR = Path(__file__).parent.parent / "logs"


def load_trades(days: int = 30) -> list[dict]:
    """Load trade history from logs."""
    trades = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    for path in sorted(LOGS_DIR.glob("trades_*.jsonl")):
        try:
            with open(path) as f:
                for line in f:
                    try:
                        data = json.loads(line)
                        # Only get trade closes
                        if data.get("type") == "trade_close":
                            trades.append(data)
                    except:
                        pass
        except:
            pass
    
    return trades


def analyze_by_strategy(trades: list[dict]):
    """Analyze performance by strategy."""
    by_strategy = defaultdict(list)
    
    for t in trades:
        strat = t.get("strategy_id", "unknown")
        by_strategy[strat].append(t)
    
    print("\n📊 PERFORMANCE BY STRATEGY:")
    print("-" * 60)
    
    for strat, strat_trades in sorted(by_strategy.items()):
        if not strat_trades:
            continue
        
        wins = [t for t in strat_trades if t.get("pnl", 0) > 0]
        total_pnl = sum(t.get("pnl", 0) for t in strat_trades)
        win_rate = len(wins) / len(strat_trades) * 100 if strat_trades else 0
        avg_r = sum(t.get("r_multiple", 0) for t in strat_trades) / len(strat_trades) if strat_trades else 0
        
        print(f"\n{strat}:")
        print(f"  Trades: {len(strat_trades)}")
        print(f"  Win Rate: {win_rate:.1f}%")
        print(f"  Total PnL: ${total_pnl:+.2f}")
        print(f"  Avg R: {avg_r:+.2f}")


def analyze_by_exit_reason(trades: list[dict]):
    """Analyze performance by exit reason."""
    by_reason = defaultdict(list)
    
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        by_reason[reason].append(t)
    
    print("\n📊 PERFORMANCE BY EXIT REASON:")
    print("-" * 60)
    
    for reason, reason_trades in sorted(by_reason.items(), key=lambda x: -len(x[1])):
        if not reason_trades:
            continue
        
        wins = len([t for t in reason_trades if t.get("pnl", 0) > 0])
        total_pnl = sum(t.get("pnl", 0) for t in reason_trades)
        avg_hold = sum(t.get("hold_minutes", 0) for t in reason_trades) / len(reason_trades)
        
        print(f"\n{reason}:")
        print(f"  Count: {len(reason_trades)} ({len(reason_trades)/len(trades)*100:.1f}%)")
        print(f"  Win Rate: {wins/len(reason_trades)*100:.1f}%")
        print(f"  Total PnL: ${total_pnl:+.2f}")
        print(f"  Avg Hold: {avg_hold:.0f} min")


def analyze_by_symbol(trades: list[dict]):
    """Analyze performance by symbol."""
    by_symbol = defaultdict(list)
    
    for t in trades:
        sym = t.get("symbol", "unknown")
        by_symbol[sym].append(t)
    
    print("\n📊 PERFORMANCE BY SYMBOL (Top 15):")
    print("-" * 60)
    
    symbol_stats = []
    for sym, sym_trades in by_symbol.items():
        if not sym_trades:
            continue
        
        wins = len([t for t in sym_trades if t.get("pnl", 0) > 0])
        total_pnl = sum(t.get("pnl", 0) for t in sym_trades)
        win_rate = wins / len(sym_trades) * 100
        
        symbol_stats.append({
            "symbol": sym,
            "trades": len(sym_trades),
            "pnl": total_pnl,
            "win_rate": win_rate,
        })
    
    # Sort by PnL
    symbol_stats.sort(key=lambda x: -x["pnl"])
    
    print(f"\n{'Symbol':<15} {'Trades':>8} {'Win%':>8} {'PnL':>10}")
    print("-" * 45)
    
    for s in symbol_stats[:15]:
        pnl_str = f"${s['pnl']:+.2f}"
        print(f"{s['symbol']:<15} {s['trades']:>8} {s['win_rate']:>7.1f}% {pnl_str:>10}")


def analyze_by_hour(trades: list[dict]):
    """Analyze performance by hour of day."""
    by_hour = defaultdict(list)
    
    for t in trades:
        ts_str = t.get("ts", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                by_hour[ts.hour].append(t)
            except:
                pass
    
    print("\n📊 PERFORMANCE BY HOUR (UTC):")
    print("-" * 60)
    
    print(f"\n{'Hour':<8} {'Trades':>8} {'Win%':>8} {'PnL':>10}")
    print("-" * 38)
    
    for hour in range(24):
        hour_trades = by_hour.get(hour, [])
        if not hour_trades:
            continue
        
        wins = len([t for t in hour_trades if t.get("pnl", 0) > 0])
        total_pnl = sum(t.get("pnl", 0) for t in hour_trades)
        win_rate = wins / len(hour_trades) * 100 if hour_trades else 0
        
        pnl_str = f"${total_pnl:+.2f}"
        print(f"{hour:02d}:00   {len(hour_trades):>8} {win_rate:>7.1f}% {pnl_str:>10}")


def suggest_improvements(trades: list[dict]):
    """Suggest parameter improvements based on trade history."""
    print("\n💡 SUGGESTED IMPROVEMENTS:")
    print("-" * 60)
    
    if not trades:
        print("  Not enough data for suggestions")
        return
    
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    
    # Calculate current stats
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t.get("pnl_pct", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.get("pnl_pct", 0) for t in losses) / len(losses) if losses else 0
    
    print(f"\n  Current Performance:")
    print(f"    Win Rate: {win_rate:.1f}%")
    print(f"    Avg Win: {avg_win:+.2f}%")
    print(f"    Avg Loss: {avg_loss:.2f}%")
    
    # Suggestions
    suggestions = []
    
    if win_rate < 35:
        suggestions.append("⚠️ Win rate too low (<35%). Consider:")
        suggestions.append("   - Tighter entry criteria (higher score threshold)")
        suggestions.append("   - Better market regime filtering")
    
    if abs(avg_loss) > avg_win * 1.5:
        suggestions.append("⚠️ Losses too large compared to wins. Consider:")
        suggestions.append("   - Tighter stop losses")
        suggestions.append("   - Earlier exit on thesis invalidation")
    
    # Check stop hits
    stop_exits = [t for t in trades if t.get("exit_reason") == "stop"]
    if len(stop_exits) / len(trades) > 0.5:
        suggestions.append("⚠️ Too many stop-outs (>50%). Consider:")
        suggestions.append("   - Wider stops (but watch R:R)")
        suggestions.append("   - Better entry timing (wait for pullback)")
    
    # Check time exits
    time_exits = [t for t in trades if "time" in str(t.get("exit_reason", ""))]
    time_exit_pnl = sum(t.get("pnl", 0) for t in time_exits)
    if time_exits and time_exit_pnl < 0:
        suggestions.append("⚠️ Time exits losing money. Consider:")
        suggestions.append("   - Longer time stops for winners")
        suggestions.append("   - Earlier exit for non-movers")
    
    print(f"\n  Suggestions:")
    for s in suggestions:
        print(f"  {s}")
    
    if not suggestions:
        print("  ✅ No major issues detected!")


def main():
    parser = argparse.ArgumentParser(description="Analyze trade history")
    parser.add_argument("--days", type=int, default=30, help="Days of history to analyze")
    args = parser.parse_args()
    
    print(f"\n{'='*60}")
    print(f"TRADE ANALYSIS - Last {args.days} Days")
    print(f"{'='*60}")
    
    trades = load_trades(args.days)
    
    if not trades:
        print("\n❌ No trades found!")
        print(f"   Looked in: {LOGS_DIR}")
        return
    
    print(f"\n📊 Found {len(trades)} trades")
    
    # Overall stats
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    
    print(f"\n📈 OVERALL:")
    print(f"   Total Trades: {len(trades)}")
    print(f"   Wins: {len(wins)} | Losses: {len(trades) - len(wins)}")
    print(f"   Win Rate: {win_rate:.1f}%")
    print(f"   Total PnL: ${total_pnl:+.2f}")
    
    analyze_by_strategy(trades)
    analyze_by_exit_reason(trades)
    analyze_by_symbol(trades)
    analyze_by_hour(trades)
    suggest_improvements(trades)
    
    print(f"\n{'='*60}\n")


if __name__ == "__main__":
    main()
