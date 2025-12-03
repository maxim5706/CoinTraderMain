"""
Modern Textual TUI Dashboard for CoinTrader.

Features:
- Full interactive TUI with keyboard navigation
- Scrollable log panel (no more flashing!)
- Real-time updates
- Clean, professional look
"""

from datetime import datetime, timezone
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, Static, DataTable, Log, Label, ProgressBar
from textual.reactive import reactive
from textual.timer import Timer

from core.state import BotState


class StatusBar(Static):
    """Top status bar showing key metrics."""
    
    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
    
    def compose(self) -> ComposeResult:
        yield Static(id="status-content")
    
    def update_status(self):
        mode = self.state.mode.upper()
        mode_style = "yellow" if mode == "PAPER" else "green bold"
        
        portfolio = self.state.portfolio_value
        btc = getattr(self.state, "btc_1h_trend", 0)
        btc_style = "green" if btc > 0 else "red" if btc < 0 else "yellow"
        
        ws_ok = "✅" if self.state.ws_ok else "❌"
        
        content = self.query_one("#status-content", Static)
        content.update(
            f"[{mode_style}]{mode}[/] │ "
            f"💰 ${portfolio:.2f} │ "
            f"BTC: [{btc_style}]{btc:+.1f}%[/] │ "
            f"WS: {ws_ok} │ "
            f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}"
        )


class ScannerPanel(Static):
    """Shows top trading candidates."""
    
    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
    
    def compose(self) -> ComposeResult:
        yield DataTable(id="scanner-table")
    
    def on_mount(self):
        table = self.query_one("#scanner-table", DataTable)
        table.add_columns("Symbol", "Score", "Strategy", "Vol")
        table.zebra_stripes = True
    
    def update_data(self):
        table = self.query_one("#scanner-table", DataTable)
        table.clear()
        
        candidates = list(getattr(self.state, "scanner_candidates", []))[:8]
        for c in candidates:
            symbol = getattr(c, "symbol", "?").replace("-USD", "")
            score = getattr(c, "score", 0)
            strat = getattr(c, "strategy_id", "?")[:8]
            vol = getattr(c, "vol_spike", 1)
            
            score_str = f"[green]{score}[/]" if score >= 80 else f"[yellow]{score}[/]" if score >= 70 else str(score)
            vol_str = f"{vol:.0f}x"
            
            table.add_row(symbol, score_str, strat, vol_str)


class PositionsPanel(Static):
    """Shows current positions."""
    
    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
    
    def compose(self) -> ComposeResult:
        yield DataTable(id="positions-table")
    
    def on_mount(self):
        table = self.query_one("#positions-table", DataTable)
        table.add_columns("Symbol", "Value", "P&L", "Conf")
        table.zebra_stripes = True
    
    def update_data(self):
        table = self.query_one("#positions-table", DataTable)
        table.clear()
        
        positions = list(getattr(self.state, "position_displays", []))[:6]
        for p in positions:
            symbol = getattr(p, "symbol", "?").replace("-USD", "")
            value = getattr(p, "value_usd", 0)
            pnl = getattr(p, "pnl_pct", 0)
            conf = getattr(p, "confidence", 70)
            
            pnl_style = "green" if pnl > 0 else "red"
            table.add_row(
                symbol,
                f"${value:.0f}",
                f"[{pnl_style}]{pnl:+.1f}%[/]",
                f"{conf}%"
            )


class SignalPanel(Static):
    """Shows current signal being evaluated."""
    
    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
    
    def compose(self) -> ComposeResult:
        yield Static(id="signal-content")
    
    def update_data(self):
        sig = getattr(self.state, "current_signal", None)
        content = self.query_one("#signal-content", Static)
        
        if not sig or not getattr(sig, "symbol", None):
            content.update("[dim]No active signal[/]")
            return
        
        action = getattr(sig, "signal_type", "?")
        conf = getattr(sig, "confidence", 0)
        symbol = sig.symbol.replace("-USD", "")
        strat = getattr(sig, "strategy_id", "?")
        entry = getattr(sig, "entry_price", 0)
        stop = getattr(sig, "stop_price", 0)
        tp1 = getattr(sig, "tp1_price", 0)
        
        # Calculate R:R
        rr = 0
        if entry and stop and entry != stop:
            risk = entry - stop
            reward = tp1 - entry if tp1 else 0
            rr = reward / risk if risk > 0 else 0
        
        lines = [
            f"[bold cyan]{action}[/] @ {conf:.0f}%",
            f"[white]{symbol}[/] - {strat}",
            "",
            f"Entry: [white]${entry:.4f}[/]",
            f"Stop:  [red]${stop:.4f}[/]",
            f"TP1:   [green]${tp1:.4f}[/]",
            f"R:R:   [yellow]{rr:.1f}x[/]",
        ]
        content.update("\n".join(lines))


class StatsPanel(Static):
    """Shows daily trading stats."""
    
    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
    
    def compose(self) -> ComposeResult:
        yield Static(id="stats-content")
    
    def update_data(self):
        content = self.query_one("#stats-content", Static)
        
        trades = getattr(self.state, "trades_today", 0)
        wins = getattr(self.state, "wins_today", 0)
        losses = getattr(self.state, "losses_today", 0)
        pnl = getattr(self.state, "daily_pnl", 0)
        win_rate = getattr(self.state, "win_rate", 0)
        
        pnl_style = "green" if pnl >= 0 else "red"
        
        lines = [
            f"Trades: {trades}",
            f"W/L: {wins}/{losses}",
            f"Win%: {win_rate*100:.0f}%",
            f"PnL: [{pnl_style}]${pnl:+.2f}[/]",
        ]
        content.update("\n".join(lines))


