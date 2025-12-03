"""
Dashboard module - Terminal UI for CoinTrader.

This module provides a clean, modular dashboard built with Rich.
Each panel is a separate component for easy maintenance.
"""

from dashboard.display import Dashboard

# Backward compatibility alias
DashboardV2 = Dashboard

__all__ = ["Dashboard", "DashboardV2"]
