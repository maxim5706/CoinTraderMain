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
        
        # BTC trend and regime
        btc = getattr(s, "btc_trend_1h", 0) or 0
        btc_color = "green" if btc > 0 else "red" if btc < -1 else "yellow"
        regime = getattr(s, "btc_regime", "normal")
        regime_icon = "🟢" if regime == "normal" else "🟡" if regime == "caution" else "🔴"
        
        ws = "✅" if s.ws_ok else "❌"
        ws_age = s.ws_last_age or 0
        
        sync_age = getattr(s, "portfolio_snapshot_age_s", 999)
        sync = f"[green]OK[/]" if sync_age < 15 else f"[yellow]{sync_age:.0f}s[/]"
        
        return (
            f"[{mode_color} bold]{mode}[/] │ "
            f"💰 ${portfolio:.2f} (${cash:.0f}+${holdings:.0f}) │ "
            f"BTC: [{btc_color}]{btc:+.1f}%[/] {regime_icon} │ "
            f"WS: {ws} {ws_age:.0f}s │ "
            f"Sync: {sync} │ "
            f"[dim]{datetime.now(timezone.utc).strftime('%H:%M:%S')}[/]"
        )


class LiveScanner(Static):
    """Scanner panel - live watchlist with all candidates."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        lines = []
        # Show up to 15 candidates for full watchlist
        candidates = list(getattr(self.bot_state, "burst_leaderboard", []))[:15]
        
        # Get universe stats for empty state message
        uni = getattr(self.bot_state, "universe", None)
        eligible = getattr(uni, "eligible_symbols", 0) if uni else 0
        warm = getattr(self.bot_state, "warm_symbols", 0)
        
        if not candidates:
            # Show what we're doing while waiting for candidates
            return f"[dim]Scanning {eligible} symbols...\nWarm: {warm}[/]"
        
        # Header
        lines.append("[dim]SYM    SCR STRAT  VOL   TREND[/]")
        
        for c in candidates:
            sym = getattr(c, "symbol", "?").replace("-USD", "")[:6]
            score = getattr(c, "entry_score", 0) or getattr(c, "score", 0)
            trend = getattr(c, "trend_5m", 0)
            vol = getattr(c, "vol_spike", 0)
            burst = getattr(c, "burst_score", 0)
            
            # Infer strategy from metrics
            strat = getattr(c, "strategy", "") or getattr(c, "strategy_id", "")
            if not strat:
                if burst >= 3:
                    strat = "burst"
                elif vol >= 5:
                    strat = "impuls"
                elif trend > 1.0 and vol < 2:
                    strat = "daily"
                elif abs(trend) < 0.3:
                    strat = "range"
                else:
                    strat = "scan"
            strat = strat[:6]
            
            # Color coding
            score_color = "green bold" if score >= 80 else "green" if score >= 70 else "yellow" if score >= 60 else "dim"
            trend_color = "green" if trend > 0.5 else "red" if trend < -0.5 else "dim"
            vol_color = "cyan" if vol >= 3 else "dim"
            
            lines.append(
                f"[cyan]{sym:6}[/] [{score_color}]{score:3}[/] {strat:6} [{vol_color}]{vol:4.1f}x[/] [{trend_color}]{trend:+5.1f}%[/]"
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
            # PositionDisplay fields
            value = getattr(p, "size_usd", 0)
            pnl_pct = getattr(p, "unrealized_pct", 0)
            pnl_usd = getattr(p, "unrealized_pnl", 0)
            entry = getattr(p, "entry_price", 0)
            stop = getattr(p, "stop_price", 0)
            age = getattr(p, "age_min", 0)
            
            # Calculate stop distance %
            stop_dist = ((entry - stop) / entry * 100) if entry > 0 else 0
            
            total_value += value
            total_pnl += pnl_usd
            
            pnl_color = "green" if pnl_pct > 0 else "red" if pnl_pct < 0 else "dim"
            # Show: SYM $value +pnl% (stop% age)
            lines.append(
                f"[cyan]{sym:5}[/] ${value:4.0f} [{pnl_color}]{pnl_pct:+5.1f}%[/] "
                f"[dim]({stop_dist:.1f}% {age:.0f}m)[/]"
            )
        
        lines.append(f"[dim]───────────────────[/]")
        pnl_color = "green" if total_pnl >= 0 else "red"
        lines.append(f"[bold]Total:[/] ${total_value:.0f} [{pnl_color}]${total_pnl:+.2f}[/]")
        
        return "\n".join(lines)


class LiveSignal(Static):
    """Current signal and pipeline status panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        sig = getattr(self.bot_state, "current_signal", None)
        
        # Get focus symbol and stage from focus_coin object
        focus_coin = getattr(self.bot_state, "focus_coin", None)
        focus = getattr(focus_coin, "symbol", "").replace("-USD", "") if focus_coin else "—"
        stage = getattr(focus_coin, "stage", "scan") if focus_coin else "scan"
        
        # Show current action/reason
        if sig:
            action = getattr(sig, "action", "WAIT")
            reason = getattr(sig, "reason", "")[:35]
            conf = getattr(sig, "confidence", 0)
            entry = getattr(sig, "entry_price", 0)
            stop = getattr(sig, "stop_price", 0)
            tp1 = getattr(sig, "tp1_price", 0)
            
            if action == "WAIT":
                # Color stage for visibility
                stage_color = "green" if stage in ("impulse", "flag") else "yellow" if stage == "burst" else "dim"
                return (
                    f"[yellow]⏳ WAITING[/]\n"
                    f"Focus: [cyan]{focus}[/] [{stage_color}]{stage}[/]\n"
                    f"{reason or 'Scanning...'}\n"
                    f"Conf: {conf:.0f}%"
                )
            elif action in ("BUY", "ENTER_LONG", "ENTER_LONG_FAST"):
                # Only show prices if they look valid (not 0 and reasonable)
                if entry > 0 and entry < 100000:
                    rr = 0
                    if entry and stop and entry != stop:
                        risk = entry - stop
                        reward = tp1 - entry if tp1 else 0
                        rr = reward / risk if risk > 0 else 0
                    
                    # Format price based on magnitude
                    if entry < 1:
                        price_fmt = f"${entry:.6f}"
                    elif entry < 100:
                        price_fmt = f"${entry:.4f}"
                    else:
                        price_fmt = f"${entry:.2f}"
                    
                    return (
                        f"[green bold]🟢 {action}[/]\n"
                        f"[white bold]{focus}[/] [{stage}]\n"
                        f"Entry: {price_fmt}\n"
                        f"R:R: {rr:.1f}x | Conf: {conf:.0f}%"
                    )
                else:
                    return (
                        f"[green bold]🟢 {action}[/]\n"
                        f"[white bold]{focus}[/]\n"
                        f"Conf: {conf:.0f}%"
                    )
            elif action == "SKIP_TRAP":
                return (
                    f"[red]⚠️ TRAP DETECTED[/]\n"
                    f"Focus: {focus}\n"
                    f"{reason}"
                )
        
        return "[dim]Pipeline idle...[/]"


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
        realized = getattr(s, "realized_pnl", 0)
        unrealized = getattr(s, "actual_pnl", 0) - realized
        total_pnl = getattr(s, "actual_pnl", 0)
        win_rate = getattr(s, "win_rate", 0)
        
        # Daily loss limit
        loss_limit = getattr(s, "daily_loss_limit_usd", 30)
        loss_pct = (abs(min(total_pnl, 0)) / loss_limit * 100) if loss_limit > 0 else 0
        limit_color = "green" if loss_pct < 50 else "yellow" if loss_pct < 80 else "red"
        
        pnl_color = "green" if total_pnl >= 0 else "red"
        real_color = "green" if realized >= 0 else "red"
        
        return (
            f"Trades: {trades} ({wins}W/{losses}L)\n"
            f"Win%: {win_rate*100:.0f}%\n"
            f"Real: [{real_color}]${realized:+.2f}[/]\n"
            f"Total: [{pnl_color}]${total_pnl:+.2f}[/]\n"
            f"Limit: [{limit_color}]{loss_pct:.0f}%[/] of ${loss_limit:.0f}"
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
        
        # Tier breakdown
        t1 = getattr(s, "tier1_count", 0)
        t2 = getattr(s, "tier2_count", 0)
        t3 = getattr(s, "tier3_count", 0)
        
        warm = getattr(s, "warm_symbols", 0)
        cold = getattr(s, "cold_symbols", 0)
        backfills = getattr(s, "pending_backfills", 0)
        
        # Budget from actual fields
        budget_total = getattr(s, "bot_budget_usd", 0)
        exposure_pct = getattr(s, "exposure_pct", 0)  # Already 0-100 percentage
        budget_used = budget_total * (exposure_pct / 100) if budget_total else 0
        
        # Rate limit status
        rate_degraded = getattr(s, "rest_rate_degraded", False)
        rate_icon = "🔴" if rate_degraded else "🟢"
        
        return (
            f"Runtime: {runtime}m | Rate: {rate_icon}\n"
            f"Universe: {eligible} ({t1}ws/{t2}+{t3}rest)\n"
            f"Warm: {warm} | Cold: {cold}\n"
            f"Backfill: {backfills} pending\n"
            f"Budget: ${budget_used:.0f}/${budget_total:.0f}"
        )


class LiveLogs(Static):
    """Live log panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
        self._last_count = 0
    
    def render(self) -> str:
        # Show more logs to fill the panel
        logs = list(getattr(self.bot_state, "live_log", []))[-25:]
        
        if not logs:
            return "[dim]Waiting for events...[/]"
        
        lines = []
        for ts, lvl, msg in logs:
            ts_str = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts)[:8]
            # Allow longer messages to use panel width
            msg_display = msg[:80]
            
            if lvl == "TRADE":
                lines.append(f"[green]{ts_str}[/] {msg_display}")
            elif lvl == "WARN":
                lines.append(f"[yellow]{ts_str}[/] {msg_display}")
            elif lvl == "ERROR":
                lines.append(f"[red]{ts_str}[/] {msg_display}")
            else:
                lines.append(f"[dim]{ts_str}[/] {msg_display}")
        
        return "\n".join(lines)


class LiveVerdicts(Static):
    """Rejection stats and verdicts panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        s = self.bot_state
        
        # Rejection counts
        spread = getattr(s, "rejections_spread", 0)
        warmth = getattr(s, "rejections_warmth", 0)
        regime = getattr(s, "rejections_regime", 0)
        score = getattr(s, "rejections_score", 0)
        rr = getattr(s, "rejections_rr", 0)
        limits = getattr(s, "rejections_limits", 0)
        
        total = spread + warmth + regime + score + rr + limits
        
        # Activity counters
        ticks = getattr(s, "ticks_last_5s", 0)
        candles = getattr(s, "candles_last_5s", 0)
        
        lines = [
            f"[bold]Rejections:[/] {total}",
            f"  Spread: {spread} | Score: {score}",
            f"  R:R: {rr} | Limits: {limits}",
            f"  Warmth: {warmth} | Regime: {regime}",
            f"[dim]─────────────────[/]",
            f"Activity: {ticks}t/{candles}c per 5s"
        ]
        
        return "\n".join(lines)


class LiveActivity(Static):
    """Live stream activity panel."""
    
    def __init__(self, state: BotState, **kwargs):
        super().__init__(**kwargs)
        self.bot_state = state
    
    def render(self) -> str:
        s = self.bot_state
        
        # Stream activity
        ticks = getattr(s, "ticks_last_5s", 0)
        candles = getattr(s, "candles_last_5s", 0)
        events = getattr(s, "events_last_5s", 0)
        
        # Recent hot symbol
        hot = getattr(s, "last_hot_symbol", None) or "—"
        hot_age = getattr(s, "hot_symbol_age", 0)
        
        # Kill switch status
        kill = getattr(s, "kill_switch", False)
        kill_reason = getattr(s, "kill_reason", "")
        
        if kill:
            status = f"[red bold]⛔ KILLED[/]\n{kill_reason[:30]}"
        else:
            status = "[green]✅ Active[/]"
        
        return (
            f"Ticks: {ticks}/5s\n"
            f"Candles: {candles}/5s\n"
            f"Events: {events}/5s\n"
            f"Hot: {hot[:8]}\n"
            f"Status: {status}"
        )


class LiveTradingDashboard(App):
    """Live trading dashboard - integrates with bot state."""
    
    CSS = """
    Screen {
        layout: grid;
        grid-size: 4 4;
        grid-rows: 1 1fr 1fr 1fr;
        grid-gutter: 1;
    }
    
    #status { column-span: 4; height: 1; }
    
    #scanner { border: solid cyan; padding: 0 1; }
    #positions { border: solid green; padding: 0 1; }
    #stats { border: solid magenta; padding: 0 1; }
    #verdicts { border: solid red; padding: 0 1; }
    
    #signal { border: solid yellow; padding: 0 1; }
    #health { border: solid blue; padding: 0 1; }
    #orders { border: solid white; padding: 0 1; }
    #activity { border: solid cyan; padding: 0 1; }
    
    #logs { column-span: 4; border: solid grey; padding: 0 1; }
    
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
            Label("🚫 Verdicts", classes="title"),
            LiveVerdicts(self.bot_state),
            id="verdicts"
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
            Label("📶 Activity", classes="title"),
            LiveActivity(self.bot_state),
            id="activity"
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
