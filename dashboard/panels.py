"""
Dashboard Panels - Individual UI components.

Each function renders a specific panel of the dashboard.
Clean separation of concerns for easy maintenance.
"""

from datetime import datetime, timezone
from typing import Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.state import BotState


def render_top_bar(state: BotState) -> Text:
    """Render the status bar at top of dashboard."""
    bar = Text()
    
    # Mode
    mode_color = "yellow" if state.mode == "paper" else "green bold"
    bar.append("MODE: ", style="dim")
    bar.append(f"{state.mode.upper()}", style=mode_color)
    bar.append(" │ ")
    
    # Portfolio value
    if state.mode == "paper":
        total = state.paper_balance_usd or (state.paper_balance + state.paper_positions_value)
        bar.append(f"📝 ${total:.2f}", style="bold yellow")
    else:
        bar.append(f"💰 ${state.portfolio_value:.2f}", style="bold white")
        bar.append(f" (${state.cash_balance:.0f} + ${state.holdings_value:.0f})", style="dim")
    bar.append(" │ ")
    
    # Sync status
    bar.append("SYNC: ", style="dim")
    age = getattr(state, "portfolio_snapshot_age_s", 999.0)
    if age < 15:
        bar.append("OK", style="green")
    else:
        bar.append(f"STALE ({age:.0f}s)", style="yellow")
    bar.append(" │ ")
    
    # WebSocket
    bar.append("WS: ", style="dim")
    if state.ws_ok and state.ws_last_age < 5:
        bar.append("✅", style="green")
    elif state.ws_ok:
        bar.append("⚠️", style="yellow")
    else:
        bar.append("❌", style="red")
    bar.append(" │ ")
    
    # BTC regime
    bar.append("BTC: ", style="dim")
    btc_trend = getattr(state, "btc_1h_trend", 0)
    if btc_trend > 0.5:
        bar.append(f"🟢 +{btc_trend:.1f}%", style="green")
    elif btc_trend < -1:
        bar.append(f"🔴 {btc_trend:.1f}%", style="red")
    else:
        bar.append(f"🟡 {btc_trend:+.1f}%", style="yellow")
    bar.append(" │ ")
    
    # Time
    bar.append(datetime.now(timezone.utc).strftime("%H:%M:%S"), style="dim")
    
    return bar


def render_scanner_panel(state: BotState) -> Panel:
    """Render scanner showing top signals."""
    table = Table(box=None, padding=(0, 1), expand=True)
    table.add_column("Symbol", style="cyan", width=8)
    table.add_column("Strat", style="yellow", width=8)
    table.add_column("Score", justify="right", width=5)
    table.add_column("Vol", justify="right", width=6)
    table.add_column("Trend", justify="right", width=7)
    
    # Show top candidates from scanner
    candidates = list(getattr(state, "scanner_candidates", []))[:10]
    for c in candidates:
        symbol = getattr(c, "symbol", "?").replace("-USD", "")
        strat = getattr(c, "strategy_id", "?")[:8]
        score = getattr(c, "score", 0)
        vol = getattr(c, "vol_spike", 1)
        trend = getattr(c, "trend_5m", 0)
        
        score_style = "green" if score >= 80 else "yellow" if score >= 70 else "dim"
        vol_str = f"{vol:.0f}x" if vol < 1000 else f"{vol/1000:.0f}kx"
        trend_str = f"{trend:+.1f}%"
        
        table.add_row(
            symbol,
            strat,
            f"[{score_style}]{score}[/]",
            vol_str,
            trend_str,
        )
    
    if not candidates:
        table.add_row("[dim]No signals[/]", "", "", "", "")
    
    return Panel(table, title="[bold cyan]📡 Scanner[/]", border_style="cyan")


def render_positions_panel(state: BotState) -> Panel:
    """Render current positions."""
    table = Table(box=None, padding=(0, 1), expand=True)
    table.add_column("Sym", style="cyan", width=6)
    table.add_column("$", justify="right", width=5)
    table.add_column("P&L", justify="right", width=7)
    table.add_column("Conf", justify="right", width=4)
    
    positions = list(getattr(state, "position_displays", []))[:7]
    for p in positions:
        symbol = getattr(p, "symbol", "?").replace("-USD", "")
        value = getattr(p, "value_usd", 0)
        pnl_pct = getattr(p, "pnl_pct", 0)
        conf = getattr(p, "confidence", 70)
        
        pnl_style = "green" if pnl_pct > 0 else "red"
        pnl_str = f"[{pnl_style}]{pnl_pct:+.1f}%[/]"
        
        table.add_row(symbol, f"${value:.0f}", pnl_str, f"{conf}%")
    
    count = len(positions)
    total_value = sum(getattr(p, "value_usd", 0) for p in positions)
    
    return Panel(
        table,
        title=f"[bold green]📊 Positions ({count})[/]",
        subtitle=f"[dim]${total_value:.0f}[/]",
        border_style="green",
    )


