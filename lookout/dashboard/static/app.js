"use strict";
// Step 1 placeholder view; the full dashboard replaces this in Step 2.
// All log-derived text is inserted with textContent, never innerHTML.

function ago(seconds) {
  if (seconds == null) return "never";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${Math.round(seconds / 3600)} h ago`;
}

function cell(row, text, cls) {
  const td = row.insertCell();
  td.textContent = text;
  if (cls) td.className = cls;
}

async function render() {
  const status = await LookoutData.get("api/status");
  document.getElementById("demo-badge").hidden = !LookoutData.isReplay();
  document.getElementById("heartbeat").textContent = status ? `last run ${ago(status.heartbeat.age_s)}` : "no data";
  const list = await LookoutData.get("api/incidents");
  const body = document.querySelector("#incidents tbody");
  body.replaceChildren();
  for (const inc of (list ? list.incidents : [])) {
    const row = body.insertRow();
    cell(row, inc.severity, `sev-${inc.severity}`);
    cell(row, inc.status);
    cell(row, inc.summary);
  }
}

render();
LookoutData.stream(() => render());
