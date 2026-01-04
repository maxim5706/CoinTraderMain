import { postJson, getJson } from "./apiClient.js";

let lastAction = null;
let killTimer = null;
let killArmed = false;

export function getLastAction() {
  return lastAction;
}

function setLastAction(label, status, error) {
  lastAction = {
    label,
    status,
    error,
    time: new Date().toISOString(),
  };
  const event = new CustomEvent("last-action", { detail: lastAction });
  window.dispatchEvent(event);
}

export async function sendCommand(command) {
  setLastAction(command, "Working...");
  const res = await postJson(`/api/control/command?command=${command}`);
  if (res.ok && res.data?.success) {
    setLastAction(command, "Success");
  } else {
    setLastAction(command, "Failed", res.data?.error || res.error);
  }
  return res;
}

export async function setMode(mode) {
  setLastAction(`Set Mode ${mode}`, "Working...");
  const statusRes = await getJson("/api/bot/status");
  if (statusRes.ok && statusRes.data?.running) {
    setLastAction(`Set Mode ${mode}`, "Failed", "Stop bot before switching mode");
    return { ok: false, error: "Bot running" };
  }
  const res = await postJson(`/api/control/mode?mode=${mode}`);
  if (res.ok && res.data?.success) setLastAction(`Set Mode ${mode}`, "Success");
  else setLastAction(`Set Mode ${mode}`, "Failed", res.data?.error || res.error);
  return res;
}

export function isKillArmed() {
  return killArmed;
}

export async function toggleKillTwoStep(killActive) {
  // If kill is already active, just resume (no two-step needed)
  if (killActive) {
    setLastAction("Resume Trading", "Working...");
    const res = await postJson("/api/kill");
    if (res.ok) setLastAction("Resume Trading", "Success");
    else setLastAction("Resume Trading", "Failed", res.error || res.data?.error);
    killArmed = false;
    dispatchKillArmedEvent();
    return;
  }
  
  // Two-step arming for activating kill switch
  if (killArmed) {
    // Second click - confirm
    clearKillTimer();
    setLastAction("Kill Switch", "Working...");
    const res = await postJson("/api/kill");
    if (res.ok) setLastAction("Kill Switch", "Success");
    else setLastAction("Kill Switch", "Failed", res.error || res.data?.error);
    killArmed = false;
    dispatchKillArmedEvent();
    return;
  }
  
  // First click - arm
  killArmed = true;
  dispatchKillArmedEvent();
  killTimer = setTimeout(() => {
    killArmed = false;
    killTimer = null;
    dispatchKillArmedEvent();
  }, 3000);
}

function dispatchKillArmedEvent() {
  window.dispatchEvent(new CustomEvent("kill-armed", { detail: { armed: killArmed } }));
}

export function clearKillTimer() {
  if (killTimer) {
    clearTimeout(killTimer);
    killTimer = null;
    killArmed = false;
  }
}
