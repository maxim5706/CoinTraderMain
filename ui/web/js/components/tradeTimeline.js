// Trade Timeline - Chronological stream of bot events
export function renderTradeTimeline(container, events = [], limit = 15) {
  if (!container) return;
  
  // Dedupe consecutive blocked signals for same symbol (reduce noise)
  const sorted = [...events].sort((a, b) => new Date(b.ts) - new Date(a.ts));
  const deduped = [];
  let lastBlockedSymbol = null;
  let blockCount = 0;
  
  for (const e of sorted) {
    if (e.type === 'signal_blocked') {
      if (e.symbol === lastBlockedSymbol) {
        blockCount++;
        continue; // Skip consecutive blocks for same symbol
      }
      lastBlockedSymbol = e.symbol;
      blockCount = 1;
    } else {
      lastBlockedSymbol = null;
      blockCount = 0;
    }
    deduped.push(e);
  }
  
  const display = deduped.slice(0, limit);
  
  if (display.length === 0) {
    container.innerHTML = `
      <div class="text-gray-500 text-center py-4 text-sm">
        No trading events yet
      </div>
    `;
    return;
  }
  
  const typeConfig = {
    'entry': { icon: '▲', color: 'text-green-400', bg: 'bg-green-900/30', label: 'ENTRY' },
    'exit': { icon: '▼', color: 'text-red-400', bg: 'bg-red-900/30', label: 'EXIT' },
    'signal_blocked': { icon: '○', color: 'text-gray-400', bg: 'bg-gray-800/50', label: 'BLOCKED' },
    'signal_taken': { icon: '●', color: 'text-green-400', bg: 'bg-green-900/30', label: 'TAKEN' },
    'stop_hit': { icon: '✕', color: 'text-red-400', bg: 'bg-red-900/30', label: 'STOP' },
    'tp_hit': { icon: '★', color: 'text-green-400', bg: 'bg-green-900/30', label: 'TP' },
    'cancel': { icon: '⊘', color: 'text-yellow-400', bg: 'bg-yellow-900/30', label: 'CANCEL' },
  };
  
  const rows = display.map(e => {
    const config = typeConfig[e.type] || typeConfig['signal_blocked'];
    const time = new Date(e.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    const symbol = (e.symbol || '').replace('-USD', '');
    const pnl = e.pnl_usd != null ? `${e.pnl_usd >= 0 ? '+' : ''}$${e.pnl_usd.toFixed(2)}` : '';
    const value = e.value_usd ? `$${e.value_usd.toFixed(2)}` : '';
    const reason = e.reason || e.gate || '';
    
    return `
      <div class="flex items-center gap-3 py-2 px-3 ${config.bg} rounded-lg mb-1 hover:bg-white/5 transition-colors">
        <span class="text-xs text-gray-500 w-16 shrink-0">${time}</span>
        <span class="${config.color} w-5">${config.icon}</span>
        <span class="font-medium w-12 shrink-0">${symbol}</span>
        <span class="text-xs px-2 py-0.5 rounded ${config.color} bg-black/20 w-16 text-center">${config.label}</span>
        <span class="text-sm flex-1 truncate ${e.pnl_usd != null ? (e.pnl_usd >= 0 ? 'text-green-400' : 'text-red-400') : 'text-gray-400'}">
          ${pnl || value || reason}
        </span>
      </div>
    `;
  }).join('');
  
  container.innerHTML = `
    <div class="space-y-1 max-h-80 overflow-y-auto scrollbar-thin">
      ${rows}
    </div>
    ${events.length > limit ? `
      <button class="w-full text-center text-xs text-gray-500 hover:text-gray-300 py-2 mt-2" id="show-all-events">
        Show all ${events.length} events ↓
      </button>
    ` : ''}
  `;
}

// Convert recent_signals to timeline events format
export function signalsToEvents(signals) {
  return (signals || []).map(s => ({
    ts: s.ts || new Date().toISOString(),
    type: s.taken ? 'signal_taken' : 'signal_blocked',
    symbol: s.symbol,
    gate: s.gate || s.blocking_gate,
    reason: s.reason || s.blocking_reason,
    score: s.score,
    data: { spread_bps: s.spread_bps }
  }));
}

// Convert trades to timeline events format
export function tradesToEvents(trades) {
  return (trades || []).map(t => ({
    ts: t.ts,
    type: t.side === 'buy' ? 'entry' : 'exit',
    symbol: t.symbol,
    value_usd: t.value_usd,
    pnl_usd: t.pnl_usd,
    reason: t.reason
  }));
}

// Merge and sort multiple event sources
export function mergeEvents(...eventArrays) {
  const all = eventArrays.flat().filter(Boolean);
  return all.sort((a, b) => new Date(b.ts) - new Date(a.ts));
}
