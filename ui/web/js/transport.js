import { getJson } from "./apiClient.js";

export function createTransport({ onState, onControl }) {
  let ws = null;
  let reconnects = 0;

  async function poll() {
    const [state, control] = await Promise.all([getJson("/api/state"), getJson("/api/control")]);
    if (state.ok && onState) onState(state.data);
    if (control.ok && onControl) onControl(control.data);
  }

  function connectWs() {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        if (onState) onState(data);
      } catch {
        /* noop */
      }
    };
    ws.onclose = () => {
      reconnects += 1;
      setTimeout(connectWs, Math.min(2000 * reconnects, 10000));
    };
  }

  return {
    start() {
      connectWs();
      poll();
      setInterval(poll, 2000);
    },
  };
}
