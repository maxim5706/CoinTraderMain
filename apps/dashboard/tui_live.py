"""
Live Textual TUI Dashboard - Integrated with trading bot.

This version is designed to run alongside the async trading bot
and receive real-time state updates.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Static, DataTable, RichLog, Label
from textual.timer import Timer

from core.state import BotState


class LiveStatusBar(Static):
    """Top status bar - updates from bot state."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        s = self.bot_state
        mode = s.mode.upper() if s.mode else "?"
        mode_color = "yellow" if mode == "PAPER" else "green"
        
        portfolio = s.portfolio_value or 0
        cash = s.cash_balance or 0
        holdings = s.holdings_value or 0
        
        btc = getattr(s, "btc_1h_trend", 0) or 0
        btc_color = "green" if btc > 0 else "red" if btc < -1 else "yellow"
        
        ws = "✅" if s.ws_ok else "❌"
        ws_age = s.ws_last_age or 0
        
        sync_age = getattr(s, "portfolio_snapshot_age_s", 999)
        sync = f"[green]OK[/]" if sync_age < 15 else f"[yellow]{sync_age:.0f}s[/]"
        
        return (
            f"[{mode_color} bold]{mode}[/] │ "
            f"💰 ${portfolio:.2f} (${cash:.0f}+${holdings:.0f}) │ "
            f"BTC: [{btc_color}]{btc:+.1f}%[/] │ "
            f"WS: {ws} {ws_age:.0f}s │ "
            f"Sync: {sync} │ "
            f"[dim]{datetime.now(timezone.utc).strftime('%H:%M:%S')}[/]"
        )


class LiveScanner(Static):
    """Scanner panel showing hot candidates."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        lines = []
        # Use burst_leaderboard (same as old dashboard)
        candidates = list(getattr(self.bot_state, "burst_leaderboard", []))[:8]
        
        if not candidates:
            return "[dim]Scanning...[/]"
        
        for c in candidates:
            sym = getattr(c, "symbol", "?").replace("-USD", "")[:6]
            score = getattr(c, "entry_score", 0) or getattr(c, "score", 0)
            strat = getattr(c, "strategy", "")[:6] or getattr(c, "strategy_id", "?")[:6]
            trend = getattr(c, "trend_5m", 0)
            
            score_color = "green" if score >= 80 else "yellow" if score >= 70 else "dim"
            trend_color = "green" if trend > 0 else "red" if trend < 0 else "dim"
            
            lines.append(
                f"[cyan]{sym:6}[/] [{score_color}]{score:3}[/] {strat:6} [{trend_color}]{trend:+.1f}%[/]"
            )
        
        return "\n".join(lines)


class LivePositions(Static):
    """Positions panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        lines = []
        # Get positions (run_v2 sets as list of PositionDisplay namedtuples)
        raw_positions = getattr(self.bot_state, "positions", []) or []
        # Handle both dict and list
        if isinstance(raw_positions, dict):
            positions = list(raw_positions.values())[:6]
        else:
            positions = list(raw_positions)[:6]
        
        if not positions:
            return "[dim]No positions[/]"
        
        total_value = 0
        total_pnl = 0
        
        for p in positions:
            sym = getattr(p, "symbol", "?").replace("-USD", "")[:6]
            # PositionDisplay has: size_usd, unrealized_pnl, unrealized_pct
            value = getattr(p, "size_usd", 0)
            pnl_pct = getattr(p, "unrealized_pct", 0)
            pnl_usd = getattr(p, "unrealized_pnl", 0)
            
            total_value += value
            total_pnl += pnl_usd
            
            pnl_color = "green" if pnl_pct > 0 else "red" if pnl_pct < 0 else "dim"
            lines.append(f"[cyan]{sym:6}[/] ${value:5.0f} [{pnl_color}]{pnl_pct:+5.1f}%[/]")
        
        lines.append(f"[dim]───────────────────[/]")
        pnl_color = "green" if total_pnl >= 0 else "red"
        lines.append(f"[bold]Total:[/] ${total_value:.0f} [{pnl_color}]${total_pnl:+.2f}[/]")
        
        return "\n".join(lines)


class LiveSignal(Static):
    """Current signal panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        sig = getattr(self.bot_state, "current_signal", None)
        
        if not sig or not getattr(sig, "symbol", None):
            return "[dim]No active signal[/]"
        
        sym = sig.symbol.replace("-USD", "")
        action = getattr(sig, "signal_type", "?")
        conf = getattr(sig, "confidence", 0)
        strat = getattr(sig, "strategy_id", "?")
        entry = getattr(sig, "entry_price", 0)
        stop = getattr(sig, "stop_price", 0)
        tp1 = getattr(sig, "tp1_price", 0)
        
        # R:R calculation
        rr = 0
        if entry and stop and entry != stop:
            risk = entry - stop
            reward = tp1 - entry if tp1 else 0
            rr = reward / risk if risk > 0 else 0
        
        return (
            f"[bold cyan]{action}[/] @ {conf:.0f}%\n"
            f"[white bold]{sym}[/] - {strat}\n"
            f"\n"
            f"Entry: [white]${entry:.4f}[/]\n"
            f"Stop:  [red]${stop:.4f}[/]\n"
            f"TP1:   [green]${tp1:.4f}[/]\n"
            f"R:R:   [yellow]{rr:.1f}x[/]"
        )


class LiveStats(Static):
    """Daily stats panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        s = self.bot_state
        trades = getattr(s, "trades_today", 0)
        wins = getattr(s, "wins_today", 0)
        losses = getattr(s, "losses_today", 0)
        pnl = getattr(s, "daily_pnl", 0)
        win_rate = getattr(s, "win_rate", 0)
        
        pnl_color = "green" if pnl >= 0 else "red"
        
        return (
            f"Trades: {trades}\n"
            f"W/L: {wins}/{losses}\n"
            f"Win%: {win_rate*100:.0f}%\n"
            f"PnL: [{pnl_color}]${pnl:+.2f}[/]"
        )


