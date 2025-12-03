"""Enhanced Rich terminal dashboard with trust panels."""

from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.layout import Layout
from rich.live import Live

from core.state import BotState, BurstCandidate, FocusCoinState, CurrentSignal, PositionDisplay


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
        
        # Mode/Profile
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
        
        # Truth/sync (live)
        if self.state.mode != "paper":
            age = getattr(self.state, "portfolio_snapshot_age_s", 999.0)
            paused = getattr(self.state, "sync_paused", False)
            stale = getattr(self.state, "truth_stale", False)
            top.append("SYNC: ", style="dim")
            if paused:
                top.append("PAUSED", style="red bold")
            elif age < 15 and not stale:
                top.append("FRESH", style="green")
                top.append(f" ({age:.0f}s)", style="dim")
            else:
                top.append("STALE", style="yellow")
                top.append(f" ({age:.0f}s)", style="dim")
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
        row.append(" REST ", style="dim")
        row.append("🔴" if getattr(self.state, "rest_rate_degraded", False) else "🟢")
        row.append(" B/F ", style="dim")
        row.append(f"{getattr(self.state,'pending_backfills',0)}", style="yellow" if getattr(self.state,'pending_backfills',0) else "green")
        row.append(" UI ", style="dim")
        row.append(self._render_led(self.state.heartbeat_dashboard, 10))
        return row
    
    def render_scanner_table(self) -> Table:
        """Render the opportunity scanner (multi-strategy)."""
        table = Table(
            title="[bold cyan]📡 Scanner[/]",
            expand=True,
            show_edge=False,
            header_style="bold cyan"
        )
        
        table.add_column("Symbol", width=8)
        table.add_column("Strat", width=8)  # Strategy that generated signal
        table.add_column("Score", justify="right", width=5)
        table.add_column("R:R", justify="right", width=4)
        table.add_column("Vol↑", justify="right", width=4)
        table.add_column("Trend", justify="right", width=6)
        
        for row in self.state.burst_leaderboard[:9]:
            # Color trend
            if row.trend_5m > 0.5:
                trend_style = "green"
            elif row.trend_5m < -0.5:
                trend_style = "red"
            else:
                trend_style = "dim"
            
            # Format entry score with color
            entry_score = getattr(row, 'entry_score', 0)
            if entry_score >= 70:
                score_style = "bold green"
            elif entry_score >= 50:
                score_style = "yellow"
            else:
                score_style = "dim"
            
            # Get strategy from row if available
            strategy = getattr(row, 'strategy', '')
            if not strategy:
                # Infer from metrics
                if row.burst_score >= 3:
                    strategy = "burst"
                elif row.vol_spike >= 5:
                    strategy = "impulse"
                elif row.trend_5m > 1.0 and row.vol_spike < 2:
                    strategy = "daily"  # Multi-day trend (slow grind)
                elif row.vol_spike < 1.5 and abs(row.trend_5m) < 0.5:
                    strategy = "range"  # Consolidation/range
                else:
                    strategy = "vwap"
            strategy = strategy[:7]  # Truncate
            
            # R:R ratio
            rr = getattr(row, 'rr_ratio', 0)
            rr_text = f"{rr:.1f}" if rr > 0 else "-"
            
            table.add_row(
                row.symbol.replace("-USD", ""),
                Text(strategy, style="cyan"),
                Text(f"{entry_score}" if entry_score > 0 else "-", style=score_style),
                rr_text,
                f"{row.vol_spike:.0f}x" if row.vol_spike > 0 else "-",
                Text(f"{row.trend_5m:+.1f}%" if row.trend_5m != 0 else "-", style=trend_style),
            )
        
        # Fill empty rows to keep table height stable
        for _ in range(max(0, 8 - len(self.state.burst_leaderboard))):
            table.add_row("-", "-", "-", "-", "-", "-", style="dim")
        
        return table
    
    def render_blocked_panel(self) -> Panel:
        """Render blocked signals panel - shows what's being rejected."""
        blocked = list(getattr(self.state, 'blocked_signals', []))[:5]
        
        if not blocked:
            # Show recent rejection stats instead
            lines = []
            total_rej = (self.state.rejections_warmth + self.state.rejections_regime + 
                        self.state.rejections_score + self.state.rejections_rr + 
                        self.state.rejections_spread + self.state.rejections_limits)
            if total_rej > 0:
                lines.append(f"[dim]Total blocked:[/] {total_rej}")
                if self.state.rejections_limits > 0:
                    lines.append(f"[yellow]limits:[/] {self.state.rejections_limits}")
                if self.state.rejections_score > 0:
                    lines.append(f"[dim]score:[/] {self.state.rejections_score}")
                if self.state.rejections_spread > 0:
                    lines.append(f"[dim]spread:[/] {self.state.rejections_spread}")
                if self.state.rejections_rr > 0:
                    lines.append(f"[dim]r:r:[/] {self.state.rejections_rr}")
            else:
                lines.append("[dim]No rejections yet[/]")
            return Panel("\n".join(lines), title="[bold yellow]⚠️ Blocked[/]", expand=True)
        
        lines = []
        for sig in blocked:
            sym = getattr(sig, 'symbol', '?').replace('-USD', '')
            strat = getattr(sig, 'strategy', '')[:6]
            conf = getattr(sig, 'confidence', 0) * 100
            reason = getattr(sig, 'block_reason', '?')[:20]
            lines.append(f"[bold]{sym}[/] {strat} {conf:.0f}%")
            lines.append(f"  [red]{reason}[/]")
        
        return Panel("\n".join(lines), title="[bold yellow]⚠️ Blocked[/]", expand=True)
    
    def render_next_play_panel(self) -> Panel:
        """Render the next play panel - simplified, shows current best opportunity."""
        fc = self.state.focus_coin
        sig = self.state.current_signal
        
        if not fc.symbol:
            # Show waiting status
            lines = ["[dim]Scanning for opportunities...[/]", ""]
            
            # Show what's blocking us
            if self.state.rejections_limits > 0:
                lines.append("[yellow]⛔ Gate: position limits[/]")
            elif self.state.rejections_warmth > 0:
                lines.append("[dim]⏳ Warming up data...[/]")
            else:
                lines.append("[dim]No valid setups yet[/]")
            
            return Panel("\n".join(lines), title="[bold magenta]🎯 Next Play[/]", expand=True)
        
        lines = []
        
        # Header: Symbol @ Price | Strategy | Confidence
        stage_colors = {
            "waiting": "dim", "burst": "yellow", "impulse": "cyan",
            "flag": "blue", "breakout": "bold green", "warmup": "magenta", "trap": "bold red"
        }
        stage_style = stage_colors.get(fc.stage, "dim")
        
        # Get strategy name
        strategy = getattr(sig, 'strategy', '') or fc.stage
        conf = sig.confidence * 100 if sig.confidence else 0
        conf_style = "green" if conf >= 70 else "yellow" if conf >= 50 else "dim"
        
        lines.append(f"[bold]{fc.symbol}[/] @ ${fc.price:.4f}")
        lines.append(f"[cyan]{strategy}[/] | [{conf_style}]{conf:.0f}% conf[/] | [{stage_style}]{fc.stage.upper()}[/]")
        
        # Spread warning
        if fc.spread_bps > 0:
            spread_color = "green" if fc.spread_bps <= 12 else "yellow" if fc.spread_bps <= 20 else "red"
            lines.append(f"[{spread_color}]Spread: {fc.spread_bps:.1f}bps[/]")
        
        lines.append("")
        
        # Entry details if signal is active
        if sig.action in ["ENTER_LONG", "ENTER_LONG_FAST"]:
            lines.append(f"[bold]Entry:[/] ${sig.entry_price:.4f}")
            lines.append(f"[red]Stop:[/]  ${sig.stop_price:.4f}  [green]TP1:[/] ${sig.tp1_price:.4f}")
            
            # R:R calculation
            if sig.stop_price and sig.entry_price and sig.tp1_price:
                risk = abs(sig.entry_price - sig.stop_price)
                reward = abs(sig.tp1_price - sig.entry_price)
                rr = reward / risk if risk > 0 else 0
                lines.append(f"[bold]R:R:[/] {rr:.1f}x")
        else:
            lines.append(f"[dim]Action: {sig.action}[/]")
        
        lines.append("")
        
        # Gate status - why are we blocked?
        gate_reason = getattr(sig, 'block_reason', '') or sig.reason or ''
        if gate_reason and sig.action == "WAIT":
            lines.append(f"[yellow]⛔ Gate:[/] {gate_reason[:40]}")
        elif sig.action == "WAIT":
            # Check common blockers
            if self.state.rejections_limits > 0:
                lines.append("[yellow]⛔ Gate: position limits[/]")
            elif not fc.warmup_ready:
                lines.append(f"[magenta]⏳ Warmup: 1m {fc.warmup_1m}/10 | 5m {fc.warmup_5m}/3[/]")
        
        lines.append("")
        
        # Quick metrics
        lines.append(f"[dim]Vol:[/] {fc.vol_spike:.0f}x  [dim]Trend:[/] {fc.trend_5m:+.1f}%")
        
        # Recent log (compact)
        lines.append("")
        lines.append("[dim]─── Recent ───[/]")
        if self.state.live_log:
            for ts, lvl, msg in list(self.state.live_log)[:10]:
                color = {"TRADE": "green", "STRAT": "yellow", "WARN": "red"}.get(lvl, "dim")
                tstr = ts.strftime("%H:%M:%S")
                lines.append(f"[{color}]{tstr}[/] {msg[:30]}")
        else:
            lines.append("[dim]no events yet[/]")
        
        return Panel("\n".join(lines), title="[bold magenta]🎯 Next Play[/]", expand=True)
    
    def render_focus_panel(self) -> Panel:
        """Alias for backwards compatibility."""
        return self.render_next_play_panel()
    
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
            age = getattr(self.state, "portfolio_snapshot_age_s", 999.0)
            if holdings > 0:
                return Panel(
                    f"[dim]Syncing...[/]\n[yellow]${holdings:.0f} on exchange[/]\n[dim]snap:{age:.0f}s[/]",
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
        weak_count = 0
        
        for p in self.state.positions:
            pnl_color = "green" if p.unrealized_pct >= 0 else "red"
            sym = p.symbol.replace('-USD', '')
            
            # Get confidence - this is key!
            conf = getattr(p, 'current_confidence', 70)
            
            # Color based on confidence threshold (15% = auto-exit)
            if conf < 15:
                conf_style = "red bold"
                qual_icon = "🔴"
                weak_count += 1
            elif conf < 50:
                conf_style = "yellow"
                qual_icon = "🟡"
                weak_count += 1
            else:
                conf_style = "green"
                qual_icon = "🟢"
            
            # Compact line: Icon Sym $Size PnL% Conf%
            lines.append(f"{qual_icon} [bold]{sym:5}[/] ${p.size_usd:>4.0f} [{pnl_color}]{p.unrealized_pct:+5.1f}%[/] [{conf_style}]{conf:>3.0f}%[/]")
            total_value += p.size_usd
            total_pnl += p.unrealized_pnl
        
        # Add total line with weak count warning
        pnl_color = "green" if total_pnl >= 0 else "red"
        lines.append(f"[dim]────────────────[/]")
        lines.append(f"${total_value:.0f} [{pnl_color}]${total_pnl:+.1f}[/]")
        if weak_count > 0:
            lines.append(f"[yellow]⚠ {weak_count} weak plays[/]")
        
        return Panel(
            "\n".join(lines),
            title=f"[bold green]📊 LIVE ({len(self.state.positions)})[/]",
            expand=True
        )

    def render_recent_orders_panel(self) -> Panel:
        """Render a compact strip of recent order events."""
        orders = list(getattr(self.state, "recent_orders", []))
        if not orders:
            return Panel(
                "[dim]No orders yet[/]",
                title="[bold magenta]🧾 Orders[/]",
                expand=True
            )

        icons = {
            "open": "🟢",
            "partial_close": "🟡",
            "close": "🔴",
        }
        lines = []
        for evt in orders[:8]:
            icon = icons.get(getattr(evt, "event_type", ""), "⚪")
            ts = getattr(evt, "ts", None)
            ts_str = ts.strftime("%H:%M:%S") if ts else ""
            sym = getattr(evt, "symbol", "").replace("-USD", "")
            size = getattr(evt, "size_usd", 0.0)
            price = getattr(evt, "price", 0.0)
            line = f"{icon} {sym:5} ${size:.0f} @ ${price:.2f}"
            pnl = getattr(evt, "pnl", None)
            if pnl is not None and getattr(evt, "event_type", "") != "open":
                pnl_color = "green" if pnl >= 0 else "red"
                line += f" [{pnl_color}]{pnl:+.2f}[/]"
            if ts_str:
                line = f"{ts_str}  {line}"
            lines.append(line)

        return Panel(
            "\n".join(lines),
            title="[bold magenta]🧾 Orders[/]",
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
        lines.append(f"[dim]Streams:[/] {self.state.universe.symbols_streaming}/{self.state.universe.eligible_symbols}")
        
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
        if self.state.mode != "paper":
            age = getattr(self.state, "portfolio_snapshot_age_s", 999.0)
            paused = getattr(self.state, "sync_paused", False)
            stale = getattr(self.state, "truth_stale", False)
            truth_status = "🟢" if age < 15 and not stale and not paused else "🟡" if not paused else "🔴"
            lines.append(f"{truth_status} [dim]Snapshot:[/] {age:.0f}s {'PAUSED' if paused else ''}")
        
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
        
        # Middle: Scanner + Blocked + Next Play
        layout["middle"].split_row(
            Layout(name="scanner", ratio=1),
            Layout(name="blocked", size=24),
            Layout(name="next_play", ratio=1),
        )
        layout["scanner"].update(Panel(self.render_scanner_table(), border_style="cyan"))
        layout["blocked"].update(self.render_blocked_panel())
        layout["next_play"].update(self.render_next_play_panel())
        
        # Bottom: Signal + Orders + Positions + Health + Stats
        layout["bottom"].split_row(
            Layout(name="signal", ratio=1),
            Layout(name="orders", ratio=1),
            Layout(name="live_pos", size=28),  # Wider for confidence display
            Layout(name="sanity", size=22),
            Layout(name="stats", size=14),
        )
        layout["signal"].update(self.render_signal_panel())
        layout["orders"].update(self.render_recent_orders_panel())
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
