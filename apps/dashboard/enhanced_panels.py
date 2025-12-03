"""
Enhanced Dashboard Panels - Integration with new PnL Engine and Position Registry

These panels show data from our new integrated components:
- PnLEngine for accurate calculations
- PositionRegistry for dust handling and limits
- Strategy attribution tracking
"""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typing import Optional

from execution.order_router import OrderRouter


def render_enhanced_portfolio_panel(router: OrderRouter) -> Panel:
    """Enhanced portfolio panel using new PnLEngine data."""
    
    # Get data from new components
    registry_stats = router.position_registry.get_stats(router.get_price)
    strategy_pnl = router.pnl_engine.get_strategy_pnl()
    
    lines = []
    
    # Portfolio summary from registry
    lines.append(f"[bold]Portfolio Summary[/]")
    lines.append(f"Active Positions: {registry_stats.active_positions}")
    lines.append(f"Dust Positions: {registry_stats.dust_positions} (tracked)")
    lines.append(f"Total Exposure: ${registry_stats.total_exposure_usd:.2f}")
    
    # Position limits status
    dust_threshold = router.position_registry.limits.dust_threshold_usd
    max_positions = router.position_registry.limits.max_positions
    max_per_strategy = router.position_registry.limits.max_positions_per_strategy
    
    lines.append("")
    lines.append(f"[bold]Limits & Config[/]")
    lines.append(f"Max Positions: {registry_stats.active_positions}/{max_positions}")
    lines.append(f"Per-Strategy Limit: {max_per_strategy}")
    lines.append(f"Dust Threshold: ${dust_threshold}")
    
    # Strategy breakdown
    if registry_stats.by_strategy:
        lines.append("")
        lines.append(f"[bold]By Strategy[/]")
        for strategy_id, count in registry_stats.by_strategy.items():
            strategy_name = strategy_id or "default"
            lines.append(f"  {strategy_name}: {count} positions")
    
    return Panel(
        "\n".join(lines),
        title="🏗️ Enhanced Portfolio",
        border_style="blue"
    )


def render_strategy_pnl_panel(router: OrderRouter) -> Panel:
    """Show PnL attribution by strategy."""
    
    strategy_pnl = router.pnl_engine.get_strategy_pnl()
    
    if not strategy_pnl:
        return Panel(
            "[dim]No strategy PnL data yet[/]",
            title="🎯 Strategy PnL",
            border_style="yellow"
        )
    
    table = Table(expand=True, show_header=True, header_style="bold")
    table.add_column("Strategy", width=15)
    table.add_column("PnL", justify="right", width=10)
    table.add_column("%", justify="right", width=8)
    
    total_pnl = sum(strategy_pnl.values())
    
    for strategy_id, pnl in sorted(strategy_pnl.items(), key=lambda x: x[1], reverse=True):
        strategy_name = strategy_id or "default"
        pnl_color = "green" if pnl >= 0 else "red"
        
        # Calculate percentage of total
        pnl_pct = (pnl / abs(total_pnl) * 100) if total_pnl != 0 else 0
        
        table.add_row(
            strategy_name,
            Text(f"${pnl:+.2f}", style=pnl_color),
            f"{pnl_pct:+.1f}%"
        )
    
    # Add total row
    total_color = "green" if total_pnl >= 0 else "red"
    table.add_row(
        "[bold]TOTAL[/]",
        Text(f"${total_pnl:+.2f}", style=f"bold {total_color}"),
        "100.0%"
    )
    
    return Panel(
        table,
        title="🎯 Strategy Attribution",
        border_style="cyan"
    )


def render_dust_positions_panel(router: OrderRouter) -> Panel:
    """Show dust positions that are tracked but not counted."""
    
    dust_positions = router.position_registry.get_dust_positions()
    
    if not dust_positions:
        return Panel(
            "[dim]No dust positions[/]",
            title="🧹 Dust Tracker",
            border_style="dim"
        )
    
    lines = []
    total_dust_value = 0
    
    for symbol, position in dust_positions.items():
        current_price = router.get_price(symbol)
        dust_value = position.size_qty * current_price
        total_dust_value += dust_value
        
        lines.append(f"{symbol.replace('-USD', '')}: ${dust_value:.4f}")
    
    lines.append(f"[dim]────────[/]")
    lines.append(f"[bold]Total: ${total_dust_value:.4f}[/]")
    
    return Panel(
        "\n".join(lines),
        title="🧹 Dust Positions",
        border_style="yellow",
        subtitle="Tracked but not counted in limits"
    )


