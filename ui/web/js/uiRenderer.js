import { formatAgeText } from "./utils.js";

export function renderSystemPill(container, derived, state) {
  if (!container) return;
  const fresh = derived.freshness;
  const mode = state?.mode || "paper";
  const isRunning = derived.status === "RUNNING";
  const posCount = state?.positions?.length || 0;
  const pnl = state?.unrealized_pnl || 0;
  const pnlClass = pnl >= 0 ? "text-emerald-400" : "text-red-400";
  const pnlStr = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
  
  // Cyan dot for running (not green - reserve green for PnL)
  const dotClass = !isRunning ? "bg-gray-500" : 
    fresh.status === "OK" ? "bg-cyan-400" : 
    fresh.status === "DEGRADED" ? "bg-yellow-400" : "bg-red-500";
  
  // Mode indicator integrated
  const modeIndicator = mode === "live" 
    ? `<span class="text-[10px] text-red-400 font-medium uppercase tracking-wide">Live</span>`
    : `<span class="text-[10px] text-gray-500 font-medium uppercase tracking-wide">Paper</span>`;
  
  container.innerHTML = `
    <div class="flex items-center gap-2">
      <span class="w-2 h-2 rounded-full ${dotClass} inline-block"></span>
      <span class="text-sm font-medium text-gray-200">${isRunning ? posCount : '—'}</span>
      <span class="text-xs text-gray-500">positions</span>
      <span class="text-gray-600 mx-1">·</span>
      <span class="text-sm ${pnlClass}">${isRunning ? pnlStr : '—'}</span>
      <span class="text-gray-600 mx-1">·</span>
      ${modeIndicator}
    </div>
  `;
}

export function renderHealthStrip(derived, state) {
  const dot = document.getElementById("health-dot");
  const label = document.getElementById("health-status-label");
  const sub = document.getElementById("health-sub");
  const reason = document.getElementById("health-reason");
  const fixit = document.getElementById("fixit-section");
  const fixitCmd = document.getElementById("fixit-cmd");
  const fixitHelper = document.getElementById("fixit-helper");

  // Cyan for OK, yellow for degraded, red for error (not green - reserve for PnL)
  const dotColor = derived.freshness.status === "OK" ? "bg-cyan-400" : 
    derived.freshness.status === "DEGRADED" ? "bg-yellow-400" : "bg-red-500";
  if (dot) dot.className = `w-2 h-2 rounded-full ${dotColor}`;
  if (label) label.textContent = derived.status;
  if (sub) sub.textContent = `Updated ${formatAgeText(derived.freshness.stateAge)} ago`;
  
  // Only show reason if there's a problem
  if (reason) {
    const hasIssue = derived.freshness.status !== "OK";
    reason.classList.toggle("hidden", !hasIssue);
    if (hasIssue) {
      const reasons = derived.freshness.reasons || [];
      reason.textContent = reasons.length ? reasons.slice(0, 2).join(", ") : "";
    }
  }
  
  if (fixit) {
    const show = derived.freshness.status !== "OK";
    fixit.classList.toggle("hidden", !show);
    if (fixitCmd) fixitCmd.textContent = state?.capabilities?.restart_instructions?.bot || "pm2 restart coin-back";
    if (fixitHelper)
      fixitHelper.textContent = derived.freshness.status === "STALE" ? "Bot may need restart" : "";
  }
}

export function renderLastAction(lastAction) {
  const el = document.getElementById("last-action-line");
  if (!el) return;
  if (!lastAction) {
    el.textContent = "Last action: --";
    return;
  }
  const ts = lastAction.time ? new Date(lastAction.time).toLocaleTimeString() : "";
  el.textContent = `Last action: ${lastAction.label} — ${lastAction.status} ${ts} ${
    lastAction.error ? "(" + lastAction.error + ")" : ""
  }`;
}

export function renderControls(derived, capabilities) {
  const primary = document.getElementById("primary-action-btn");
  const restart = document.getElementById("restart-btn");
  const canControl = capabilities?.process_control !== false && derived.freshness.status !== "STALE";
  if (primary) {
    if (derived.status === "RUNNING") {
      primary.textContent = "Stop";
    } else if (derived.status === "TRANSITIONING") {
      primary.textContent = "Working...";
    } else {
      primary.textContent = "Start";
    }
    primary.disabled = !canControl || derived.status === "TRANSITIONING";
  }
  if (restart) {
    restart.disabled = !canControl || derived.status !== "RUNNING";
  }
}

export function renderModeButtons(mode, canSwitch) {
  const paper = document.getElementById("mode-paper-btn");
  const live = document.getElementById("mode-live-btn");
  const header = document.querySelector(".app-header");
  
  if (paper) {
    paper.disabled = !canSwitch;
    paper.className =
      "px-3 py-1.5 text-xs rounded transition-all " +
      (mode === "paper" ? "bg-blue-600 text-white" : "bg-gray-700 text-gray-400 hover:bg-gray-600");
  }
  if (live) {
    live.disabled = !canSwitch;
    live.className =
      "px-3 py-1.5 text-xs rounded transition-all " +
      (mode === "live" ? "bg-red-600 text-white" : "bg-gray-700 text-gray-400 hover:bg-gray-600");
  }
  
  // LIVE header accent styling
  if (header) {
    header.classList.toggle("live-mode", mode === "live");
  }
}

export function renderKillButton(killActive, armed) {
  const btn = document.getElementById("kill-btn");
  if (!btn) return;
  
  if (armed) {
    btn.textContent = "Confirm Kill (3s)";
    btn.className = "btn bg-red-600 hover:bg-red-700 text-white animate-pulse";
  } else if (killActive) {
    btn.textContent = "Resume Trading";
    btn.className = "btn bg-yellow-600 hover:bg-yellow-700 text-white";
  } else {
    btn.textContent = "Kill Switch";
    btn.className = "btn btn-secondary";
  }
}
