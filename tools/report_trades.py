#!/usr/bin/env python3
"""
Trade Statistics Report Generator

Analyzes trade logs to compute compounding metrics:
- Win rate, avg win/loss, profit factor
- Equity curve and max drawdown
- FAST vs NORMAL trade comparison
- Daily summaries

Usage:
    python tools/report_trades.py [--days N] [--output html]
"""

import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from collections import defaultdict


@dataclass
class Trade:
    """Parsed trade from logs."""
    symbol: str
    entry_time: datetime
    exit_time: Optional[datetime] = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    size_usd: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    is_fast: bool = False
    
    @property
    def is_win(self) -> bool:
        return self.pnl > 0
    
    @property
    def hold_minutes(self) -> float:
        if self.exit_time and self.entry_time:
            return (self.exit_time - self.entry_time).total_seconds() / 60
        return 0


@dataclass
class TradeStats:
    """Computed statistics for a set of trades."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_win_pnl: float = 0.0
    total_loss_pnl: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    avg_hold_min: float = 0.0
    
    # For equity curve
    equity_curve: List[float] = field(default_factory=list)
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    
    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0
    
    @property
    def avg_win(self) -> float:
        return self.total_win_pnl / self.wins if self.wins > 0 else 0.0
    
    @property
    def avg_loss(self) -> float:
        return self.total_loss_pnl / self.losses if self.losses > 0 else 0.0
    
    @property
    def profit_factor(self) -> float:
        return self.total_win_pnl / self.total_loss_pnl if self.total_loss_pnl > 0 else float('inf')
    
    @property
    def expectancy(self) -> float:
        """Expected $ per trade."""
        return self.total_pnl / self.total_trades if self.total_trades > 0 else 0.0
    
    @property
    def rr_ratio(self) -> float:
        """Risk/Reward = avg win / avg loss."""
        return self.avg_win / self.avg_loss if self.avg_loss > 0 else float('inf')


def load_trades(logs_dir: Path, days: int = 30) -> List[Trade]:
    """Load trades from JSONL log files."""
    trades = []
    cutoff = datetime.now() - timedelta(days=days)
    
    # Find all trade log files
    trade_files = list(logs_dir.glob("trades_*.jsonl"))
    
    # Also check for trade_close entries in older format
    for log_file in sorted(trade_files):
        try:
            with open(log_file, 'r') as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        
                        # Parse trade_close records
                        if record.get('type') == 'trade_close':
                            ts_str = record.get('ts', '')
                            try:
                                exit_time = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                            except:
                                continue
                            
                            if exit_time.replace(tzinfo=None) < cutoff:
                                continue
                            
                            trade = Trade(
                                symbol=record.get('symbol', ''),
                                exit_time=exit_time,
                                entry_price=record.get('entry_price', 0),
                                exit_price=record.get('exit_price', 0),
                                size_usd=record.get('size_usd', 0),
                                pnl=record.get('pnl', 0),
                                pnl_pct=record.get('pnl_pct', 0),
                                exit_reason=record.get('exit_reason', ''),
                                is_fast='fast' in record.get('exit_reason', '').lower()
                            )
                            trades.append(trade)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"Error reading {log_file}: {e}")
    
    return sorted(trades, key=lambda t: t.exit_time or datetime.min)


def compute_stats(trades: List[Trade]) -> TradeStats:
    """Compute statistics from trades."""
    stats = TradeStats()
    
    if not trades:
        return stats
    
    stats.total_trades = len(trades)
    equity = 0.0
    
    for trade in trades:
        equity += trade.pnl
        stats.equity_curve.append(equity)
        
        if trade.is_win:
            stats.wins += 1
            stats.total_win_pnl += trade.pnl
            stats.biggest_win = max(stats.biggest_win, trade.pnl)
        else:
            stats.losses += 1
            stats.total_loss_pnl += abs(trade.pnl)
            stats.biggest_loss = max(stats.biggest_loss, abs(trade.pnl))
        
        stats.total_pnl += trade.pnl
        
        # Track drawdown
        stats.peak_equity = max(stats.peak_equity, equity)
        current_dd = stats.peak_equity - equity
        stats.max_drawdown = max(stats.max_drawdown, current_dd)
        if stats.peak_equity > 0:
            dd_pct = current_dd / stats.peak_equity * 100
            stats.max_drawdown_pct = max(stats.max_drawdown_pct, dd_pct)
    
    # Average hold time
    hold_times = [t.hold_minutes for t in trades if t.hold_minutes > 0]
    stats.avg_hold_min = sum(hold_times) / len(hold_times) if hold_times else 0
    
    return stats


def print_report(stats: TradeStats, title: str = "TRADE STATISTICS"):
    """Print formatted report."""
    
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")
    
    # Basic stats
    print(f"  Total Trades:    {stats.total_trades}")
    print(f"  Wins/Losses:     {stats.wins}W / {stats.losses}L")
    print(f"  Win Rate:        {stats.win_rate*100:.1f}%")
    print()
    
    # PnL stats
    pnl_color = '\033[92m' if stats.total_pnl >= 0 else '\033[91m'
    reset = '\033[0m'
    print(f"  Total PnL:       {pnl_color}${stats.total_pnl:+.2f}{reset}")
    print(f"  Avg Win:         ${stats.avg_win:.2f}")
    print(f"  Avg Loss:        ${stats.avg_loss:.2f}")
    print(f"  Biggest Win:     ${stats.biggest_win:.2f}")
    print(f"  Biggest Loss:    ${stats.biggest_loss:.2f}")
    print()
    
    # Quality metrics
    pf = stats.profit_factor
    pf_str = f"{pf:.2f}" if pf < 100 else "∞"
    pf_color = '\033[92m' if pf >= 1.5 else '\033[93m' if pf >= 1.0 else '\033[91m'
    print(f"  Profit Factor:   {pf_color}{pf_str}{reset}")
    
    rr = stats.rr_ratio
    rr_str = f"{rr:.2f}" if rr < 100 else "∞"
    rr_color = '\033[92m' if rr >= 1.5 else '\033[93m' if rr >= 1.0 else '\033[91m'
    print(f"  R:R Ratio:       {rr_color}{rr_str}{reset}")
    
    print(f"  Expectancy:      ${stats.expectancy:+.2f}/trade")
    print()
    
    # Risk metrics
    print(f"  Max Drawdown:    ${stats.max_drawdown:.2f} ({stats.max_drawdown_pct:.1f}%)")
    print(f"  Avg Hold Time:   {stats.avg_hold_min:.1f} min")
    print()
    
    # Equity curve sparkline
    if stats.equity_curve:
        min_eq = min(stats.equity_curve)
        max_eq = max(stats.equity_curve)
        range_eq = max_eq - min_eq if max_eq != min_eq else 1
        
        bars = "▁▂▃▄▅▆▇█"
        sparkline = ""
        step = max(1, len(stats.equity_curve) // 40)
        for i in range(0, len(stats.equity_curve), step):
            val = stats.equity_curve[i]
            idx = int((val - min_eq) / range_eq * (len(bars) - 1))
            sparkline += bars[idx]
        
        print(f"  Equity Curve:    {sparkline}")
    
    print(f"\n{'='*60}\n")


def print_daily_breakdown(trades: List[Trade]):
    """Print daily trade breakdown."""
    
    daily: Dict[str, List[Trade]] = defaultdict(list)
    for trade in trades:
        if trade.exit_time:
            day = trade.exit_time.strftime('%Y-%m-%d')
            daily[day].append(trade)
    
    print("\n  DAILY BREAKDOWN")
    print("  " + "-"*56)
    print(f"  {'Date':<12} {'Trades':>7} {'W/L':>8} {'PnL':>10} {'PF':>7}")
    print("  " + "-"*56)
    
    for day in sorted(daily.keys()):
        day_trades = daily[day]
        day_stats = compute_stats(day_trades)
        pf_str = f"{day_stats.profit_factor:.2f}" if day_stats.profit_factor < 100 else "∞"
        pnl_str = f"${day_stats.total_pnl:+.2f}"
        print(f"  {day:<12} {day_stats.total_trades:>7} {day_stats.wins}W/{day_stats.losses}L{'':<3} {pnl_str:>10} {pf_str:>7}")
    
    print("  " + "-"*56)


def main():
    parser = argparse.ArgumentParser(description='Trade Statistics Report')
    parser.add_argument('--days', type=int, default=30, help='Days of history to analyze')
    parser.add_argument('--logs', type=str, default='logs', help='Logs directory')
    args = parser.parse_args()
    
    logs_dir = Path(args.logs)
    if not logs_dir.exists():
        print(f"Logs directory not found: {logs_dir}")
        return
    
    print(f"\nLoading trades from {logs_dir} (last {args.days} days)...")
    trades = load_trades(logs_dir, args.days)
    
    if not trades:
        print("No trades found in logs.")
        return
    
    print(f"Found {len(trades)} trades")
    
    # Overall stats
    overall = compute_stats(trades)
    print_report(overall, "📊 OVERALL STATISTICS")
    
    # Daily breakdown
    print_daily_breakdown(trades)
    
    # FAST vs NORMAL comparison
    fast_trades = [t for t in trades if t.is_fast]
    normal_trades = [t for t in trades if not t.is_fast]
    
    if fast_trades:
        fast_stats = compute_stats(fast_trades)
        print_report(fast_stats, "⚡ FAST MODE TRADES")
    
    if normal_trades:
        normal_stats = compute_stats(normal_trades)
        print_report(normal_stats, "📈 NORMAL MODE TRADES")
    
    # Recommendations
    print("\n  💡 RECOMMENDATIONS")
    print("  " + "-"*56)
    
    if overall.profit_factor < 1.0:
        print("  ⚠️  Profit factor < 1.0 - review entry criteria")
    if overall.rr_ratio < 1.5:
        print("  ⚠️  R:R ratio < 1.5 - widen TP or tighten stops")
    if overall.win_rate < 0.4:
        print("  ⚠️  Win rate < 40% - improve signal quality")
    if overall.max_drawdown_pct > 20:
        print("  ⚠️  Max DD > 20% - reduce position sizing")
    if overall.avg_hold_min < 5:
        print("  ⚠️  Avg hold < 5min - might be overtrading")
    
    if overall.profit_factor >= 1.5 and overall.win_rate >= 0.5:
        print("  ✅ System is profitable - focus on consistency")
    
    print()


if __name__ == "__main__":
    main()
