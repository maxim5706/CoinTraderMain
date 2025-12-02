#!/usr/bin/env python3
"""
Analyze a PAPER trading session from logs.
Computes the 6 key metrics + gate funnel + exit reasons.

Usage:
    uv run python tools/analyze_session.py [--date YYYY-MM-DD]
"""

import json
import sys
from pathlib import Path
from datetime import datetime, date
from collections import defaultdict

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOGS_DIR = PROJECT_ROOT / "logs"


def load_jsonl(filepath: Path) -> list[dict]:
    """Load all records from a JSONL file."""
    if not filepath.exists():
        return []
    records = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def analyze_trades(date_str: str) -> dict:
    """Analyze trades for a given date."""
    trades_file = LOGS_DIR / f"trades_{date_str}.jsonl"
    records = load_jsonl(trades_file)
    
    # Filter to trade_close events
    closes = [r for r in records if r.get("type") == "trade_close"]
    entries = [r for r in records if r.get("type") == "order_intent"]
    
    if not closes:
        return {"error": "No completed trades found"}
    
    # Basic stats
    wins = [t for t in closes if t.get("pnl", 0) > 0]
    losses = [t for t in closes if t.get("pnl", 0) <= 0]
    
    win_rate = len(wins) / len(closes) if closes else 0
    
    # R multiples
    r_multiples = [t.get("r_multiple", 0) for t in closes]
    win_rs = [t.get("r_multiple", 0) for t in wins]
    loss_rs = [t.get("r_multiple", 0) for t in losses]
    
    avg_win_r = sum(win_rs) / len(win_rs) if win_rs else 0
    avg_loss_r = sum(loss_rs) / len(loss_rs) if loss_rs else 0
    
    # Profit factor
    gross_wins = sum(t.get("pnl", 0) for t in wins)
    gross_losses = abs(sum(t.get("pnl", 0) for t in losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')
    
    # Exit reasons histogram
    exit_reasons = defaultdict(int)
    for t in closes:
        reason = t.get("exit_reason", "unknown")
        # Normalize thesis_invalid variants
        if reason.startswith("thesis_invalid"):
            reason = "thesis_invalid"
        exit_reasons[reason] += 1
    
    # Score distribution on entries
    scores = [e.get("score_total", 0) for e in entries]
    
    # Hold times
    hold_times = [t.get("hold_minutes", 0) for t in closes]
    
    return {
        "date": date_str,
        "total_trades": len(closes),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "profit_factor": profit_factor,
        "total_pnl": sum(t.get("pnl", 0) for t in closes),
        "avg_r": sum(r_multiples) / len(r_multiples) if r_multiples else 0,
        "exit_reasons": dict(exit_reasons),
        "avg_hold_min": sum(hold_times) / len(hold_times) if hold_times else 0,
        "avg_entry_score": sum(scores) / len(scores) if scores else 0,
    }


def analyze_rejections(date_str: str) -> dict:
    """Analyze gate funnel from rejections log."""
    rej_file = LOGS_DIR / f"rejections_{date_str}.jsonl"
    records = load_jsonl(rej_file)
    
    if not records:
        return {"error": "No rejection data found"}
    
    # Count by gate
    gates = defaultdict(int)
    for r in records:
        gate = r.get("gate", "unknown")
        gates[gate] += 1
    
    total = sum(gates.values())
    
    # Calculate percentages
    gate_pcts = {g: (c / total * 100) for g, c in gates.items()}
    
    return {
        "total_rejections": total,
        "by_gate": dict(gates),
        "pct_by_gate": gate_pcts,
    }


def analyze_health(date_str: str) -> dict:
    """Analyze health snapshots."""
    health_file = LOGS_DIR / f"health_{date_str}.jsonl"
    records = load_jsonl(health_file)
    
    if not records:
        return {"error": "No health data found"}
    
    # Get stats over time
    ml_fresh = [r.get("ml_fresh_pct", 0) for r in records]
    warm = [r.get("warm_symbols", 0) for r in records]
    
    return {
        "snapshots": len(records),
        "avg_ml_fresh_pct": sum(ml_fresh) / len(ml_fresh) if ml_fresh else 0,
        "avg_warm_symbols": sum(warm) / len(warm) if warm else 0,
        "final_ml_fresh": ml_fresh[-1] if ml_fresh else 0,
        "final_warm": warm[-1] if warm else 0,
    }


def print_report(trade_stats: dict, rejection_stats: dict, health_stats: dict):
    """Print formatted analysis report."""
    print("\n" + "=" * 60)
    print(f"📊 SESSION ANALYSIS: {trade_stats.get('date', 'unknown')}")
    print("=" * 60)
    
    if "error" in trade_stats:
        print(f"\n❌ {trade_stats['error']}")
    else:
        print("\n🎯 THE 6 KEY METRICS")
        print("-" * 40)
        print(f"  1. Win Rate:        {trade_stats['win_rate']*100:.1f}%  {'✅' if trade_stats['win_rate'] >= 0.4 else '⚠️'}")
        print(f"  2. Avg Win (R):     {trade_stats['avg_win_r']:+.2f}R")
        print(f"  3. Avg Loss (R):    {trade_stats['avg_loss_r']:+.2f}R")
        
        pf = trade_stats['profit_factor']
        pf_str = f"{pf:.2f}" if pf < 100 else "∞"
        print(f"  4. Profit Factor:   {pf_str}  {'✅' if pf >= 1.2 else '⚠️'}")
        
        ratio = abs(trade_stats['avg_win_r'] / trade_stats['avg_loss_r']) if trade_stats['avg_loss_r'] != 0 else 0
        print(f"  5. Win/Loss Ratio:  {ratio:.2f}  {'✅' if ratio >= 1.5 else '⚠️'}")
        print(f"  6. Avg R Multiple:  {trade_stats['avg_r']:+.2f}R")
        
        print(f"\n📈 SUMMARY")
        print("-" * 40)
        print(f"  Trades: {trade_stats['total_trades']} ({trade_stats['wins']}W / {trade_stats['losses']}L)")
        print(f"  Total PnL: ${trade_stats['total_pnl']:+.2f}")
        print(f"  Avg Hold: {trade_stats['avg_hold_min']:.1f} min")
        print(f"  Avg Entry Score: {trade_stats['avg_entry_score']:.0f}/100")
        
        print(f"\n🚪 EXIT REASONS")
        print("-" * 40)
        for reason, count in sorted(trade_stats['exit_reasons'].items(), key=lambda x: -x[1]):
            pct = count / trade_stats['total_trades'] * 100
            print(f"  {reason}: {count} ({pct:.0f}%)")
    
    if "error" not in rejection_stats:
        print(f"\n🔒 GATE FUNNEL")
        print("-" * 40)
        print(f"  Total Rejections: {rejection_stats['total_rejections']}")
        for gate, count in sorted(rejection_stats['by_gate'].items(), key=lambda x: -x[1]):
            pct = rejection_stats['pct_by_gate'][gate]
            bar = "█" * int(pct / 5)
            flag = "⚠️" if pct > 70 else ""
            print(f"  {gate:12} {count:4} ({pct:5.1f}%) {bar} {flag}")
    
    if "error" not in health_stats:
        print(f"\n🏥 SYSTEM HEALTH")
        print("-" * 40)
        print(f"  Snapshots: {health_stats['snapshots']}")
        print(f"  Avg ML Fresh: {health_stats['avg_ml_fresh_pct']:.1f}%")
        print(f"  Avg Warm Symbols: {health_stats['avg_warm_symbols']:.1f}")
        print(f"  Final ML Fresh: {health_stats['final_ml_fresh']:.1f}%")
        print(f"  Final Warm: {health_stats['final_warm']}")
    
    print("\n" + "=" * 60)
    
    # Recommendations
    print("\n💡 RECOMMENDATIONS")
    print("-" * 40)
    
    if "error" not in trade_stats:
        if trade_stats['win_rate'] < 0.35:
            print("  • Win rate low → tighten entry filters or chop detection")
        if trade_stats['profit_factor'] < 1.2 and trade_stats['win_rate'] >= 0.4:
            print("  • PF low but WR ok → winners too small, loosen trail")
        if trade_stats['avg_loss_r'] < -1.5:
            print("  • Large losses → consider tighter stops or faster thesis exit")
        
        exits = trade_stats['exit_reasons']
        if exits.get('stop', 0) > trade_stats['total_trades'] * 0.5:
            print("  • 50%+ stopped out → entries too loose or stops too tight")
        if exits.get('thesis_invalid', 0) > trade_stats['total_trades'] * 0.4:
            print("  • 40%+ thesis exits → trend filter may be too eager")
    
    if "error" not in rejection_stats:
        pcts = rejection_stats['pct_by_gate']
        if pcts.get('warmth', 0) > 70:
            print("  • 70%+ warmth blocks → check candle history / warm threshold")
        if pcts.get('regime', 0) > 50:
            print("  • 50%+ regime blocks → regime detector may be too sensitive")
        if pcts.get('rr', 0) > 40:
            print("  • 40%+ R:R blocks → stop/TP geometry doesn't match patterns")
    
    print()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Analyze trading session")
    parser.add_argument("--date", type=str, help="Date to analyze (YYYY-MM-DD)", 
                       default=date.today().isoformat())
    args = parser.parse_args()
    
    date_str = args.date
    
    print(f"\nAnalyzing session for {date_str}...")
    
    trade_stats = analyze_trades(date_str)
    rejection_stats = analyze_rejections(date_str)
    health_stats = analyze_health(date_str)
    
    print_report(trade_stats, rejection_stats, health_stats)


if __name__ == "__main__":
    main()
