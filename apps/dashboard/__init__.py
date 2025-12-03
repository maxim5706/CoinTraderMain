"""Dashboard application entrypoints."""

from apps.dashboard.dashboard_v2 import DashboardV2
from apps.dashboard.tui import TradingDashboard, run_dashboard
from apps.dashboard.tui_live import LiveTradingDashboard, run_tui_async, run_tui

__all__ = [
    "DashboardV2",           # Old Rich dashboard (backward compat)
    "TradingDashboard",      # Basic Textual TUI
    "run_dashboard",         # Run basic TUI
    "LiveTradingDashboard",  # Live Textual TUI (integrated with bot)
    "run_tui_async",         # Run live TUI async
    "run_tui",               # Run live TUI standalone
]
