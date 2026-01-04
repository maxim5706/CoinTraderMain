#!/usr/bin/env python3
"""
Performance Aggregator - Analyze what's working vs not

Run: python scripts/analyze_performance.py [--days 1]
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict

LOGS_DIR = Path(__file__).parent.parent / "logs" / "live"
DATA_DIR = Path(__file__).parent.parent / "data"


def load_trades(days: int = 1) -> list:
    """Load trade logs from the last N days."""
    trades = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    for log_file in sorted(LOGS_DIR.glob("trades_*.jsonl"), reverse=True):
        try:
            with open(log_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        trade = json.loads(line)
                        # Parse timestamp
                        ts_str = trade.get("ts") or trade.get("timestamp", "")
                        if ts_str:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            if ts >= cutoff:
                                trades.append(trade)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception as e:
            print(f"Error reading {log_file}: {e}")
    
    return trades


def load_strategy_registry() -> dict:
    """Load strategy registry with stats."""
    registry_file = DATA_DIR / "strategy_registry.json"
    if not registry_file.exists():
        return {}
    
    try:
        with open(registry_file) as f:
            return json.load(f)
    except Exception:
        return {}


def load_blocked_signals(days: int = 1) -> list:
    """Load blocked signal events."""
    events = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    for log_file in sorted(LOGS_DIR.glob("events_*.jsonl"), reverse=True):
        try:
            with open(log_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        if event.get("type") == "signal_blocked":
                            ts_str = event.get("ts", "")
                            if ts_str:
                                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                if ts >= cutoff:
                                    events.append(event)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception:
            continue
    
    return events


def analyze_by_strategy(trades: list) -> dict:
    """Aggregate performance by strategy."""
    stats = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0,
        "total_pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0,
        "avg_hold_min": 0.0, "hold_times": []
    })
    
    for trade in trades:
        if trade.get("type") != "trade_close":
            continue
        
        strategy = trade.get("strategy_id", "unknown")
        pnl = float(trade.get("pnl", 0))
        hold_min = float(trade.get("hold_minutes", 0))
        
        s = stats[strategy]
        s["trades"] += 1
        s["total_pnl"] += pnl
        s["hold_times"].append(hold_min)
        
        if pnl > 0:
            s["wins"] += 1
            s["gross_profit"] += pnl
        else:
            s["losses"] += 1
            s["gross_loss"] += abs(pnl)
    
    # Calculate averages
    for strategy, s in stats.items():
        if s["trades"] > 0:
            s["win_rate"] = (s["wins"] / s["trades"]) * 100
            s["avg_pnl"] = s["total_pnl"] / s["trades"]
            s["avg_hold_min"] = sum(s["hold_times"]) / len(s["hold_times"])
            s["profit_factor"] = s["gross_profit"] / s["gross_loss"] if s["gross_loss"] > 0 else float('inf')
        del s["hold_times"]
    
    return dict(stats)


def analyze_blocked_reasons(events: list) -> dict:
    """Aggregate blocked signal reasons."""
    reasons = defaultdict(int)
    by_symbol = defaultdict(int)
    
    for event in events:
        reason = event.get("reason", "unknown")
        symbol = event.get("symbol", "unknown")
        reasons[reason] += 1
        by_symbol[symbol] += 1
    
    return {
        "by_reason": dict(sorted(reasons.items(), key=lambda x: -x[1])),
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda x: -x[1])[:10])
    }


def analyze_exit_reasons(trades: list) -> dict:
    """Aggregate exit reasons."""
    reasons = defaultdict(lambda: {"count": 0, "total_pnl": 0.0, "wins": 0})
    
    for trade in trades:
        if trade.get("type") != "trade_close":
            continue
        
        reason = trade.get("exit_reason", "unknown")
        pnl = float(trade.get("pnl", 0))
        
        reasons[reason]["count"] += 1
        reasons[reason]["total_pnl"] += pnl
        if pnl > 0:
            reasons[reason]["wins"] += 1
    
    # Calculate win rates
    for r in reasons.values():
        r["win_rate"] = (r["wins"] / r["count"] * 100) if r["count"] > 0 else 0
    
    return dict(sorted(reasons.items(), key=lambda x: -x[1]["count"]))


def print_report(days: int = 1):
    """Print full performance report."""
    print(f"\n{'='*60}")
    print(f"  PERFORMANCE REPORT - Last {days} day(s)")
    print(f"{'='*60}\n")
    
    # Load data
    trades = load_trades(days)
    registry = load_strategy_registry()
    blocked = load_blocked_signals(days)
    
    # Overall stats
    closed_trades = [t for t in trades if t.get("type") == "trade_close"]
    total_pnl = sum(float(t.get("pnl", 0)) for t in closed_trades)
    wins = sum(1 for t in closed_trades if float(t.get("pnl", 0)) > 0)
    losses = len(closed_trades) - wins
    
    print("📊 OVERALL SUMMARY")
    print("-" * 40)
    print(f"  Closed trades: {len(closed_trades)}")
    print(f"  Win/Loss: {wins}W / {losses}L ({wins/(len(closed_trades))*100:.1f}% win rate)" if closed_trades else "  No trades")
    print(f"  Total P&L: ${total_pnl:+.2f}")
    print(f"  Blocked signals: {len(blocked)}")
    print()
    
    # Strategy breakdown
    strat_stats = analyze_by_strategy(trades)
    if strat_stats:
        print("📈 BY STRATEGY")
        print("-" * 40)
        print(f"  {'Strategy':<20} {'Trades':>6} {'Win%':>7} {'P&L':>10} {'PF':>6}")
        print(f"  {'-'*20} {'-'*6} {'-'*7} {'-'*10} {'-'*6}")
        
        for strat, s in sorted(strat_stats.items(), key=lambda x: -x[1]["total_pnl"]):
            pf = f"{s.get('profit_factor', 0):.2f}" if s.get('profit_factor', 0) < 100 else "∞"
            print(f"  {strat:<20} {s['trades']:>6} {s.get('win_rate', 0):>6.1f}% ${s['total_pnl']:>+8.2f} {pf:>6}")
        print()
    
    # Exit reasons
    exit_stats = analyze_exit_reasons(trades)
    if exit_stats:
        print("🚪 EXIT REASONS")
        print("-" * 40)
        print(f"  {'Reason':<25} {'Count':>6} {'Win%':>7} {'P&L':>10}")
        print(f"  {'-'*25} {'-'*6} {'-'*7} {'-'*10}")
        
        for reason, s in list(exit_stats.items())[:10]:
            print(f"  {reason:<25} {s['count']:>6} {s['win_rate']:>6.1f}% ${s['total_pnl']:>+8.2f}")
        print()
    
    # Blocked signals
    if blocked:
        blocked_stats = analyze_blocked_reasons(blocked)
        print("🚫 BLOCKED SIGNALS")
        print("-" * 40)
        print("  Top reasons:")
        for reason, count in list(blocked_stats["by_reason"].items())[:5]:
            print(f"    {reason}: {count}")
        print()
        print("  Top blocked symbols:")
        for symbol, count in list(blocked_stats["by_symbol"].items())[:5]:
            print(f"    {symbol}: {count}")
        print()
    
    # Registry stats (lifetime)
    if registry.get("strategies"):
        print("📚 STRATEGY REGISTRY (lifetime)")
        print("-" * 40)
        print(f"  {'Strategy':<20} {'On':>4} {'Trades':>7} {'Win%':>7} {'P&L':>10}")
        print(f"  {'-'*20} {'-'*4} {'-'*7} {'-'*7} {'-'*10}")
        
        for name, cfg in sorted(registry["strategies"].items(), key=lambda x: -x[1].get("priority", 0)):
            stats = cfg.get("stats", {})
            enabled = "✓" if cfg.get("enabled") else "✗"
            trades_count = stats.get("total_trades", 0)
            win_rate = stats.get("win_rate", 0)
            pnl = stats.get("total_pnl", 0)
            print(f"  {name:<20} {enabled:>4} {trades_count:>7} {win_rate:>6.1f}% ${pnl:>+8.2f}")
        print()
    
    print("=" * 60)
    print("  Run with --days N to analyze more history")
    print("=" * 60)


if __name__ == "__main__":
    days = 1
    if len(sys.argv) > 1:
        if sys.argv[1] == "--days" and len(sys.argv) > 2:
            days = int(sys.argv[2])
        else:
            try:
                days = int(sys.argv[1])
            except ValueError:
                pass
    
    print_report(days)
