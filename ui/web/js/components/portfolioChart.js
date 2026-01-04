// Portfolio Chart with Trade Markers
export function renderPortfolioChart(container, snapshots, trades = [], timeframe = '1D') {
  if (!container) return;
  
  const width = container.clientWidth || 600;
  const height = 180;
  const padding = { top: 20, right: 10, bottom: 30, left: 10 };
  
  // Filter snapshots by timeframe
  const now = Date.now();
  const cutoffs = { '1D': 86400000, '1W': 604800000, '1M': 2592000000, 'ALL': Infinity };
  const cutoff = cutoffs[timeframe] || cutoffs['1D'];
  const filtered = snapshots.filter(s => now - new Date(s.ts || s.timestamp).getTime() < cutoff);
  
  if (filtered.length < 2) {
    container.innerHTML = `<div class="text-gray-500 text-center py-8">Not enough data for ${timeframe} chart</div>`;
    return;
  }
  
  // Calculate bounds
  const values = filtered.map(s => s.value || s.total_usd);
  const min = Math.min(...values) * 0.999;
  const max = Math.max(...values) * 1.001;
  const range = max - min || 1;
  const first = values[0];
  const last = values[values.length - 1];
  const change = last - first;
  const changePct = ((change / first) * 100).toFixed(2);
  const isPositive = change >= 0;
  const lineColor = isPositive ? '#2fe6a2' : '#ff4d6d';
  
  // Build path points
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  
  const points = filtered.map((s, i) => {
    const x = padding.left + (i / (filtered.length - 1)) * chartWidth;
    const y = padding.top + chartHeight - ((( s.value || s.total_usd) - min) / range) * chartHeight;
    return { x, y, ts: s.ts || s.timestamp, value: s.value || s.total_usd };
  });
  
  const pathD = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x} ${p.y}`).join(' ');
  
  // Gradient area
  const areaD = pathD + ` L ${points[points.length-1].x} ${height - padding.bottom} L ${padding.left} ${height - padding.bottom} Z`;
  
  // Trade markers
  const tradeMarkers = (trades || []).map(t => {
    const tradeTime = new Date(t.ts).getTime();
    const idx = filtered.findIndex(s => new Date(s.ts).getTime() >= tradeTime);
    if (idx < 0) return '';
    const p = points[idx];
    if (!p) return '';
    const color = t.side === 'buy' ? '#2fe6a2' : '#ff4d6d';
    const icon = t.side === 'buy' ? '▲' : '▼';
    const pnlText = t.pnl_usd ? ` ${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd.toFixed(2)}` : '';
    return `
      <g class="trade-marker cursor-pointer" data-symbol="${t.symbol}" data-side="${t.side}">
        <circle cx="${p.x}" cy="${p.y}" r="5" fill="${color}" stroke="white" stroke-width="1.5"/>
        <title>${t.symbol} ${t.side.toUpperCase()}${pnlText} @ ${new Date(t.ts).toLocaleTimeString()}</title>
      </g>
    `;
  }).join('');
  
  // Time labels
  const timeLabels = [];
  const labelCount = 5;
  for (let i = 0; i < labelCount; i++) {
    const idx = Math.floor((i / (labelCount - 1)) * (filtered.length - 1));
    const p = points[idx];
    const time = new Date(filtered[idx].ts || filtered[idx].timestamp);
    const label = timeframe === '1D' ? time.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'}) : time.toLocaleDateString([], {month: 'short', day: 'numeric'});
    timeLabels.push(`<text x="${p.x}" y="${height - 5}" fill="rgba(255,255,255,0.4)" font-size="10" text-anchor="middle">${label}</text>`);
  }
  
  container.innerHTML = `
    <div class="flex items-center justify-between mb-2">
      <div class="flex gap-2">
        ${['1D', '1W', '1M', 'ALL'].map(tf => `
          <button class="chart-tf-btn px-2 py-1 text-xs rounded ${tf === timeframe ? 'bg-gray-700 text-white' : 'text-gray-400 hover:text-white'}" data-tf="${tf}">${tf}</button>
        `).join('')}
      </div>
      <div class="text-sm">
        <span class="text-gray-400">$${first.toFixed(2)}</span>
        <span class="mx-1">→</span>
        <span class="${isPositive ? 'text-green-400' : 'text-red-400'}">$${last.toFixed(2)} (${isPositive ? '+' : ''}${changePct}%)</span>
      </div>
    </div>
    <svg width="100%" height="${height}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
      <defs>
        <linearGradient id="chartGradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${lineColor}" stop-opacity="0.3"/>
          <stop offset="100%" stop-color="${lineColor}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <path d="${areaD}" fill="url(#chartGradient)"/>
      <path d="${pathD}" fill="none" stroke="${lineColor}" stroke-width="2"/>
      ${tradeMarkers}
      ${timeLabels.join('')}
    </svg>
  `;
  
  // Bind timeframe buttons
  container.querySelectorAll('.chart-tf-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      renderPortfolioChart(container, snapshots, trades, btn.dataset.tf);
    });
  });
}

export function createChartComponent(containerId) {
  const container = document.getElementById(containerId);
  let currentSnapshots = [];
  let currentTrades = [];
  let currentTimeframe = '1D';
  
  return {
    update(snapshots, trades) {
      currentSnapshots = snapshots || [];
      currentTrades = trades || [];
      renderPortfolioChart(container, currentSnapshots, currentTrades, currentTimeframe);
    },
    setTimeframe(tf) {
      currentTimeframe = tf;
      renderPortfolioChart(container, currentSnapshots, currentTrades, currentTimeframe);
    }
  };
}
