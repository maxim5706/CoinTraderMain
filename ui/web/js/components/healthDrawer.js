// Health Drawer - Collapsible diagnostics panel
// Track expanded state to preserve across re-renders
let healthDrawerOpen = false;

export function renderHealthDrawer(container, state, coverageData = null) {
  if (!container) return;
  
  // Preserve current open state before re-render
  const existingDetails = container.querySelector('details');
  if (existingDetails) {
    healthDrawerOpen = existingDetails.open;
  }
  
  const hb = state.heartbeats || {};
  const eng = state.engine || {};
  
  // Calculate coverage - prefer state.coverage_summary (from warm/cold symbols)
  // as it's more accurate in PM2 mode where /api/coverage can't access live buffers
  let coverage = { ok: 0, stale: 0, missing: 0 };
  if (state.coverage_summary && (state.coverage_summary.ok > 0 || state.coverage_summary.stale > 0)) {
    coverage = state.coverage_summary;
  } else if (coverageData?.summary?.['1m']) {
    const tfSum = coverageData.summary['1m'];
    coverage = { ok: tfSum.OK || 0, stale: tfSum.STALE || 0, missing: tfSum.MISSING || 0 };
  }
  
  // Heartbeat status
  const heartbeats = [
    { id: 'ws', label: 'WebSocket', age: hb.ws },
    { id: '1m', label: '1m Candles', age: hb.candles_1m },
    { id: '5m', label: '5m Candles', age: hb.candles_5m },
    { id: 'ml', label: 'ML Scanner', age: hb.ml },
    { id: 'router', label: 'Order Router', age: hb.order_router }
  ];
  
  const formatAge = (seconds) => {
    if (seconds === undefined || seconds === null) return '--';
    if (seconds < 60) return `${seconds.toFixed(0)}s`;
    if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
    return `${(seconds / 3600).toFixed(1)}h`;
  };
  
  const getStatusClass = (seconds) => {
    if (seconds === undefined || seconds === null) return 'bg-gray-600';
    if (seconds < 10) return 'bg-green-500';
    if (seconds < 30) return 'bg-yellow-500';
    return 'bg-red-500';
  };
  
  // Engine stats
  const uptime = eng.uptime_seconds || 0;
  const uptimeStr = `${Math.floor(uptime / 3600)}h ${Math.floor((uptime % 3600) / 60)}m`;
  
  container.innerHTML = `
    <details class="group" ${healthDrawerOpen ? 'open' : ''}>
      <summary class="flex items-center justify-between cursor-pointer py-3 px-4 bg-gray-800/50 rounded-lg hover:bg-gray-800">
        <div class="flex items-center gap-3">
          <span class="text-gray-400">▶</span>
          <span class="font-medium">Bot Health & Diagnostics</span>
          <span class="text-xs px-2 py-0.5 rounded ${coverage.stale > 50 ? 'bg-yellow-900 text-yellow-300' : 'bg-green-900 text-green-300'}">
            ${coverage.ok} ok / ${coverage.stale} stale
          </span>
        </div>
        <span class="text-gray-500 text-sm group-open:hidden">Click to expand</span>
      </summary>
      
      <div class="mt-4 space-y-6 p-4 bg-gray-900/50 rounded-lg">
        <!-- Heartbeats -->
        <div>
          <h4 class="text-sm font-medium text-gray-400 mb-3">System Heartbeats</h4>
          <div class="grid grid-cols-5 gap-3">
            ${heartbeats.map(h => `
              <div class="text-center">
                <div class="w-3 h-3 rounded-full ${getStatusClass(h.age)} mx-auto mb-1"></div>
                <div class="text-xs text-gray-400">${h.label}</div>
                <div class="text-sm font-mono">${formatAge(h.age)}</div>
              </div>
            `).join('')}
          </div>
        </div>
        
        <!-- Coverage -->
        <div>
          <h4 class="text-sm font-medium text-gray-400 mb-3">Data Coverage</h4>
          <div class="grid grid-cols-3 gap-4 text-center">
            <div>
              <div class="text-2xl font-bold text-green-400">${coverage.ok}</div>
              <div class="text-xs text-gray-500">Fresh</div>
            </div>
            <div>
              <div class="text-2xl font-bold text-yellow-400">${coverage.stale}</div>
              <div class="text-xs text-gray-500">Stale</div>
            </div>
            <div>
              <div class="text-2xl font-bold text-red-400">${coverage.missing}</div>
              <div class="text-xs text-gray-500">Missing</div>
            </div>
          </div>
        </div>
        
        <!-- Engine Stats -->
        <div>
          <h4 class="text-sm font-medium text-gray-400 mb-3">Engine Statistics</h4>
          <div class="grid grid-cols-4 gap-4 text-sm">
            <div>
              <div class="text-gray-500">Uptime</div>
              <div class="font-mono">${uptimeStr}</div>
            </div>
            <div>
              <div class="text-gray-500">Profit Factor</div>
              <div class="font-mono">${(eng.profit_factor || 0).toFixed(2)}</div>
            </div>
            <div>
              <div class="text-gray-500">Win/Loss</div>
              <div class="font-mono">${eng.wins || 0}W / ${eng.losses || 0}L</div>
            </div>
            <div>
              <div class="text-gray-500">Max DD</div>
              <div class="font-mono text-red-400">$${(eng.max_drawdown || 0).toFixed(2)}</div>
            </div>
          </div>
        </div>
        
        <!-- API Stats -->
        <div>
          <h4 class="text-sm font-medium text-gray-400 mb-3">API Health</h4>
          <div class="grid grid-cols-4 gap-4 text-sm">
            <div>
              <div class="text-gray-500">REST Requests</div>
              <div class="font-mono">${(eng.rest_requests || 0).toLocaleString()}</div>
            </div>
            <div>
              <div class="text-gray-500">429 Errors</div>
              <div class="font-mono ${(eng.rest_429s || 0) > 0 ? 'text-red-400' : ''}">${eng.rest_429s || 0}</div>
            </div>
            <div>
              <div class="text-gray-500">WS Ticks/5s</div>
              <div class="font-mono">${eng.ticks_5s || 0}</div>
            </div>
            <div>
              <div class="text-gray-500">Vol Regime</div>
              <div class="font-mono">${eng.vol_regime || 'normal'}</div>
            </div>
          </div>
        </div>
      </div>
    </details>
  `;
}

// Quick health summary for header
export function getHealthSummary(state) {
  const hb = state.heartbeats || {};
  const maxAge = Math.max(hb.ws || 999, hb.candles_1m || 999, hb.ml || 999);
  
  if (maxAge < 10) return { status: 'OK', color: 'green' };
  if (maxAge < 60) return { status: 'DELAYED', color: 'yellow' };
  return { status: 'STALE', color: 'red' };
}
