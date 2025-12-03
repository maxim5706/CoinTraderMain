"""
Main Dashboard Display - Clean TUI for CoinTrader.

This is a streamlined dashboard that:
- Shows essential trading information
- Updates in real-time via Rich Live
- Is organized into logical panels
"""

from datetime import datetime, timezone
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.state import BotState
from dashboard.panels import (
    render_top_bar,
    render_scanner_panel,
    render_positions_panel,
    render_signal_panel,
    render_stats_panel,
    render_sanity_panel,
    render_orders_panel,
)

console = Console()


class Dashboard:
    """Clean, modular terminal dashboard."""
    
    def __init__(self):
        self.console = console
        self.state = BotState()
    
    def render(self) -> Layout:
        """Render the full dashboard layout."""
        # Update heartbeat
        self.state.heartbeat_dashboard = datetime.now(timezone.utc)
        
        # Create layout
        layout = Layout()
        
        # Top bar (full width)
        layout.split_column(
            Layout(name="header", size=1),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )
        
        # Main area split into columns
        layout["main"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="center", ratio=2),
            Layout(name="right", ratio=1),
        )
        
        # Left column: Scanner + Positions
        layout["left"].split_column(
            Layout(name="scanner", ratio=2),
            Layout(name="positions", ratio=1),
        )
        
        # Center column: Signal + Orders
        layout["center"].split_column(
            Layout(name="signal", ratio=1),
            Layout(name="orders", ratio=1),
        )
        
        # Right column: Stats + Sanity
        layout["right"].split_column(
            Layout(name="stats"),
            Layout(name="sanity"),
        )
        
        # Render panels
        layout["header"].update(render_top_bar(self.state))
        layout["scanner"].update(render_scanner_panel(self.state))
        layout["positions"].update(render_positions_panel(self.state))
        layout["signal"].update(render_signal_panel(self.state))
        layout["orders"].update(render_orders_panel(self.state))
        layout["stats"].update(render_stats_panel(self.state))
        layout["sanity"].update(render_sanity_panel(self.state))
        layout["footer"].update(self._render_footer())
        
        return layout
    
    def _render_footer(self) -> Panel:
        """Render footer with recent events."""
        lines = []
        if self.state.live_log:
            for ts, lvl, msg in list(self.state.live_log)[:3]:
                color = {"TRADE": "green", "STRAT": "yellow", "WARN": "red"}.get(lvl, "dim")
                tstr = ts.strftime("%H:%M:%S")
                lines.append(f"[{color}]{tstr}[/] {msg[:60]}")
        else:
            lines.append("[dim]No recent events[/]")
        
        return Panel("\n".join(lines), title="[dim]Recent[/]", border_style="dim")
    
    # Backward compatibility alias
    def render_full(self) -> Layout:
        """Alias for render() for backward compatibility."""
        return self.render()
