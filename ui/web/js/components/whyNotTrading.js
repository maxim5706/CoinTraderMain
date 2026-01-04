// Why Not Trading Panel - Decision Lens with actionable insights
export function renderWhyNotTrading(container, state) {
  if (!container) return;
  
  const blockers = [];
  const info = [];
  
  // Max positions check
  const posCount = state.positions?.length || 0;
  const maxPos = state.max_positions || 15;
  if (posCount >= maxPos) {
    blockers.push({
      icon: '⚠',
      color: 'text-yellow-400',
      text: `Max positions reached (${posCount}/${maxPos})`,
      action: 'Close oldest losers to free slots'
    });
  }
  
  // Kill switch
  if (state.kill_switch) {
    blockers.push({
      icon: '🛑',
      color: 'text-red-400',
      text: 'Kill switch is ACTIVE',
      action: 'Resume trading to allow new entries'
    });
  }
  
  // Pause entries
  if (state.pause_entries || state.config?.pause_new_entries) {
    blockers.push({
      icon: '⏸',
      color: 'text-yellow-400',
      text: 'New entries are PAUSED',
      action: 'Disable pause in config'
    });
  }
  
  // Stale data
  const staleCount = state.coverage_summary?.stale || 0;
  if (staleCount > 50) {
    blockers.push({
      icon: '📊',
      color: 'text-orange-400',
      text: `${staleCount} symbols with stale data`,
      action: 'Check data feeds / API rate limits'
    });
  }
  
  // Health degraded
  const healthStatus = state.health?.status || 'UNKNOWN';
  if (healthStatus === 'STALE' || healthStatus === 'DEGRADED') {
    blockers.push({
      icon: '💔',
      color: 'text-red-400',
      text: `System health: ${healthStatus}`,
      action: 'Check bot logs / restart if needed'
    });
  }
  
  // Gate statistics from recent signals
  const gateStats = state.gate_mix || {};
  const topGate = Object.entries(gateStats).sort((a, b) => b[1] - a[1])[0];
  if (topGate && topGate[1] > 30) {
    info.push({
      icon: '🚧',
      color: 'text-gray-400',
      text: `Top blocker: ${topGate[0]} (${topGate[1]}%)`,
      action: null
    });
  }
  
  // Info items (non-blocking)
  info.push({
    icon: state.kill_switch ? '🛑' : '✓',
    color: state.kill_switch ? 'text-red-400' : 'text-green-400',
    text: `Kill switch: ${state.kill_switch ? 'ON' : 'OFF'}`,
    action: null
  });
  
  info.push({
    icon: '📈',
    color: 'text-gray-400',
    text: `Spread threshold: ${state.config?.spread_max_bps || state.config_running?.spread_max_bps || 50}bps`,
    action: null
  });
  
  const allItems = [...blockers, ...info];
  
  // Suggested action
  let suggestion = null;
  if (posCount >= maxPos) {
    const losers = (state.positions || []).filter(p => (p.pnl_usd || 0) < 0).length;
    if (losers > 0) {
      suggestion = `Close ${Math.min(3, losers)} oldest losers to free slots for fresh entries`;
    }
  } else if (staleCount > 100) {
    suggestion = `Data coverage is poor. Consider reducing symbol universe or checking API health`;
  } else if (blockers.length === 0) {
    suggestion = `System is ready to trade. Waiting for qualifying signals...`;
  }
  
  container.innerHTML = `
    <div class="space-y-2">
      ${allItems.map(item => `
        <div class="flex items-start gap-2 text-sm">
          <span class="${item.color}">${item.icon}</span>
          <div class="flex-1">
            <span class="${item.color}">${item.text}</span>
            ${item.action ? `<div class="text-xs text-gray-500 mt-0.5">${item.action}</div>` : ''}
          </div>
        </div>
      `).join('')}
    </div>
    ${suggestion ? `
      <div class="mt-4 pt-3 border-t border-gray-700">
        <div class="text-xs text-gray-400 mb-1">💡 Suggested action</div>
        <div class="text-sm text-blue-300">${suggestion}</div>
      </div>
    ` : ''}
  `;
}

// Get summary for quick display
export function getBlockerSummary(state) {
  const issues = [];
  
  const posCount = state.positions?.length || 0;
  const maxPos = state.max_positions || 15;
  if (posCount >= maxPos) issues.push('max_pos');
  if (state.kill_switch) issues.push('kill');
  if (state.pause_entries) issues.push('paused');
  if ((state.coverage_summary?.stale || 0) > 50) issues.push('stale_data');
  if (state.health?.status === 'STALE') issues.push('health');
  
  return {
    count: issues.length,
    issues,
    canTrade: issues.length === 0 || (issues.length === 1 && issues[0] === 'stale_data')
  };
}