def render_accurate_pnl_panel(router: OrderRouter) -> Panel:
    """Show accurate PnL using PnLEngine."""
    
    lines = []
    
    # Calculate total unrealized PnL using our engine
    total_unrealized = 0
    for symbol, position in router.positions.items():
        current_price = router.get_price(symbol)
        if current_price > 0:
            unrealized = router.pnl_engine.calculate_unrealized_pnl(position, current_price)
            total_unrealized += unrealized
    
    # Daily stats from router
    daily_stats = router.daily_stats
    
    # PnL summary
    pnl_color = "green" if total_unrealized >= 0 else "red"
    lines.append(f"[bold]Unrealized PnL[/]")
    lines.append(f"[{pnl_color}]${total_unrealized:+.2f}[/]")
    
    lines.append("")
    lines.append(f"[bold]Daily Stats[/]")
    lines.append(f"Trades: {daily_stats.trades}")
    lines.append(f"Wins: {daily_stats.wins}")
    lines.append(f"Losses: {daily_stats.losses}")
    
    win_rate = (daily_stats.wins / daily_stats.trades * 100) if daily_stats.trades > 0 else 0
    lines.append(f"Win Rate: {win_rate:.1f}%")
    
    total_pnl_color = "green" if daily_stats.total_pnl >= 0 else "red"
    lines.append(f"Total PnL: [{total_pnl_color}]${daily_stats.total_pnl:+.2f}[/]")
    
    return Panel(
        "\n".join(lines),
        title="💸 Accurate PnL",
        border_style="green" if total_unrealized >= 0 else "red"
    )


def render_position_limits_panel(router: OrderRouter) -> Panel:
    """Show position limit status and capacity."""
    
    lines = []
    
    # Current capacity
    registry_stats = router.position_registry.get_stats(router.get_price)
    limits = router.position_registry.limits
    
    # Overall capacity
    capacity_pct = (registry_stats.active_positions / limits.max_positions) * 100
    capacity_color = "green" if capacity_pct < 70 else "yellow" if capacity_pct < 90 else "red"
    
    lines.append(f"[bold]Position Capacity[/]")
    lines.append(f"[{capacity_color}]{registry_stats.active_positions}/{limits.max_positions} ({capacity_pct:.0f}%)[/]")
    
    # Test if we can open new positions
    test_strategies = ["burst_flag", "vwap_reclaim", "mean_reversion"]
    
    lines.append("")
    lines.append(f"[bold]Strategy Capacity[/]")
    
    for strategy in test_strategies:
        can_open, reason = router.position_registry.can_open_position(strategy, 5.0)
        status_icon = "✅" if can_open else "❌"
        status_text = "OK" if can_open else reason
        lines.append(f"{status_icon} {strategy}: {status_text}")
    
    # Exposure by strategy
    strategy_exposure = router.position_registry.get_exposure_by_strategy(router.get_price)
    if strategy_exposure:
        lines.append("")
        lines.append(f"[bold]Strategy Exposure[/]")
        for strategy_id, exposure in strategy_exposure.items():
            strategy_name = strategy_id or "default"
            lines.append(f"{strategy_name}: ${exposure:.2f}")
    
    return Panel(
        "\n".join(lines),
        title="🚦 Position Limits",
        border_style="blue"
    )


def get_enhanced_panels(router: Optional[OrderRouter] = None):
    """Get all enhanced panels for dashboard integration."""
    
    if not router:
        return []
    
    return [
        render_enhanced_portfolio_panel(router),
        render_strategy_pnl_panel(router), 
        render_dust_positions_panel(router),
        render_accurate_pnl_panel(router),
        render_position_limits_panel(router),
    ]
