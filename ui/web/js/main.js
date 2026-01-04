import { createTransport } from "./transport.js";
import { buildDerived } from "./stateModel.js";
import { renderSystemPill, renderHealthStrip, renderControls, renderModeButtons, renderLastAction, renderKillButton } from "./uiRenderer.js";
import { sendCommand, setMode, toggleKillTwoStep, getLastAction, isKillArmed } from "./actions.js";
import { postJson } from "./apiClient.js";

// New Robinhood-style components
import { renderPortfolioHero } from "./components/portfolioHero.js";
import { renderPortfolioChart } from "./components/portfolioChart.js";
import { renderTradeTimeline, signalsToEvents, mergeEvents } from "./components/tradeTimeline.js";
import { renderWhyNotTrading } from "./components/whyNotTrading.js";
import { renderPositionsTable } from "./components/positionsTable.js";
import { renderHealthDrawer } from "./components/healthDrawer.js";
import { renderStatsBar } from "./components/statsBar.js";

let botState = {};
let controlState = {};
// coinbaseData removed - all data comes from botState via /api/state
let portfolioHistory = [];
let coverageData = null;
let inFlight = false;

function updateUIFromDerived() {
  const derived = buildDerived(botState, controlState);
  
  // Header controls
  renderSystemPill(document.getElementById("system-pill"), derived, botState);
  renderHealthStrip(derived, botState);
  renderControls(derived, botState.capabilities);
  renderModeButtons(botState.mode, derived.status !== "RUNNING" && derived.status !== "TRANSITIONING");
  renderLastAction(getLastAction());
  renderKillButton(botState.kill_switch, isKillArmed());
  
  // Robinhood-style components
  renderPortfolioHero(document.getElementById("portfolio-hero"), botState);
  renderStatsBar(document.getElementById("stats-bar"), botState);
  renderPortfolioChart(document.getElementById("portfolio-chart"), portfolioHistory, botState.recent_trades || []);
  renderWhyNotTrading(document.getElementById("why-not-trading"), botState);
  
  // Trade timeline from signals
  const events = signalsToEvents(botState.recent_signals || []);
  renderTradeTimeline(document.getElementById("trade-timeline"), events);
  
  // Positions
  renderPositionsTable(document.getElementById("positions-table"), botState.positions || [], {
    onClose: (symbol) => closePosition(symbol),
    onCloseAll: () => closeAllPositions(),
    onCloseLosers: () => closeLosers()
  });
  
  // Health drawer
  renderHealthDrawer(document.getElementById("health-drawer"), botState, coverageData);
}

// Position actions
async function closePosition(symbol) {
  if (!confirm(`Close position ${symbol}?`)) return;
  await postJson("/api/close-position", { symbol });
}

async function closeAllPositions() {
  if (!confirm("Close ALL positions?")) return;
  await postJson("/api/close-all");
}

async function closeLosers() {
  const losers = (botState.positions || []).filter(p => (p.pnl_usd || 0) < 0);
  if (!confirm(`Close ${losers.length} losing positions?`)) return;
  await postJson("/api/close-losers");
}

// coinbaseData fetch removed - data comes from botState via WebSocket

async function fetchPortfolioHistory() {
  try {
    const resp = await fetch("/api/portfolio-history");
    if (resp.ok) {
      const data = await resp.json();
      portfolioHistory = data.snapshots || [];
    }
  } catch (e) { /* ignore */ }
}

async function fetchCoverageData() {
  try {
    const resp = await fetch("/api/coverage");
    if (resp.ok) coverageData = await resp.json();
  } catch (e) { /* ignore */ }
}

function handleState(data) {
  botState = data || {};
  window.capabilities = botState.capabilities || {};
  updateUIFromDerived();
  if (window.updateUI) window.updateUI(botState);
}

function handleControl(data) {
  controlState = data || {};
  updateUIFromDerived();
  if (window.updateControlUI) window.updateControlUI(controlState, botState);
}

function wireControls() {
  const primary = document.getElementById("primary-action-btn");
  const restart = document.getElementById("restart-btn");
  const paper = document.getElementById("mode-paper-btn");
  const live = document.getElementById("mode-live-btn");
  const kill = document.getElementById("kill-btn");
  const copyDiag = document.getElementById("copy-diagnostics-btn");

  if (primary) {
    primary.addEventListener("click", async () => {
      if (inFlight) return;
      const derived = buildDerived(botState, controlState);
      inFlight = true;
      primary.disabled = true;
      if (derived.status === "RUNNING") await sendCommand("stop");
      else await sendCommand("run");
      inFlight = false;
      primary.disabled = false;
    });
  }

  if (restart) {
    restart.addEventListener("click", async () => {
      if (inFlight) return;
      inFlight = true;
      restart.disabled = true;
      await sendCommand("restart");
      inFlight = false;
      restart.disabled = false;
    });
  }

  if (paper) paper.addEventListener("click", () => setMode("paper"));
  if (live)
    live.addEventListener("click", () => {
      const confirmLive = prompt("Type LIVE to confirm real money mode:");
      if (confirmLive === "LIVE") setMode("live");
    });

  if (kill)
    kill.addEventListener("click", () => {
      toggleKillTwoStep(botState.kill_switch);
    });

  if (copyDiag) copyDiag.addEventListener("click", () => window.copyDiagnostics && window.copyDiagnostics());
}

function init() {
  wireControls();
  window.addEventListener("last-action", () => renderLastAction(getLastAction()));
  window.addEventListener("kill-armed", () => renderKillButton(botState.kill_switch, isKillArmed()));
  
  // Fetch additional data on startup and periodically
  fetchPortfolioHistory();
  fetchCoverageData();
  setInterval(fetchPortfolioHistory, 60000);  // Every 60s
  setInterval(fetchCoverageData, 30000);  // Every 30s
  
  const transport = createTransport({ onState: handleState, onControl: handleControl });
  transport.start();
}

document.addEventListener("DOMContentLoaded", init);
