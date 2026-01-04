// Derived state helpers
export function computeFreshness(state) {
  if (state?.health) {
    const h = state.health;
    return {
      status: h.status || "STALE",
      stateAge: h.state_age_s ?? h.state_age ?? 999,
      ws: state.heartbeats?.ws ?? 999,
      scanner: state.heartbeats?.scanner ?? 999,
      router: state.heartbeats?.order_router ?? 999,
      reasons: h.reasons || [],
    };
  }

  const ts = state?.ts ? Date.parse(state.ts) : NaN;
  const stateAge = isNaN(ts) ? 999 : Math.max(0, (Date.now() - ts) / 1000);
  const hb = state?.heartbeats || {};
  const ws = hb.ws ?? 999;
  const scanner = hb.scanner ?? 999;
  const router = hb.order_router ?? 999;

  let status = "STALE";
  if (stateAge < 3 && ws < 5 && scanner < 20 && router < 20) {
    status = "OK";
  } else if (stateAge < 15) {
    status = "DEGRADED";
  }

  const reasons = [];
  if (stateAge > 15) reasons.push("state_stale");
  if (ws >= 8) reasons.push("ws_old");
  if (scanner >= 30) reasons.push("scanner_old");
  if (router >= 30) reasons.push("router_old");
  if (state?.rest_rate_degraded) reasons.push("rest_throttling");

  return { status, stateAge, ws, scanner, router, reasons };
}

export function computeUnifiedStatus(control, bot) {
  const cStatus = control?.status || "stopped";
  const bStatus = bot?.bot_status || bot?.status || (bot?.bot_running ? "running" : "stopped");
  let status = bStatus === "running" || bStatus === "Running" ? "running" : cStatus;
  if (["starting", "stopping"].includes(cStatus)) status = "transitioning";
  if (status === "stopped") status = "off";
  return status.toUpperCase();
}

export function computePhase(bot) {
  return bot?.phase || "—";
}

export function computeBanner(derived) {
  const { status, freshness, warmPct, phase } = derived;
  if (status === "ERROR") return "ERROR";
  if (freshness.status === "STALE") return "ERROR";
  if (status === "TRANSITIONING") return "STARTING";
  if (phase !== "trading") return "STARTING";
  if (warmPct < 0.8) return "WARMING";
  if (freshness.status === "DEGRADED") return "DEGRADED";
  return "READY";
}

export function buildDerived(botState, controlState) {
  const freshness = computeFreshness(botState);
  const status = computeUnifiedStatus(controlState, botState);
  const phase = computePhase(botState);
  const warmSymbols = botState?.warm_symbols || 0;
  const total = botState?.symbols_streaming || 0;
  const warmPct = total ? warmSymbols / total : 0;
  const banner = computeBanner({ status, freshness, warmPct, phase });
  return { freshness, status, phase, warmPct, banner };
}
