"""UI module - Dashboard, TUI, and Web components."""

from ui.web_server import app as web_app, set_bot_state, run_server_async

__all__ = [
    "web_app",               # FastAPI web app
    "set_bot_state",         # Share bot state with web server
    "run_server_async",      # Run web server async
]