def render_signal_panel(state: BotState) -> Panel:
    """Render current signal being evaluated."""
    sig = getattr(state, "current_signal", None)
    if not sig or not sig.symbol:
        return Panel("[dim]No active signal[/]", title="[bold yellow]⚡ Signal[/]", border_style="yellow")
    
    lines = []
    
    # Signal type and confidence
    action = getattr(sig, "signal_type", "?")
    conf = getattr(sig, "confidence", 0)
    lines.append(f"[bold]{action}[/] @ {conf:.0f}% conf")
    lines.append("")
    
    # Symbol and strategy
    lines.append(f"[cyan]{sig.symbol}[/] - {getattr(sig, 'strategy_id', '?')}")
    
    # Price levels
    entry = getattr(sig, "entry_price", 0)
    stop = getattr(sig, "stop_price", 0)
    tp1 = getattr(sig, "tp1_price", 0)
    
    lines.append("")
    lines.append(f"Entry: [white]${entry:.4f}[/]")
    lines.append(f"Stop:  [red]${stop:.4f}[/]")
    lines.append(f"TP1:   [green]${tp1:.4f}[/]")
    
    # R:R ratio
    if entry and stop and tp1:
        risk = entry - stop
        reward = tp1 - entry
        rr = reward / risk if risk > 0 else 0
        lines.append(f"R:R:   [white]{rr:.1f}x[/]")
    
    return Panel("\n".join(lines), title="[bold yellow]⚡ Signal[/]", border_style="yellow")


def render_orders_panel(state: BotState) -> Panel:
    """Render recent orders."""
    lines = []
    
    orders = list(getattr(state, "recent_orders", []))[:10]
    for o in orders:
        ts = getattr(o, "ts", datetime.now()).strftime("%H:%M:%S")
        symbol = getattr(o, "symbol", "?").replace("-USD", "")
        side = getattr(o, "side", "?")
        size = getattr(o, "size_usd", 0)
        price = getattr(o, "price", 0)
        
        side_icon = "🟢" if side == "buy" else "🔴"
        lines.append(f"{ts} {side_icon} {symbol} ${size:.0f} @ ${price:.4f}")
    
    if not orders:
        lines.append("[dim]No recent orders[/]")
    
    return Panel("\n".join(lines), title="[bold blue]📋 Orders[/]", border_style="blue")


def render_stats_panel(state: BotState) -> Panel:
    """Render daily trading stats."""
    ds = getattr(state, "daily_stats", None)
    
    lines = []
    lines.append(f"Trades: {getattr(ds, 'trades', 0) if ds else 0}")
    
    if ds and ds.trades > 0:
        lines.append(f"Win Rate: {ds.win_rate * 100:.0f}%")
        lines.append(f"P&L: ${ds.total_pnl:+.2f}")
        pf = ds.profit_factor
        pf_str = f"{pf:.1f}" if pf < 100 else "∞"
        lines.append(f"PF: {pf_str}")
    else:
        lines.append("[dim]No trades yet[/]")
    
    return Panel("\n".join(lines), title="[bold magenta]📈 Today[/]", border_style="magenta")


def render_sanity_panel(state: BotState) -> Panel:
    """Render system health checks."""
    lines = []
    
    # Runtime
    runtime_s = getattr(state, "runtime_seconds", 0)
    runtime_m = runtime_s // 60
    lines.append(f"Runtime: {runtime_m}m")
    
    # Universe
    eligible = getattr(state, "universe_eligible", 0)
    warm = getattr(state, "warm_symbols", 0)
    lines.append(f"Universe: {eligible} ({warm} warm)")
    
    # Budget
    used = getattr(state, "budget_used", 0)
    total = getattr(state, "budget_total", 0)
    lines.append(f"Budget: ${used:.0f}/${total:.0f}")
    
    # WS health
    ws_age = getattr(state, "ws_last_age", 999)
    if ws_age < 5:
        lines.append(f"[green]WS: {ws_age:.1f}s[/]")
    else:
        lines.append(f"[yellow]WS: {ws_age:.1f}s[/]")
    
    return Panel("\n".join(lines), title="[bold]🔍 Health[/]", border_style="dim")
