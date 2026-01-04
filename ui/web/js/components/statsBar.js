// Stats Bar - Design System v2

export function renderStatsBar(container, data = {}) {
  if (!container) return;
  
  const positions = data.positions || [];
  const eng = data.engine || {};
  
  // Calculate stats
  const wins = eng.wins_today || 0;
  const losses = eng.losses_today || 0;
  const totalTrades = wins + losses;
  const winRate = totalTrades > 0 ? Math.round((wins / totalTrades) * 100) : 0;
  
  const unrealized = positions.reduce((sum, p) => sum + (p.pnl_usd || 0), 0);
  const realized = eng.realized_pnl_today || 0;
  const posCount = positions.length;
  const exposurePct = (eng.exposure_pct || 0).toFixed(0);
  const regime = data.btc_regime || 'normal';
  
  // PnL color classes only (green for profit)
  const unrealizedClass = unrealized >= 0 ? 'pnl-pos' : 'pnl-neg';
  const realizedClass = realized === 0 ? 'text-label' : realized > 0 ? 'pnl-pos' : 'pnl-neg';
  
  container.innerHTML = `
    <div class="flex flex-wrap gap-8">
      <!-- Performance -->
      <div class="flex items-start gap-6">
        <div>
          <div class="t-label">Win Rate</div>
          <div class="t-value mono">${winRate}%</div>
          <div class="t-meta">${wins}W / ${losses}L</div>
        </div>
        <div>
          <div class="t-label">Unrealized</div>
          <div class="t-value mono ${unrealizedClass}">${formatPnl(unrealized)}</div>
        </div>
        <div>
          <div class="t-label">Realized</div>
          <div class="t-value mono ${realizedClass}">${formatPnl(realized)}</div>
        </div>
      </div>
      
      <!-- Exposure -->
      <div class="flex items-start gap-6">
        <div>
          <div class="t-label">Positions</div>
          <div class="t-value">${posCount}</div>
          <div class="t-meta">${exposurePct}% exposed</div>
        </div>
        <div>
          <div class="t-label">Regime</div>
          <div class="t-value capitalize">${regime}</div>
        </div>
      </div>
    </div>
  `;
}

function formatPnl(v) {
  const val = parseFloat(v) || 0;
  const sign = val >= 0 ? '+' : '';
  return sign + '$' + val.toFixed(2);
}