class LiveHealth(Static):
    """System health panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        s = self.bot_state
        # Calculate runtime from startup_time
        startup = getattr(s, "startup_time", None)
        if startup:
            runtime = int((datetime.now(timezone.utc) - startup).total_seconds() // 60)
        else:
            runtime = 0
        
        # Universe stats (nested in universe object)
        uni = getattr(s, "universe", None)
        eligible = getattr(uni, "eligible_symbols", 0) if uni else 0
        streams = getattr(uni, "symbols_streaming", 0) if uni else 0
        
        warm = getattr(s, "warm_symbols", 0)
        cold = getattr(s, "cold_symbols", 0)
        
        # Budget from actual fields
        budget_total = getattr(s, "bot_budget_usd", 0)
        exposure = getattr(s, "exposure_pct", 0)
        budget_used = budget_total * exposure if budget_total else 0
        
        return (
            f"Runtime: {runtime}m\n"
            f"Universe: {eligible}\n"
            f"Warm: {warm} | Cold: {cold}\n"
            f"Streams: {streams}\n"
            f"Budget: ${budget_used:.0f}/${budget_total:.0f}"
        )


class LiveLogs(Static):
    """Live log panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
        self._last_count = 0
    
    def render(self) -> str:
        logs = list(getattr(self.bot_state, "live_log", []))[-15:]
        
        if not logs:
            return "[dim]Waiting for events...[/]"
        
        lines = []
        for ts, lvl, msg in logs:
            ts_str = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)[:8]
            
            if lvl == "TRADE":
                lines.append(f"[green]{ts_str}[/] {msg[:50]}")
            elif lvl == "WARN":
                lines.append(f"[yellow]{ts_str}[/] {msg[:50]}")
            elif lvl == "ERROR":
                lines.append(f"[red]{ts_str}[/] {msg[:50]}")
            else:
                lines.append(f"[dim]{ts_str}[/] {msg[:50]}")
        
        return "\n".join(lines)


class LiveTradingDashboard(App):
    """Live trading dashboard - integrates with bot state."""
    
    CSS = """
    Screen {
        layout: grid;
        grid-size: 3 4;
        grid-rows: 1 1fr 1fr 1fr;
        grid-gutter: 1;
    }
    
    #status { column-span: 3; height: 1; }
    
    #scanner { border: solid cyan; padding: 0 1; }
    #positions { border: solid green; padding: 0 1; }
    #stats { border: solid magenta; padding: 0 1; }
    
    #signal { border: solid yellow; padding: 0 1; }
    #health { border: solid blue; padding: 0 1; }
    #orders { border: solid white; padding: 0 1; }
    
    #logs { column-span: 3; border: solid grey; padding: 0 1; }
    
    .title { text-style: bold; }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("p", "toggle_pause", "Pause"),
    ]
    
    def __init__(self, state: BotState):
        super().__init__()
        self.bot_state = state
        self._paused = False
    
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        yield LiveStatusBar(self.bot_state, id="status")
        
        yield Container(
            Label("📡 Scanner", classes="title"),
            LiveScanner(self.bot_state),
            id="scanner"
        )
        yield Container(
            Label("📊 Positions", classes="title"),
            LivePositions(self.bot_state),
            id="positions"
        )
        yield Container(
            Label("📈 Stats", classes="title"),
            LiveStats(self.bot_state),
            id="stats"
        )
        
        yield Container(
            Label("⚡ Signal", classes="title"),
            LiveSignal(self.bot_state),
            id="signal"
        )
        yield Container(
            Label("🔍 Health", classes="title"),
            LiveHealth(self.bot_state),
            id="health"
        )
        yield Container(
            Label("📋 Orders", classes="title"),
            LiveLogs(self.bot_state),
            id="orders"
        )
        
        yield Container(
            Label("📜 Recent Events", classes="title"),
            LiveLogs(self.bot_state),
            id="logs"
        )
        
        yield Footer()
    
    def on_mount(self):
        """Start refresh timer."""
        self.set_interval(0.5, self.refresh_all)
    
    def refresh_all(self):
        """Refresh all widgets."""
        if self._paused:
            return
        # Refresh all custom Static widgets to re-render with new state
        for widget in self.query("Static"):
            widget.refresh()
    
    def action_toggle_pause(self):
        """Toggle pause state."""
        self._paused = not self._paused
        self.notify(f"Updates {'paused' if self._paused else 'resumed'}")
    
    def action_refresh(self):
        """Manual refresh."""
        self.refresh()


async def run_tui_async(state: BotState):
    """Run the TUI asynchronously (for integration with bot)."""
    app = LiveTradingDashboard(state)
    await app.run_async()


def run_tui(state: BotState = None):
    """Run the TUI (standalone)."""
    state = state or BotState()
    app = LiveTradingDashboard(state)
    app.run()


if __name__ == "__main__":
    run_tui()
