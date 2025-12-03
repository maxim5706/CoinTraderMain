"""Enhanced Rich terminal dashboard with trust panels."""

from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.layout import Layout
from rich.live import Live

from core.state import BotState, BurstCandidate, FocusCoinState, CurrentSignal, PositionDisplay
from datetime import datetime, timezone


console = Console()


class DashboardV2:
    """Enhanced terminal dashboard with full trust panels."""
    
    def __init__(self):
        self.console = console
        self.state = BotState()
    
    def render_top_bar(self) -> Text:
        """Render the global status bar with portfolio value."""
        top = Text()
        # Dashboard heartbeat
        self.state.heartbeat_dashboard = datetime.now(timezone.utc)
        
        # Mode
        mode_color = "yellow" if self.state.mode == "paper" else "green bold"
        top.append("MODE: ", style="dim")
        top.append(f"{self.state.mode.upper()}", style=mode_color)
        if getattr(self.state, "profile", None):
            top.append(" (", style="dim")
            top.append(f"{self.state.profile}", style="cyan")
            top.append(")", style="dim")
        top.append(" │ ")
        
        # Balance display depends on mode
        if self.state.mode == "paper":
            paper_total = self.state.paper_balance_usd or (self.state.paper_balance + self.state.paper_positions_value)
            top.append("📝 ", style="")
            top.append(f"${paper_total:.2f}", style="bold yellow")
            top.append(" (paper)", style="dim yellow")
            top.append(" │ ")
        else:
            if self.state.portfolio_value > 0:
                top.append("💰 ", style="")
                top.append(f"${self.state.portfolio_value:.2f}", style="bold white")
                top.append(f" (${self.state.cash_balance:.0f} cash + ${self.state.holdings_value:.0f} crypto)", style="dim")
                top.append(" │ ")
        
        # API status
        top.append("API: ", style="dim")
        if self.state.mode == "paper":
            top.append("PAPER", style="yellow")
        else:
            if self.state.api_ok:
                top.append("LIVE", style="green")
            else:
                top.append("FAIL", style="red bold")
        top.append(" │ ")
        
        # WebSocket status
        self.state.update_ws_age()
        if self.state.ws_ok and self.state.ws_last_age < 5:
            top.append("WS: ", style="dim")
            top.append("✅", style="green")
            top.append(f" ({self.state.ws_last_age:.1f}s)", style="dim")
        elif self.state.ws_ok:
            top.append("WS: ", style="dim")
            top.append("⚠️", style="yellow")
            top.append(f" ({self.state.ws_last_age:.1f}s)", style="yellow")
        else:
            top.append("WS: ", style="dim")
            top.append("❌", style="red")
        top.append(" │ ")
        
        # Stream / warm counts
        top.append("Streams: ", style="dim")
        top.append(f"{self.state.universe.symbols_streaming}", style="white")
        top.append(" │ ")
        top.append("Warm: ", style="dim")
        top.append(f"{self.state.warm_symbols}/{self.state.universe.eligible_symbols}", style="white")
        top.append(" │ ")
        
        # ML freshness
        fresh = getattr(self.state, "ml_fresh_count", 0)
        total = getattr(self.state, "ml_total_count", 0)
        if total == 0:
            ml_label = "— (warming)"
            ml_style = "dim"
        else:
            ml_pct = (fresh / total * 100)
            ml_style = "yellow" if ml_pct < 60 else "green"
            ml_label = f"{ml_pct:.0f}%"
        top.append("ML: ", style="dim")
        top.append(ml_label, style=ml_style)
        top.append(" │ ")
        
        # PnL - Show unrealized position PnL (actual) not session change
        pnl = getattr(self.state, 'unrealized_pnl', 0.0) or self.state.daily_pnl
        pnl_color = "green" if pnl >= 0 else "red"
        top.append("PnL: ", style="dim")
        top.append(f"${pnl:+.2f}", style=pnl_color)
        top.append(f" (R:{self.state.realized_pnl:+.2f})", style="dim")
        top.append(" │ ")
        
        # BTC Regime
        regime = self.state.btc_regime
        if regime == "risk_off":
            top.append("BTC: ", style="dim")
            top.append(f"🔴 RISK OFF", style="red bold")
        elif regime == "caution":
            top.append("BTC: ", style="dim")
            top.append(f"🟡 CAUTION", style="yellow")
        else:
            top.append("BTC: ", style="dim")
            top.append(f"🟢 OK", style="green")
        top.append(f" ({self.state.btc_trend_1h:+.1f}%)", style="dim")
        top.append(" │ ")
        
        # Tier stats (compact)
        warm = len(self.state.warm_symbols) if isinstance(self.state.warm_symbols, list) else self.state.warm_symbols
        cold = len(self.state.cold_symbols) if isinstance(self.state.cold_symbols, list) else self.state.cold_symbols
        if cold > 0:
            top.append("Data: ", style="dim")
            top.append(f"{warm}✓/{cold}○", style="yellow")
        else:
            top.append("Data: ", style="dim")
            top.append(f"{warm}✓", style="green")
        top.append(" │ ")
        
        # Kill switch
        if self.state.kill_switch:
            top.append("KILL: ", style="dim")
            top.append("🛑 ON", style="red bold blink")
        else:
            top.append("KILL: ", style="dim")
            top.append("off", style="dim")
        top.append(" │ ")
        
        # Time
        top.append(datetime.now().strftime("%H:%M:%S"), style="dim")
        
        return top
    
    def _render_led(self, heartbeat: Optional[datetime], expected_s: float) -> Text:
        """Render a tiny LED based on heartbeat age."""
        led = Text()
        now = datetime.now(timezone.utc)
        if heartbeat is None:
            led.append("⚪", style="dim")
            return led
        age = (now - heartbeat).total_seconds()
        if age <= expected_s * 1.2:
            led.append("🟢", style="green")
        elif age <= expected_s * 3:
            led.append("🟡", style="yellow")
        else:
            led.append("🔴", style="red")
        # Simple blink by alternating block styles
        if int(now.timestamp()) % 2 == 0:
            led.append("▮", style="dim")
        else:
            led.append("▯", style="dim")
        return led
    
    def render_ethernet_row(self) -> Text:
        """Render the heartbeat/LED row."""
        row = Text()
        row.append("DATA: ", style="dim")
        row.append("WS ", style="dim")
        row.append(self._render_led(self.state.heartbeat_ws, 2))
        row.append(" 1m ", style="dim")
        row.append(self._render_led(self.state.heartbeat_candles_1m, 90))
        row.append(" 5m ", style="dim")
        row.append(self._render_led(self.state.heartbeat_candles_5m, 400))
        row.append(" FEAT ", style="dim")
        row.append(self._render_led(self.state.heartbeat_features, 120))
        row.append(" ML ", style="dim")
        row.append(self._render_led(self.state.heartbeat_ml, 180))
        row.append(" SCAN ", style="dim")
        row.append(self._render_led(self.state.heartbeat_scanner, 300))
        row.append(" ORD ", style="dim")
        row.append(self._render_led(self.state.heartbeat_order_router, 120))
        row.append(" UI ", style="dim")
        row.append(self._render_led(self.state.heartbeat_dashboard, 10))
        return row
    
    def render_burst_table(self) -> Table:
        """Render the burst leaderboard (radar)."""
        table = Table(
            title="[bold cyan]⚡ Burst Radar[/]",
            expand=True,
            show_edge=False,
            header_style="bold cyan"
        )
        
        table.add_column("Symbol", width=10)
        table.add_column("Price", justify="right", width=10)
        table.add_column("Score", justify="right", width=6)  # Entry quality score
        table.add_column("Burst", justify="right", width=6)
        table.add_column("Vol↑", justify="right", width=5)
        table.add_column("Rng↑", justify="right", width=5)
        table.add_column("Trend", justify="right", width=6)
        
        for row in self.state.burst_leaderboard[:50]:
            # Color burst score
            if row.burst_score >= 5:
                burst_style = "bold green"
            elif row.burst_score >= 3:
                burst_style = "yellow"
            elif row.burst_score > 0:
                burst_style = "white"
            else:
                burst_style = "dim cyan"  # Still loading
            
            # Color trend
            if row.trend_5m > 0.5:
                trend_style = "green"
            elif row.trend_5m < -0.5:
                trend_style = "red"
            else:
                trend_style = "dim"
            
            # Format burst score - show "..." when still loading
            if row.burst_score > 0:
                burst_text = f"{row.burst_score:.1f}"
            else:
                burst_text = "..."
            
            # Format entry score with color
            entry_score = getattr(row, 'entry_score', 0)
            if entry_score >= 70:
                score_style = "bold green"
                score_text = f"{entry_score}"
            elif entry_score >= 50:
                score_style = "yellow"
                score_text = f"{entry_score}"
            elif entry_score > 0:
                score_style = "dim"
                score_text = f"{entry_score}"
            else:
                score_style = "dim"
                score_text = "-"
            
            table.add_row(
                row.symbol.replace("-USD", ""),
                f"${row.price:.4f}" if row.price < 100 else f"${row.price:.2f}",
                Text(score_text, style=score_style),
                Text(burst_text, style=burst_style),
                f"{row.vol_spike:.0f}x" if row.vol_spike > 0 else "-",
                f"{row.range_spike:.0f}x" if row.range_spike > 0 else "-",
                Text(f"{row.trend_5m:+.1f}%" if row.trend_5m != 0 else "-", style=trend_style),
            )
        
        # Fill empty rows
        # Fill to at least 8 rows to keep table height stable
        for _ in range(max(0, 8 - len(self.state.burst_leaderboard))):
            table.add_row("-", "-", "-", "-", "-", "-", "-", style="dim")
        
        return table
    
    def render_focus_panel(self) -> Panel:
        """Render the focus coin detailed state."""
        fc = self.state.focus_coin
        
        if not fc.symbol:
            return Panel(
                "[dim]No focus coin selected yet...[/]",
                title="[bold magenta]🎯 Focus Coin[/]",
                expand=True
            )
        
        lines = []
        
        # Header
        stage_colors = {
            "waiting": "dim",
            "burst": "yellow",
            "impulse": "cyan",
            "flag": "blue",
            "breakout": "bold green",
            "warmup": "magenta",
            "trap": "bold red"
        }
        stage_style = stage_colors.get(fc.stage, "dim")
        
        # Show spread next to price
        spread_str = ""
        if fc.spread_bps > 0:
            spread_color = "green" if fc.spread_bps <= 12 else "yellow" if fc.spread_bps <= 20 else "red"
            spread_str = f"  [{spread_color}]({fc.spread_bps:.1f}bps)[/]"
        
        lines.append(f"[bold]{fc.symbol}[/]  ${fc.price:.4f}{spread_str}  [{stage_style}]{fc.stage.upper()}[/]")
        lines.append("")
        if not fc.warmup_ready:
            lines.append(
                f"[magenta]Warmup[/]: 1m {fc.warmup_1m}/10  |  5m {fc.warmup_5m}/3"
            )
            lines.append("")
        
        # Impulse section
        lines.append("[bold cyan]📈 Impulse[/]")
        if fc.impulse_move != 0:
            move_color = "green" if fc.impulse_move > 0 else "red"
            lines.append(f"  move: [{move_color}]{fc.impulse_move:+.2f}%[/]  candles: {fc.impulse_green_candles}")
            lines.append(f"  high: ${fc.impulse_high:.4f}  low: ${fc.impulse_low:.4f}")
            lines.append(f"  age: {fc.impulse_age_min:.0f}m  ATR: ${fc.impulse_atr:.4f}")
        else:
            lines.append("  [dim]waiting for impulse...[/]")
        lines.append("")
        
        # Flag section
        lines.append("[bold blue]🚩 Flag[/]")
        if fc.flag_age_min > 0:
            lines.append(f"  retrace: {fc.flag_retracement*100:.1f}%  age: {fc.flag_age_min:.0f}m")
            lines.append(f"  slope: {fc.flag_slope:+.4f}  vol_decay: {fc.flag_vol_decay:.2f}")
            lines.append(f"  high: ${fc.flag_high:.4f}  low: ${fc.flag_low:.4f}")
        else:
            lines.append("  [dim]waiting for flag...[/]")
        lines.append("")
        
        # Traps section
        lines.append("[bold red]⚠️ Traps[/]")
        traps = []
        if fc.triple_top:
            traps.append("[red]TRIPLE TOP[/]")
        if fc.head_shoulders:
            traps.append("[red]H&S[/]")
        if traps:
            lines.append(f"  {' '.join(traps)}")
        else:
            lines.append("  [green]none detected[/]")
        if fc.skip_reason:
            lines.append(f"  skip: {fc.skip_reason}")
        
        # Live population + log
        lines.append("")
        lines.append("[bold green]🛰 Live Feed[/]")
        lines.append(
            f"  ticks/5s: {self.state.ticks_last_5s}  "
            f"candles/5s: {self.state.candles_last_5s}  "
            f"events: {self.state.events_last_5s}"
        )
        
        lines.append("")
        lines.append("[bold]📜 Log[/]")
        if not self.state.live_log:
            lines.append("  [dim]no events yet...[/]")
        else:
            for ts, lvl, msg in list(self.state.live_log)[:6]:
                color = {
                    "WS": "cyan",
                    "DATA": "dim",
                    "FOCUS": "magenta",
                    "STRAT": "yellow",
                    "TRADE": "green",
                    "UNIV": "blue",
                    "WARN": "red",
                }.get(lvl, "white")
                tstr = ts.strftime("%H:%M:%S")
                lines.append(f"  [{color}]{tstr} {lvl}[/] {msg}")
        
        return Panel(
            "\n".join(lines),
            title="[bold magenta]🎯 Focus Coin[/]",
            expand=True
        )
    
    def render_signal_panel(self) -> Panel:
        """Render the current signal panel."""
        sig = self.state.current_signal
        fc = self.state.focus_coin
        
        lines = []
        
        # Action with color
        action_colors = {
            "WAIT": "dim",
            "ENTER_LONG": "bold green",
            "ENTER_LONG_FAST": "bold cyan",
            "EXIT": "yellow",
            "SKIP_TRAP": "bold red"
        }
        action_style = action_colors.get(sig.action, "dim")
        
        # Show FAST label distinctly
        action_label = sig.action
        if sig.action == "ENTER_LONG_FAST":
            action_label = "⚡ ENTER_LONG (FAST)"
        
        lines.append(f"[{action_style}]▶ {action_label}[/]  conf: {sig.confidence:.0%}")
        lines.append("")
        lines.append(f"[dim]why:[/] {sig.reason or '-'}")
        lines.append("")
        
        if sig.action in ["ENTER_LONG", "ENTER_LONG_FAST"]:
            lines.append(f"[bold]Entry:[/]  ${sig.entry_price:.4f}")
            lines.append(f"[red]Stop:[/]   ${sig.stop_price:.4f}")
            lines.append(f"[green]TP1:[/]    ${sig.tp1_price:.4f}")
            lines.append(f"[green]TP2:[/]    ${sig.tp2_price:.4f}")
            if sig.time_stop_deadline:
                lines.append(f"[dim]Time:[/]   {sig.time_stop_deadline}")
            # Show spread for FAST mode
            if sig.action == "ENTER_LONG_FAST" and fc.spread_bps > 0:
                spread_color = "green" if fc.spread_bps <= 12 else "yellow"
                lines.append(f"[{spread_color}]Spread:[/] {fc.spread_bps:.1f} bps")
        
        # ML info line (from cached state)
        if hasattr(self.state, 'ml_score') and self.state.ml_score is not None:
            ml = self.state.ml_score
            conf = self.state.ml_confidence or 0
            ml_color = "green" if ml > 0.3 else "red" if ml < -0.3 else "yellow"
            lines.append("")
            lines.append(f"[{ml_color}]ML:[/] {ml:+.2f} (conf {conf:.0%})")
        
        # Chop/Vol regime
        if hasattr(self.state, 'is_choppy'):
            chop_text = "[red]ON[/]" if self.state.is_choppy else "[green]OFF[/]"
            lines.append(f"[dim]Chop:[/] {chop_text}")
        
        return Panel(
            "\n".join(lines),
            title="[bold yellow]⚡ Signal[/]",
            expand=True
        )
    
    def render_positions_table(self) -> Panel:
        """Render open positions table (used for current mode's positions)."""
        if not self.state.positions:
            return Panel(
                "[dim]No open positions[/]",
                title="[bold green]📊 Positions[/]",
                expand=True
            )
        
        table = Table(expand=True, show_edge=False, header_style="bold")
        table.add_column("Sym", width=6)
        table.add_column("$Size", justify="right", width=8)
        table.add_column("Entry", justify="right", width=10)
        table.add_column("Stop", justify="right", width=10)
        table.add_column("PnL%", justify="right", width=8)
        table.add_column("Age", justify="right", width=5)
        
        for p in self.state.positions:
            pnl_color = "green" if p.unrealized_pct >= 0 else "red"
            table.add_row(
                p.symbol.replace("-USD", ""),
                f"${p.size_usd:.2f}",
                f"${p.entry_price:.4f}",
                f"${p.stop_price:.4f}",
                Text(f"{p.unrealized_pct:+.2f}%", style=pnl_color),
                f"{p.age_min:.0f}m"
            )
        
        return Panel(table, title="[bold green]📊 Positions[/]", expand=True)
    
    def render_live_positions_panel(self) -> Panel:
        """Render LIVE positions panel."""
        if self.state.mode == "paper":
            return Panel(
                "[dim]Paper mode[/]",
                title="[bold yellow]📝 PAPER[/]",
                expand=True
            )
        
        # In LIVE mode, show tracked positions
        if not self.state.positions:
            holdings = getattr(self.state, 'holdings_value', 0)
            if holdings > 0:
                return Panel(
                    f"[dim]Syncing...[/]\n[yellow]${holdings:.0f} on exchange[/]",
                    title="[bold green]📊 LIVE[/]",
                    expand=True
                )
            return Panel(
                "[dim]No positions[/]",
                title="[bold green]📊 LIVE[/]",
                expand=True
            )
        
        lines = []
        total_value = 0
        total_pnl = 0
        for p in self.state.positions:
            pnl_color = "green" if p.unrealized_pct >= 0 else "red"
            sym = p.symbol.replace('-USD', '')
            
            # Show play quality indicator
            quality = getattr(p, 'play_quality', 'neutral')
            conf = getattr(p, 'current_confidence', 0)
            trend = getattr(p, 'confidence_trend', 'stable')
            
            if quality == 'strong':
                qual_icon = "🟢"
            elif quality == 'weak':
                qual_icon = "🔴"
            else:
                qual_icon = "🟡"
            
            # Trend arrow
            if trend == 'rising':
                trend_icon = "↑"
            elif trend == 'falling':
                trend_icon = "↓"
            else:
                trend_icon = "→"
            
            lines.append(f"{qual_icon} [bold]{sym}[/] ${p.size_usd:.0f} [{pnl_color}]{p.unrealized_pct:+.1f}%[/] {trend_icon}")
            total_value += p.size_usd
            total_pnl += p.unrealized_pnl
        
        # Add total line
        pnl_color = "green" if total_pnl >= 0 else "red"
        lines.append(f"[dim]───────────[/]")
        lines.append(f"Total: ${total_value:.0f} [{pnl_color}]${total_pnl:+.1f}[/]")
        
        return Panel(
            "\n".join(lines),
            title=f"[bold green]📊 LIVE ({len(self.state.positions)})[/]",
            expand=True
        )
    
    def render_paper_positions_panel(self) -> Panel:
        """Render PAPER positions panel (hidden in LIVE mode)."""
        if self.state.mode != "paper":
            # In LIVE mode, show today's live stats instead
            return Panel(
                f"Mode: LIVE\nProfile: {getattr(self.state, 'profile', '?')}",
                title="[bold cyan]ℹ️ Info[/]",
                expand=True
            )
        
        # In PAPER mode, show paper positions
        if not self.state.positions:
            return Panel(
                "[dim]No paper positions[/]",
                title="[bold yellow]📝 PAPER[/]",
                expand=True
            )
        
        table = Table(expand=True, show_edge=False, header_style="bold")
        table.add_column("Sym", width=6)
        table.add_column("$", justify="right", width=6)
        table.add_column("PnL%", justify="right", width=7)
        table.add_column("Age", justify="right", width=4)
        
        for p in self.state.positions:
            pnl_color = "green" if p.unrealized_pct >= 0 else "red"
            table.add_row(
                p.symbol.replace("-USD", ""),
                f"${p.size_usd:.0f}",
                Text(f"{p.unrealized_pct:+.1f}%", style=pnl_color),
                f"{p.age_min:.0f}m"
            )
        
        return Panel(table, title="[bold yellow]📝 PAPER[/]", expand=True)
    
    def render_sanity_panel(self) -> Panel:
        """Render startup sanity check - shows health at a glance."""
        lines = []
        
        # Runtime (clamp to 0 to avoid negative display)
        if self.state.startup_time:
            from datetime import timezone
            now = datetime.now(timezone.utc)
            start = self.state.startup_time if self.state.startup_time.tzinfo else self.state.startup_time.replace(tzinfo=timezone.utc)
            runtime = max(0, (now - start).total_seconds())
            lines.append(f"[dim]Runtime:[/] {int(runtime//60)}m {int(runtime%60)}s")
        
        # Mode + Profile
        mode_color = "yellow" if self.state.mode == "paper" else "red bold"
        lines.append(f"[dim]Mode:[/] [{mode_color}]{self.state.mode.upper()}[/] ({self.state.profile})")
        
        # Universe
        uni_ok = self.state.universe.eligible_symbols >= 20
        uni_status = "🟢" if uni_ok else "🟡" if self.state.universe.eligible_symbols >= 10 else "🔴"
        lines.append(f"{uni_status} [dim]Universe:[/] {self.state.universe.eligible_symbols} eligible")
        
        # Top 5 from burst leaderboard
        if self.state.burst_leaderboard:
            top5 = [b.symbol.replace("-USD", "") for b in self.state.burst_leaderboard[:5]]
            lines.append(f"   [dim]Top:[/] {', '.join(top5)}")
        
        # Warm/Cold
        warm_count = len(self.state.warm_symbols) if isinstance(self.state.warm_symbols, list) else self.state.warm_symbols
        cold_count = len(self.state.cold_symbols) if isinstance(self.state.cold_symbols, list) else self.state.cold_symbols
        warm_ok = warm_count >= 10
        warm_status = "🟢" if warm_ok else "🟡"
        lines.append(f"{warm_status} [dim]Warm:[/] {warm_count} | Cold: {cold_count}")
        
        # Bot Budget
        budget = getattr(self.state, "bot_budget_usd", 0)
        exposure = getattr(self.state, "bot_exposure_usd", 0)
        available = getattr(self.state, "bot_available_usd", 0)
        if budget > 0:
            used_pct = exposure / budget * 100
            budget_color = "green" if used_pct < 80 else "yellow" if used_pct < 100 else "red"
            lines.append(f"💰 [dim]Budget:[/] [{budget_color}]${exposure:.0f}/${budget:.0f}[/]")
            lines.append(f"   [dim]Avail:[/] ${available:.0f}")
        
        # ML freshness
        fresh = getattr(self.state, "ml_fresh_count", 0)
        total = getattr(self.state, "ml_total_count", 0)
        if total == 0:
            lines.append("🟡 [dim]ML:[/] warming")
        else:
            ml_pct = fresh / total * 100
            ml_status = "🟢" if ml_pct >= 70 else "🟡" if ml_pct >= 50 else "🔴"
            lines.append(f"{ml_status} [dim]ML:[/] {ml_pct:.0f}% fresh")
        
        # WS status
        ws_ok = self.state.ws_ok and self.state.ws_last_age < 10
        ws_status = "🟢" if ws_ok else "🟡" if self.state.ws_ok else "🔴"
        lines.append(f"{ws_status} [dim]WS:[/] {self.state.ws_last_age:.1f}s | reconn: {self.state.ws_reconnect_count}")
        
        # Gate funnel (compact)
        lines.append("")
        lines.append("[dim]Gate Funnel:[/]")
        rej = [
            ("Warm", self.state.rejections_warmth),
            ("Regime", self.state.rejections_regime),
            ("Score", self.state.rejections_score),
            ("R:R", self.state.rejections_rr),
            ("Spread", self.state.rejections_spread),
            ("Limit", self.state.rejections_limits),
        ]
        # Show as compact bar
        total_rej = sum(r[1] for r in rej)
        if total_rej > 0:
            for name, count in rej:
                if count > 0:
                    pct = count / total_rej * 100
                    bar = "█" * max(1, int(pct / 10))
                    lines.append(f"  {name:6} {bar} {count}")
        else:
            lines.append("  [dim]No rejections yet[/]")
        
        return Panel(
            "\n".join(lines),
            title="[bold cyan]🔍 Sanity[/]",
            expand=True
        )
    
    def render_stats_panel(self) -> Panel:
        """Render daily stats with compounding metrics."""
        lines = []
        lines.append(f"Trades: {self.state.trades_today}")
        lines.append(f"W/L: {self.state.wins_today}/{self.state.losses_today}")
        
        # Win rate with color
        wr = self.state.win_rate * 100
        wr_color = "green" if wr >= 50 else "yellow" if wr >= 40 else "red"
        lines.append(f"Win%: [{wr_color}]{wr:.0f}%[/]")
        
        # Profit factor (show "—" if no trades, "∞" if wins but no losses)
        if hasattr(self.state, 'profit_factor'):
            pf = self.state.profit_factor
            if self.state.trades_today == 0:
                lines.append(f"PF: [dim]—[/]")
            elif pf is None or pf == 0:
                lines.append(f"PF: [dim]—[/]")
            elif pf >= 99:  # Effectively infinite (all wins)
                lines.append(f"PF: [green]∞[/]")
            else:
                pf_color = "green" if pf >= 1.5 else "yellow" if pf >= 1.0 else "red"
                lines.append(f"PF: [{pf_color}]{pf:.2f}[/]")
        
        # Avg R if available
        if hasattr(self.state, 'avg_r') and self.state.avg_r:
            ar = self.state.avg_r
            ar_color = "green" if ar > 0 else "red"
            lines.append(f"AvgR: [{ar_color}]{ar:+.2f}R[/]")
        
        # Daily PnL
        if hasattr(self.state, 'daily_pnl'):
            pnl = self.state.daily_pnl
            pnl_color = "green" if pnl >= 0 else "red"
            lines.append(f"PnL: [{pnl_color}]${pnl:+.2f}[/]")
        
        # Daily loss limit gauge
        if hasattr(self.state, 'loss_limit_pct'):
            pct = self.state.loss_limit_pct
            if pct > 0:
                bar_len = 8
                filled = int(pct / 100 * bar_len)
                bar = "█" * filled + "░" * (bar_len - filled)
                bar_color = "green" if pct < 50 else "yellow" if pct < 80 else "red"
                lines.append(f"Risk: [{bar_color}]{bar}[/] {pct:.0f}%")
        
        return Panel(
            "\n".join(lines),
            title="[bold]📈 Today[/]",
            expand=True
        )
    
    def render_full(self) -> Layout:
        """Render the complete dashboard layout."""
        layout = Layout()
        
        layout.split_column(
            Layout(name="top", size=2),
            Layout(name="middle", ratio=2),
            Layout(name="bottom", ratio=1),
        )
        
        # Top bar
        top_text = Text()
        top_text.append(self.render_top_bar())
        top_text.append("\n")
        top_text.append(self.render_ethernet_row())
        layout["top"].update(top_text)
        
        # Middle: Burst table + Focus coin
        layout["middle"].split_row(
            Layout(name="radar", ratio=1),
            Layout(name="focus", ratio=1),
        )
        layout["radar"].update(Panel(self.render_burst_table(), border_style="cyan"))
        layout["focus"].update(self.render_focus_panel())
        
        # Bottom: Signal + Positions + Sanity + Stats
        layout["bottom"].split_row(
            Layout(name="signal", ratio=1),
            Layout(name="live_pos", size=18),  # Wider for positions
            Layout(name="sanity", size=22),
            Layout(name="stats", size=14),
        )
        layout["signal"].update(self.render_signal_panel())
        layout["live_pos"].update(self.render_live_positions_panel())
        layout["sanity"].update(self.render_sanity_panel())
        layout["stats"].update(self.render_stats_panel())
        
        return layout
    
    def print_startup(self, api_ok: bool, api_msg: str):
        """Print startup banner with preflight results."""
        self.console.print()
        
        # Preflight result
        if api_ok:
            self.console.print(f"[green]✅ PREFLIGHT:[/] {api_msg}")
        else:
            self.console.print(f"[red]❌ PREFLIGHT:[/] {api_msg}")
        
        self.console.print()
        mode_color = 'yellow' if self.state.mode == 'paper' else 'green'
        profile = getattr(self.state, 'profile', 'default')
        
        self.console.print(Panel.fit(
            f"[bold cyan]CoinTrader[/] - Smart Day Trading Bot\n"
            f"Mode: [bold {mode_color}]{self.state.mode.upper()}[/] ({profile})\n"
            f"API: {'[green]OK[/]' if api_ok else '[red]FAIL[/]'}",
            title="🚀 Starting",
            border_style="cyan"
        ))
        self.console.print()
    
    def print_startup_complete(self, stats: dict):
        """Print startup completion summary."""
        self.console.print()
        lines = []
        lines.append(f"[green]✅ STARTUP COMPLETE[/]")
        lines.append(f"")
        lines.append(f"[bold]Universe:[/] {stats.get('eligible', 0)} symbols eligible")
        lines.append(f"[bold]Streams:[/]  {stats.get('ws_count', 0)} WS + {stats.get('rest_count', 0)} REST")
        lines.append(f"[bold]Data:[/]     {stats.get('candles_1m', 0)} 1m, {stats.get('candles_5m', 0)} 5m, {stats.get('candles_1h', 0)} 1h, {stats.get('candles_1d', 0)} 1d")
        lines.append(f"[bold]Portfolio:[/] ${stats.get('portfolio', 0):.2f} ({stats.get('positions', 0)} positions)")
        lines.append(f"[bold]Budget:[/]   ${stats.get('available', 0):.0f} available")
        
        self.console.print(Panel(
            "\n".join(lines),
            border_style="green"
        ))
        self.console.print()
    
    def print_shutdown(self):
        """Print shutdown summary."""
        self.console.print()
        pnl_color = "green" if self.state.daily_pnl >= 0 else "red"
        self.console.print(Panel.fit(
            f"Trades: {self.state.trades_today}\n"
            f"W/L: {self.state.wins_today}/{self.state.losses_today}\n"
            f"Win Rate: {self.state.win_rate*100:.1f}%\n"
            f"Total PnL: [{pnl_color}]${self.state.daily_pnl:+.2f}[/]",
            title="📊 Session Summary",
            border_style="cyan"
        ))
        self.console.print()


# Keep old Dashboard class for compatibility
Dashboard = DashboardV2
