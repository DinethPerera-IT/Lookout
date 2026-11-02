// Data access for the dashboard: the live read-only API, or the bundled replay dataset.
// Replay is used when the URL has ?replay, or as soon as the API can't be reached
// (e.g. the static folder served on its own, offline). The page then shows DEMO DATA.
"use strict";

const LookoutData = (() => {
  const state = { replay: new URLSearchParams(location.search).has("replay"), listeners: [] };

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
        if (r.ok) return await r.json();
        if (r.status === 404) return null;
        // 503 = no data yet (fresh install): fall through to replay
      } catch (e) { /* unreachable: fall through */ }
      switchToReplay();
    }
    const r = await fetch(replayFile(path), { cache: "no-store" });
    return r.ok ? r.json() : null;
  }

  // Live updates over Server-Sent Events. In replay mode there is nothing to stream.
  function stream(onUpdate) {
    if (state.replay || !window.EventSource) return;
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
