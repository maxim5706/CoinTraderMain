// Positions Table - Sortable, groupable positions display
let currentSort = { field: 'value', dir: 'desc' };
let currentGroup = 'none';
let lastPositions = [];  // Store latest positions for sort/group handlers
let lastOptions = {};

export function renderPositionsTable(container, positions = [], options = {}) {
  if (!container) return;
  
  lastPositions = positions;  // Update reference for event handlers
  lastOptions = options;
  const { onClose, onCloseAll, onCloseLosers } = options;
  
  if (positions.length === 0) {
    container.innerHTML = `<div class="text-gray-500 text-center py-8">No open positions</div>`;
    return;
  }
  
  // Sort positions
  const sorted = sortPositions(positions, currentSort.field, currentSort.dir);
  const grouped = groupPositions(sorted, currentGroup);
  
  // Stats
  const winning = positions.filter(p => (p.pnl_usd || 0) > 0).length;
  const losing = positions.filter(p => (p.pnl_usd || 0) < 0).length;
  const totalPnl = positions.reduce((sum, p) => sum + (p.pnl_usd || 0), 0);
  
  // Check if skeleton exists
  const tbody = container.querySelector('#pos-tbody');
  
  if (!tbody) {
    // First render - create full skeleton with design system v2
    container.innerHTML = `
      <div class="flex items-center justify-between mb-5">
        <div class="flex items-center gap-3">
          <select id="pos-sort" class="bg-transparent border border-[var(--border-default)] rounded-lg px-3 py-1.5 text-xs text-[var(--text-label)]">
            <option value="value">Sort: Value</option>
            <option value="pnl">Sort: P&L</option>
            <option value="pnl_pct">Sort: P&L %</option>
            <option value="age">Sort: Age</option>
            <option value="symbol">Sort: Symbol</option>
          </select>
          <select id="pos-group" class="bg-transparent border border-[var(--border-default)] rounded-lg px-3 py-1.5 text-xs text-[var(--text-label)]">
            <option value="none">No grouping</option>
            <option value="pnl">Group: Win/Loss</option>
            <option value="tier">Group: Tier</option>
          </select>
        </div>
        <div class="flex items-center gap-2">
          <button id="close-losers-btn" class="btn btn-ghost text-xs">Close Losers (<span id="losers-count">${losing}</span>)</button>
          <button id="close-all-btn" class="btn btn-danger text-xs">Close All</button>
        </div>
      </div>
      <div class="grid grid-cols-4 gap-5 mb-5">
        <div><div class="t-label">Winning</div><div id="stat-winning" class="t-value pnl-pos">${winning}</div></div>
        <div><div class="t-label">Losing</div><div id="stat-losing" class="t-value pnl-neg">${losing}</div></div>
        <div><div class="t-label">Unrealized</div><div id="stat-pnl" class="t-value mono ${totalPnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">$${totalPnl.toFixed(2)}</div></div>
        <div><div class="t-label">Total</div><div id="stat-total" class="t-value">${positions.length}</div></div>
      </div>
      <div class="overflow-x-auto">
        <table>
          <thead><tr>
            <th>Symbol</th>
            <th class="text-right">Value</th>
            <th class="text-right">P&L</th>
            <th class="text-right">%</th>
            <th class="text-right">Stop</th>
            <th class="text-right">Age</th>
            <th></th>
          </tr></thead>
          <tbody id="pos-tbody">${renderGroupedRows(grouped)}</tbody>
        </table>
      </div>`;
    
    // Bind events once
    bindEvents(container, options);
  } else {
    // Update only data parts - preserve dropdowns
    tbody.innerHTML = renderGroupedRows(grouped);
    updateStats(container, winning, losing, totalPnl, positions.length);
    bindCloseButtons(container, onClose);
  }
}

function bindEvents(container, options) {
  const { onClose, onCloseAll, onCloseLosers } = options;
  
  container.querySelector('#pos-sort')?.addEventListener('change', (e) => {
    currentSort.field = e.target.value;
    // Re-render tbody with latest positions
    const tbody = container.querySelector('#pos-tbody');
    if (tbody) {
      const sorted = sortPositions(lastPositions, currentSort.field, currentSort.dir);
      const grouped = groupPositions(sorted, currentGroup);
      tbody.innerHTML = renderGroupedRows(grouped);
      bindCloseButtons(container, lastOptions.onClose);
    }
  });
  
  container.querySelector('#pos-group')?.addEventListener('change', (e) => {
    currentGroup = e.target.value;
    const tbody = container.querySelector('#pos-tbody');
    if (tbody) {
      const sorted = sortPositions(lastPositions, currentSort.field, currentSort.dir);
      const grouped = groupPositions(sorted, currentGroup);
      tbody.innerHTML = renderGroupedRows(grouped);
      bindCloseButtons(container, lastOptions.onClose);
    }
  });
  
  container.querySelector('#close-losers-btn')?.addEventListener('click', () => lastOptions.onCloseLosers?.());
  container.querySelector('#close-all-btn')?.addEventListener('click', () => lastOptions.onCloseAll?.());
  bindCloseButtons(container, onClose);
}