class SanityPanel(Static):
    """Shows system health."""
    
    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
    
    def compose(self) -> ComposeResult:
        yield Static(id="sanity-content")
    
    def update_data(self):
        content = self.query_one("#sanity-content", Static)
        
        runtime = getattr(self.state, "runtime_seconds", 0) // 60
        eligible = getattr(self.state, "universe_eligible", 0)
        warm = getattr(self.state, "warm_symbols", 0)
        ws_age = getattr(self.state, "ws_last_age", 999)
        
        ws_style = "green" if ws_age < 5 else "yellow" if ws_age < 30 else "red"
        
        lines = [
            f"Runtime: {runtime}m",
            f"Universe: {eligible}",
            f"Warm: {warm}",
            f"WS: [{ws_style}]{ws_age:.1f}s[/]",
        ]
        content.update("\n".join(lines))


class LogPanel(Static):
    """Scrollable log panel."""
    
    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
    
    def compose(self) -> ComposeResult:
        yield Log(id="log-view", max_lines=100)
    
    def add_log(self, message: str, level: str = "INFO"):
        log = self.query_one("#log-view", Log)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        
        if level == "TRADE":
            log.write_line(f"[green]{ts}[/] {message}")
        elif level == "WARN":
            log.write_line(f"[yellow]{ts}[/] {message}")
        elif level == "ERROR":
            log.write_line(f"[red]{ts}[/] {message}")
        else:
            log.write_line(f"[dim]{ts}[/] {message}")
    
    def update_from_state(self):
        """Pull recent logs from state."""
        log = self.query_one("#log-view", Log)
        recent = list(getattr(self.state, "live_log", []))[-20:]
        
        # Only add new logs (simple approach - clear and repopulate)
        if recent and not hasattr(self, "_last_log_count"):
            self._last_log_count = 0
        
        if recent and len(recent) > getattr(self, "_last_log_count", 0):
            for ts, lvl, msg in recent[self._last_log_count:]:
                self.add_log(msg, lvl)
            self._last_log_count = len(recent)


class TradingDashboard(App):
    """Main Textual TUI Application."""
    
    CSS = """
    Screen {
        layout: grid;
        grid-size: 3 3;
        grid-rows: auto 1fr 1fr;
    }
    
    #status-bar {
        column-span: 3;
        height: 1;
        background: $surface;
        padding: 0 1;
    }
    
    #scanner {
        border: solid green;
        height: 100%;
    }
    
    #positions {
        border: solid cyan;
        height: 100%;
    }
    
    #stats {
        border: solid magenta;
        height: 100%;
    }
    
    #signal {
        border: solid yellow;
        height: 100%;
    }
    
    #sanity {
        border: solid blue;
        height: 100%;
    }
    
    #logs {
        column-span: 2;
        border: solid white;
        height: 100%;
    }
    
    DataTable {
        height: 100%;
    }
    
    .panel-title {
        text-style: bold;
        padding: 0 1;
    }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]
    
    def __init__(self, state: BotState = None):
        super().__init__()
        self.state = state or BotState()
        self._update_timer: Optional[Timer] = None
    
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        # Status bar
        yield StatusBar(self.state, id="status-bar")
        
        # Row 1: Scanner, Positions, Stats
        yield Container(
            Label("📡 Scanner", classes="panel-title"),
            ScannerPanel(self.state),
            id="scanner"
        )
        yield Container(
            Label("📊 Positions", classes="panel-title"),
            PositionsPanel(self.state),
            id="positions"
        )
        yield Container(
            Label("📈 Stats", classes="panel-title"),
            StatsPanel(self.state),
            id="stats"
        )
        
        # Row 2: Signal, Sanity, Logs
        yield Container(
            Label("⚡ Signal", classes="panel-title"),
            SignalPanel(self.state),
            id="signal"
        )
        yield Container(
            Label("🔍 Health", classes="panel-title"),
            SanityPanel(self.state),
            id="sanity"
        )
        yield Container(
            Label("📋 Logs", classes="panel-title"),
            LogPanel(self.state),
            id="logs"
        )
        
        yield Footer()
    
    def on_mount(self):
        """Start update timer."""
        self._update_timer = self.set_interval(0.5, self._refresh_all)
    
    def _refresh_all(self):
        """Refresh all panels."""
        try:
            self.query_one(StatusBar).update_status()
            self.query_one(ScannerPanel).update_data()
            self.query_one(PositionsPanel).update_data()
            self.query_one(SignalPanel).update_data()
            self.query_one(StatsPanel).update_data()
            self.query_one(SanityPanel).update_data()
            self.query_one(LogPanel).update_from_state()
        except Exception:
            pass  # Ignore errors during refresh
    
    def action_refresh(self):
        """Manual refresh."""
        self._refresh_all()
    
    def action_quit(self):
        """Quit the app."""
        self.exit()


def run_dashboard(state: BotState = None):
    """Run the Textual dashboard."""
    app = TradingDashboard(state)
    app.run()


if __name__ == "__main__":
    # Test run
    run_dashboard()
