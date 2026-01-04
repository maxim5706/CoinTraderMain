export async function getJson(path) {
  try {
    const resp = await fetch(path);
    if (!resp.ok) return { ok: false, error: `HTTP ${resp.status}` };
    return { ok: true, data: await resp.json() };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }
}

export async function postJson(path, body) {
  try {
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) return { ok: false, error: `HTTP ${resp.status}` };
    return { ok: true, data: await resp.json() };
  } catch (e) {
    return { ok: false, error: e.message || String(e) };
  }
}
