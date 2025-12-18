"""UI module - Dashboard, TUI, and Web components."""

from ui.dashboard_v2 import DashboardV2
from ui.tui_live import LiveTradingDashboard, run_tui_async, run_tui
from ui.web_server import app as web_app, set_bot_state, run_server_async

__all__ = [
    "DashboardV2",           # Rich terminal dashboard
    "LiveTradingDashboard",  # Live Textual TUI
    "run_tui_async",         # Run live TUI async
    "run_tui",               # Run live TUI standalone
    "web_app",               # FastAPI web app
    "set_bot_state",         # Share bot state with web server
    "run_server_async",      # Run web server async
]
