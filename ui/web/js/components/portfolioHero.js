// Portfolio Hero - Clean, equity-focused with design system v2
export function renderPortfolioHero(container, state, coinbase = {}) {
  if (!container) return;
  
  const eng = state.engine || {};
  
  // Values
  const totalValue = coinbase.total_balance || state.portfolio_value || 0;
  const cash = coinbase.cash_balance || state.cash_balance || 0;
  const invested = coinbase.crypto_balance || state.holdings_value || 0;
  const botAvail = eng.bot_available_usd || state.bot_available || 0;
  
  // P&L
  const dayPnl = eng.realized_pnl_today || 0;
  const unrealized = (state.positions || []).reduce((sum, p) => sum + (p.pnl_usd || 0), 0);
  const totalPnl = dayPnl + unrealized;
  const pnlPct = totalValue > 0 ? ((totalPnl / totalValue) * 100) : 0;
  const isPositive = totalPnl >= 0;
  
  // Positions
  const posCount = state.positions?.length || 0;
  const maxPos = state.max_positions || 15;
  
  container.innerHTML = `
    <div class="space-y-4">
      <!-- Primary: Total Equity -->
      <div class="flex items-baseline gap-4">
        <span class="t-display mono">
          $${totalValue.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}
        </span>
        <span class="${isPositive ? 'pnl-pos' : 'pnl-neg'} t-value mono">
          ${isPositive ? '+' : ''}$${totalPnl.toFixed(2)}
        </span>
        <span class="t-meta">${isPositive ? '+' : ''}${pnlPct.toFixed(2)}%</span>
      </div>
      
      <!-- Secondary: Sub-stats -->
      <div class="flex flex-wrap items-center gap-5">
        <div class="flex flex-col">
          <span class="t-label">Available</span>
          <span class="t-value mono">$${botAvail.toFixed(0)}</span>
        </div>
        <div class="flex flex-col">
          <span class="t-label">Cash</span>
          <span class="t-value mono">$${cash.toFixed(0)}</span>
        </div>
        <div class="flex flex-col">
          <span class="t-label">Invested</span>
          <span class="t-value mono">$${invested.toFixed(0)}</span>
        </div>
        <div class="flex flex-col">
          <span class="t-label">Positions</span>
          <span class="t-value ${posCount >= maxPos ? 'text-amber-400' : ''}">${posCount}/${maxPos}</span>
        </div>
      </div>
    </div>
  `;
}

// Compact version for mobile or condensed view
export function renderPortfolioHeroCompact(container, state, coinbase = {}) {
  if (!container) return;
  
  const totalValue = coinbase.total_balance || state.total_value || 0;
  const totalPnl = (state.realized_pnl_today || 0) + (state.unrealized_pnl || 0);
  const isPositive = totalPnl >= 0;
  const posCount = state.positions?.length || 0;
  const maxPos = state.max_positions || 15;
  
  container.innerHTML = `
    <div class="flex items-center justify-between">
      <div class="flex items-baseline gap-3">
        <span class="text-2xl font-bold">$${totalValue.toFixed(2)}</span>
        <span class="${isPositive ? 'text-green-400' : 'text-red-400'} text-sm">
          ${isPositive ? '+' : ''}$${totalPnl.toFixed(2)}
        </span>
      </div>
      <div class="text-sm text-gray-400">${posCount}/${maxPos} positions</div>
    </div>
  `;
}
