// Data access for the dashboard: the live read-only API, or the bundled replay dataset.
// Replay is used when the URL has ?replay, or as soon as the API can't be reached
// (e.g. the static folder served on its own, offline). The page then shows DEMO DATA.
"use strict";

const LookoutData = (() => {
  const params = new URLSearchParams(location.search);
  const state = { replay: params.has("replay"), listeners: [] };

  function replayFile(path) {
    const m = path.match(/^api\/incidents\/(\d+)$/);
    if (m) return `replay/incident-${m[1]}.json`;
    return "replay/" + path.replace(/^api\//, "").replace("/", "_") + ".json";
  }

  function switchToReplay() {
    if (!state.replay) {
      state.replay = true;
      state.listeners.forEach((fn) => fn());
    }
  }

  async function get(path) {          // path like "api/status" (relative: works behind any prefix)
    if (!state.replay) {
      try {
        const r = await fetch(path, { cache: "no-store" });
        const json = (r.headers.get("Content-Type") || "").startsWith("application/json");
        if (r.ok && json) return await r.json();
        if (r.status === 404 && json) return null;    // Lookout's API: "no such incident"
        // Anything else means there is no Lookout API here: a static file server (offline,
        // python3 -m http.server answers 404 in HTML) or 503 = no data yet. Use replay.
      } catch (e) { /* unreachable: fall through */ }
      switchToReplay();
    }
    const r = await fetch(replayFile(path), { cache: "no-store" });
    return r.ok ? r.json() : null;
  }

  // Live updates over Server-Sent Events. In replay mode there is nothing to stream.
  function stream(onUpdate) {
    if (state.replay || params.has("nostream") || !window.EventSource) return;   // ?nostream: e.g. for screenshots
    const es = new EventSource("api/stream");
    es.addEventListener("update", (e) => onUpdate(JSON.parse(e.data)));
    es.onerror = () => { if (es.readyState === EventSource.CLOSED) setTimeout(() => stream(onUpdate), 5000); };
  }

  return {
    get, stream,
    isReplay: () => state.replay,
    onReplay: (fn) => state.listeners.push(fn),
  };
})();