function bindCloseButtons(container, onClose) {
  container.querySelectorAll('.close-position-btn').forEach(btn => {
    btn.onclick = () => onClose?.(btn.dataset.symbol);
  });
}

function updateStats(container, winning, losing, totalPnl, total) {
  const w = container.querySelector('#stat-winning');
  const l = container.querySelector('#stat-losing');
  const p = container.querySelector('#stat-pnl');
  const t = container.querySelector('#stat-total');
  const lc = container.querySelector('#losers-count');
  if (w) w.textContent = winning;
  if (l) l.textContent = losing;
  if (p) { p.textContent = `$${totalPnl.toFixed(2)}`; p.className = `t-value mono ${totalPnl >= 0 ? 'pnl-pos' : 'pnl-neg'}`; }
  if (t) t.textContent = total;
  if (lc) lc.textContent = losing;
}

function sortPositions(positions, field, dir) {
  const mult = dir === 'desc' ? -1 : 1;
  return [...positions].sort((a, b) => {
    let aVal, bVal;
    switch (field) {
      case 'value':
        aVal = a.size_usd || a.value_usd || 0;
        bVal = b.size_usd || b.value_usd || 0;
        break;
      case 'pnl':
        aVal = a.pnl_usd || 0;
        bVal = b.pnl_usd || 0;
        break;
      case 'pnl_pct':
        aVal = a.pnl_pct || 0;
        bVal = b.pnl_pct || 0;
        break;
      case 'age':
        aVal = a.hold_minutes || a.age_minutes || 0;
        bVal = b.hold_minutes || b.age_minutes || 0;
        break;
      case 'symbol':
        aVal = a.symbol || '';
        bVal = b.symbol || '';
        return mult * aVal.localeCompare(bVal);
      default:
        aVal = a.size_usd || a.value_usd || 0;
        bVal = b.size_usd || b.value_usd || 0;
    }
    return mult * (bVal - aVal);
  });
}

function groupPositions(positions, groupBy) {
  if (groupBy === 'none') {
    return { 'All Positions': positions };
  }
  
  if (groupBy === 'pnl') {
    return {
      '🟢 Winning': positions.filter(p => (p.pnl_usd || 0) > 0),
      '🔴 Losing': positions.filter(p => (p.pnl_usd || 0) <= 0)
    };
  }
  
  if (groupBy === 'tier') {
    const groups = {};
    positions.forEach(p => {
      const tier = p.tier || 'standard';
      if (!groups[tier]) groups[tier] = [];
      groups[tier].push(p);
    });
    return groups;
  }
  
  return { 'All': positions };
}

function renderGroupedRows(grouped) {
  let html = '';
  for (const [groupName, positions] of Object.entries(grouped)) {
    if (Object.keys(grouped).length > 1 && positions.length > 0) {
      html += `
        <tr class="border-t border-gray-700">
          <td colspan="7" class="py-2 text-gray-400 font-medium">${groupName}</td>
        </tr>
      `;
    }
    html += positions.map(p => renderPositionRow(p)).join('');
  }
  return html;
}

function renderPositionRow(p) {
  const symbol = (p.symbol || '').replace('-USD', '');
  const value = p.size_usd || p.value_usd || 0;
  const pnl = p.pnl_usd || 0;
  const pnlPct = p.pnl_pct || 0;
  const stop = p.stop_price || 0;
  const age = p.hold_minutes || p.age_minutes || 0;
  const isPositive = pnl >= 0;
  
  const ageStr = age > 1440 ? `${(age / 1440).toFixed(1)}d` : age > 60 ? `${(age / 60).toFixed(1)}h` : `${age}m`;
  
  return `
    <tr>
      <td class="font-medium">${symbol}</td>
      <td class="num">$${value.toFixed(2)}</td>
      <td class="num ${isPositive ? 'pnl-pos' : 'pnl-neg'}">
        ${isPositive ? '+' : ''}$${pnl.toFixed(2)}
      </td>
      <td class="num ${isPositive ? 'pnl-pos' : 'pnl-neg'}">
        ${isPositive ? '+' : ''}${pnlPct.toFixed(1)}%
      </td>
      <td class="num text-label">$${stop.toFixed(4)}</td>
      <td class="num text-meta">${ageStr}</td>
      <td class="text-right">
        <button class="close-position-btn btn btn-ghost text-xs" data-symbol="${p.symbol}">×</button>
      </td>
    </tr>
  `;
}

export function setPositionSort(field, dir = 'desc') {
  currentSort = { field, dir };
}

export function setPositionGroup(group) {
  currentGroup = group;
}
