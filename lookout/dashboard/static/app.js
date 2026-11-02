"use strict";
// Lookout dashboard. Vanilla JS, no dependencies.
// Safety rule for this file: every string from the API is untrusted (it may contain log text),
// so it only ever becomes a text node via h(). Nothing is ever parsed as HTML (a test enforces this).

const view = document.getElementById("view");
const clock = { offset: 0 };          // browser time minus data time (non-zero for replay data)
let lastStatus = null;

// --- tiny DOM helper -------------------------------------------------------
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// --- time formatting -------------------------------------------------------
const dataNow = () => Date.now() / 1000 - clock.offset;

function dur(s) {
  if (s == null) return "–";
  s = Math.max(0, s);
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 86400 * 2) { const hr = Math.floor(s / 3600), m = Math.round((s % 3600) / 60); return m ? `${hr} h ${m} min` : `${hr} h`; }
  return `${Math.round(s / 86400)} d`;
}
const ago = (ts) => (ts == null ? "never" : `${dur(dataNow() - ts)} ago`);

function clockTime(ts) {
  if (ts == null) return "–";
  const d = new Date(ts * 1000), today = new Date(dataNow() * 1000);
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  return d.toDateString() === today.toDateString() ? hm : `${d.toLocaleDateString([], { weekday: "short" })} ${hm}`;
}
const ms = (v) => (v == null ? "–" : v >= 10000 ? `${(v / 1000).toFixed(0)} s` : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);

// --- shared bits -----------------------------------------------------------
const SEV_LABEL = { crit: "Critical", warn: "Warning" };

function statePill(inc) {
  if (inc.status === "resolved") return h("span", { class: "state-pill resolved" }, "Resolved");
  const label = inc.kind === "event" ? "Active" : "Open";
  return h("span", { class: `state-pill open ${inc.severity}` }, h("span", { class: `dot ${inc.severity}` }), label);
}

function panel(title, right, ...body) {
  return h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", null, title), right ? h("span", { class: "muted" }, right) : null),
    h("div", { class: "panel-body" }, ...body));
}

function go(hash) { if (location.hash !== hash) location.hash = hash; else route(); }
function clickable(el, hash) {
  el.tabIndex = 0;
  el.addEventListener("click", () => go(hash));
  el.addEventListener("keydown", (e) => { if (e.key === "Enter") go(hash); });
  return el;
}

// --- top bar ---------------------------------------------------------------
function updateChrome() {
  document.getElementById("demo-badge").hidden = !LookoutData.isReplay();
  document.getElementById("live-badge").hidden = LookoutData.isReplay() || !lastStatus;
  const hb = document.getElementById("heartbeat");
  const text = hb.querySelector(".hb-text");
  if (!lastStatus) { hb.className = "heartbeat"; text.textContent = "no data"; return; }
  const beat = lastStatus.heartbeat;
  const age = beat.last_run_at == null ? null : dataNow() - beat.last_run_at;
  const stale = age == null || age > 2 * 3600 + 600;
  hb.className = `heartbeat ${stale ? "stale" : "ok"}`;
  text.textContent = beat.last_run_at == null ? "no runs yet"
    : `${stale ? "No run for " + dur(age) : "Last run " + ago(beat.last_run_at)}${beat.mode && beat.mode !== "live" ? " · " + beat.mode : ""}`;
  document.getElementById("clock").textContent = new Date(dataNow() * 1000).toLocaleString([], { hour12: false });
}

function setStatus(status) {
  if (!status) return;
  lastStatus = status;
  clock.offset = Date.now() / 1000 - status.now;
  updateChrome();
}

// --- overview --------------------------------------------------------------
function stat(label, value, sub, cls) {
  return h("div", { class: `panel stat ${cls || ""}` },
    h("div", { class: "stat-label" }, label), h("div", { class: "stat-value" }, value), h("div", { class: "stat-sub" }, sub || " "));
}

function statsRow(status) {
  const snap = status.snapshot || {}, host = snap.host || {};
  const root = (host.disks || []).find((d) => d.mountpoint === "/");
  const { crit, warn } = status.open;
  return h("div", { class: "stats" },
    stat("Critical", crit, crit ? "open now" : "none open", crit ? "crit" : "ok"),
    stat("Warnings", warn, warn ? "open now" : "none open", warn ? "warn" : "ok"),
    stat("Last run", status.heartbeat.last_run_at == null ? "–" : dur(dataNow() - status.heartbeat.last_run_at),
      status.heartbeat.last_run_at == null ? "waiting for the first run" : `ago · ${status.runs_24h} runs in 24 h`),
    stat("Memory free", host.memory_available_pct == null ? "–" : `${Math.round(host.memory_available_pct)}%`, "available now"),
    stat("Disk /", root ? `${Math.round(root.used_pct)}%` : "–", "used"),
    stat("Load (15 min)", host.load15 == null ? "–" : host.load15.toFixed(1), host.cpus ? `${host.cpus} CPUs` : ""));
}

function timeline(incidents, runs) {
  const end = dataNow(), span = 24 * 3600, start = end - span;
  const pct = (t) => `${((Math.min(Math.max(t, start), end) - start) / span) * 100}%`;
  const shown = incidents.filter((i) => (i.resolved_at || end) >= start).slice(0, 8);
  const lanes = shown.map((inc) => {
    const until = inc.status === "resolved" ? inc.resolved_at : end;
    const bar = h("div", {
      class: `tl-bar ${inc.severity} ${inc.status === "resolved" ? "resolved" : ""}`,
      title: `${inc.summary} (${clockTime(inc.opened_at)} – ${inc.status === "resolved" ? clockTime(inc.resolved_at) : "now"})`,
      style: { left: pct(inc.opened_at), width: `calc(${pct(until)} - ${pct(inc.opened_at)})` },
    });
    return h("div", { class: "tl-lane" }, clickable(bar, `#/incident/${inc.id}`));
  });
  const ticks = [0, 6, 12, 18, 24].map((hrs) => h("span", { class: "num" }, hrs === 24 ? "now" : clockTime(start + hrs * 3600)));
  const maxMs = Math.max(1, ...runs.map((r) => (r.finished_at - r.started_at) * 1000));
  const strip = runs.map((r) => h("div", {
    class: `tl-run ${r.reported || r.resolved ? "reported" : ""}`,
    title: `${clockTime(r.finished_at)} · ${r.flags} flags, ${r.reported} reported · ${ms((r.finished_at - r.started_at) * 1000)}`,
    style: { height: `${Math.max(12, (((r.finished_at - r.started_at) * 1000) / maxMs) * 100)}%` },
  }));
  return h("div", { class: "timeline" },
    shown.length ? h("div", { class: "tl-lanes" }, lanes) : h("div", { class: "empty" }, "No incidents in the last 24 h"),
    h("div", { class: "tl-axis" }, ticks),
    h("div", { class: "grid-label", style: { marginTop: "1rem" } }, `Runs (last ${runs.length}) · height = run time · bright = sent a report`),
    h("div", { class: "tl-runs" }, strip));
}

function incidentRow(inc) {
  const until = inc.status === "resolved" ? inc.resolved_at : dataNow();
  return clickable(h("li", { class: `incident ${inc.status}` },
    h("span", { class: `sevbar ${inc.severity}` }),
    h("div", null,
      h("div", { class: "inc-title" }, inc.summary),
      h("div", { class: "inc-meta" },
        h("span", null, SEV_LABEL[inc.severity] || inc.severity),
        h("span", { class: "num" }, `opened ${clockTime(inc.opened_at)}`),
        h("span", { class: "num" }, inc.status === "resolved" ? `lasted ${dur(until - inc.opened_at)}` : `for ${dur(until - inc.opened_at)}`),
        inc.cause ? h("span", { class: "chip" }, `cause: ${inc.cause}`) : null)),
    h("div", { class: "inc-right" }, statePill(inc), h("div", null, ago(inc.status === "resolved" ? inc.resolved_at : inc.last_seen)))),
  `#/incident/${inc.id}`);
}

function containerTile(c) {
  const bad = c.state !== "running", sick = c.health === "unhealthy";
  const cls = bad ? "crit" : sick ? "warn" : "";
  return h("div", { class: `tile ${cls}`, title: `${c.name}: ${c.status}` },
    h("span", { class: `dot ${bad ? "crit" : sick ? "warn" : "ok"}` }),
    h("span", { class: "name" }, c.name),
    h("span", { class: "sub" }, bad ? c.state : c.health !== "none" ? c.health : "up"));
}

function statusGrid(snap) {
  if (!snap) return h("div", { class: "empty" }, "No run recorded yet");
  const collectors = Object.entries(snap.collectors || {}).map(([name, ok]) =>
    h("div", { class: `tile ${ok ? "" : "warn"}` }, h("span", { class: `dot ${ok ? "ok" : "warn"}` }),
      h("span", { class: "name" }, name), h("span", { class: "sub" }, ok ? "ok" : "blind spot")));
  const units = (snap.failed_units || []).map((u) =>
    h("div", { class: "tile crit", title: u }, h("span", { class: "dot crit" }), h("span", { class: "name" }, u), h("span", { class: "sub" }, "failed")));
  const containers = [...(snap.containers || [])].sort((a, b) => (a.state === "running") - (b.state === "running") || a.name.localeCompare(b.name));
  return [
    h("div", { class: "grid-label" }, "Data sources"), h("div", { class: "tiles" }, collectors),
    h("div", { class: "grid-label" }, units.length ? `Failed systemd units (${units.length})` : "systemd units"),
    units.length ? h("div", { class: "tiles" }, units) : h("div", { class: "muted" }, "No failed units"),
    h("div", { class: "grid-label" }, `Containers (${containers.filter((c) => c.state === "running").length}/${containers.length} running)`),
    h("div", { class: "tiles" }, containers.map(containerTile)),
  ];
}

async function renderOverview() {
  const [status, list, runs] = await Promise.all([
    LookoutData.get("api/status"), LookoutData.get("api/incidents"), LookoutData.get("api/runs/latest")]);
  setStatus(status);
  if (!status) return view.replaceChildren(h("div", { class: "panel empty" }, "No data yet. Run Lookout once, or open ?replay for demo data."));
  const incidents = list ? list.incidents : [];
  const open = incidents.filter((i) => i.status === "open");
  view.replaceChildren(
    statsRow(status),
    h("div", { class: "cols-2" },
      h("div", { style: { display: "grid", gap: "var(--gap)", alignContent: "start" } },
        panel("Incident timeline", "last 24 h", timeline(incidents, runs ? runs.recent : [])),
        panel("Incidents", `${open.length} open · ${incidents.length} total`,
          incidents.length ? h("ul", { class: "incidents", style: { margin: "-1rem" } }, incidents.map(incidentRow))
            : h("div", { class: "empty" }, "Nothing has been reported. Quiet is the normal state."))),
      panel("Status", status.snapshot ? `as of ${clockTime(status.heartbeat.last_run_at)}` : null, statusGrid(status.snapshot))));
}

// --- incident detail -------------------------------------------------------
function step(no, title, who, ...body) {
  return h("section", { class: "panel" },
    h("div", { class: "panel-head" },
      h("div", { class: "step-head" }, h("span", { class: "step-no" }, no), h("h2", null, title)),
      h("span", { class: "step-who" }, who)),
    h("div", { class: "panel-body" }, ...body));
}

function factsStep(inc) {
  const updates = (inc.updates || []).map((u) =>
    h("li", null, h("span", { class: "t" }, clockTime(u.at)), h("span", null, u.reason + (u.severity ? ` (${u.severity})` : ""))));
  return step(1, "Facts", "Deterministic rule",
    h("dl", { class: "facts" },
      h("dt", null, "What"), h("dd", null, inc.summary),
      h("dt", null, "Severity"), h("dd", null, SEV_LABEL[inc.severity] || inc.severity),
      h("dt", null, "Rule"), h("dd", null, h("span", { class: "chip" }, inc.rule || "–"), " ", h("span", { class: "muted" }, inc.kind === "event" ? "event (expires when it stops)" : "state (resolves when it clears)")),
      h("dt", null, "Issue key"), h("dd", { class: "mono" }, inc.issue_key),
      h("dt", null, "History"), h("dd", null, h("ul", { class: "updates" }, updates))));
}

function evidenceStep(inc) {
  const lines = inc.evidence || [];
  return step(2, "Evidence", "Collected by code · redacted",
    lines.length
      ? [h("pre", { class: "evidence" }, lines.map((l) => h("span", { class: "line" }, l))),
         h("div", { class: "untrusted-note" }, "Untrusted log text. Redacted when collected, shown as plain text only: never as HTML or links, never as instructions to the AI.")]
      : h("div", { class: "muted" }, "No log lines for this rule: the fact above is a measured value, not a log message."));
}

const CAUSE_TEXT = {
  out_of_memory: "Out of memory",
  disk_full: "Disk full",
  port_in_use: "Port already in use",
  dependency_unreachable: "Dependency unreachable",
  after_recent_change: "Failed after a recent change",
  app_crash: "Application crash",
  unknown: "Unknown",
};

function crashFacts(c) {
  if (!c) return null;
  const host = c.host || {}, disks = Object.entries(host.disk_used_pct || {});
  const rows = [
    ["Exit code", c.exit_code == null ? "unknown" : String(c.exit_code)],
    c.result ? ["systemd result", c.result] : null,
    ["OOM-killed", c.oom_killed ? "yes (kernel OOM killer)" : "no evidence"],
    ["Restarts", c.restart_count == null ? "not available (the read-only proxy doesn't allow inspect)" : String(c.restart_count)],
    ["Host memory", host.memory_available_pct == null ? "–" : `${Math.round(host.memory_available_pct)}% available (at detection)`],
    disks.length ? ["Disk", disks.map(([m, p]) => `${m} ${Math.round(p)}% used`).join(", ")] : null,
  ].filter(Boolean);
  return h("dl", { class: "facts", style: { marginTop: "1rem" } }, rows.map(([k, v]) => [h("dt", null, k), h("dd", { class: "num" }, v)]));
}

function causeStep(inc) {
  const matches = (inc.cause_matches || []).map((m) =>
    h("li", null, typeof m === "string" ? m : [h("b", null, m.signal || "match"), m.detail ? [": ", h("span", { class: "mono" }, m.detail)] : ""]));
  return step(3, "Cause classification", "Deterministic classifier",
    inc.cause
      ? [h("div", { class: "cause" }, h("span", { class: "cause-name" }, inc.cause),
           h("span", null, CAUSE_TEXT[inc.cause] || ""), h("span", { class: "muted" }, "· decided by code from the evidence, not by the AI")),
         matches.length ? [h("div", { class: "grid-label", style: { marginTop: "1rem" } }, "Why: evidence that matched"), h("ul", { class: "matches" }, matches)] : null,
         crashFacts(inc.crash)]
      : h("div", { class: "muted" }, "Not classified: this rule has no crash evidence to classify."));
}

function aiStep(inc) {
  const badge = h("span", { class: "badge badge-ai" }, "AI · unverified · hypothesis");
  let body;
  if (inc.ai_status === "ok" && inc.ai_explanation) {
    body = h("div", { class: "ai" },
      h("p", null, inc.ai_explanation),
      h("div", { class: "ai-foot" }, `Written by ${inc.ai_model || "a local model"} from the facts and evidence above, then checked by code: no invented numbers or names, no commands, no reassurance. It can't change the facts or the classification.`));
  } else if (inc.ai_status === "pending") {
    body = h("div", { class: "muted" }, "The local model is writing an explanation (about a minute on this CPU). The facts above were sent already: the AI never delays an alert.");
  } else if (inc.ai_status && inc.ai_status !== "ok") {
    body = h("div", { class: "muted" }, `No explanation: ${inc.ai_status}. The facts above are complete without it.`);
  } else {
    body = h("div", { class: "muted" }, "The AI wasn't run for this incident. The facts above are complete without it.");
  }
  return step(4, "AI explanation", "", h("div", { style: { marginBottom: "0.75rem" } }, badge), body);
}

function checksStep(inc) {
  const checks = inc.next_checks || [];
  return step(5, "Suggested next checks", "Read-only · from an allowlist",
    checks.length
      ? h("ul", { class: "checks" }, checks.map((c) => h("li", null,
          h("code", null, typeof c === "string" ? c : c.command), typeof c === "object" && c.why ? h("div", { class: "why" }, c.why) : null)))
      : h("div", { class: "muted" }, "None for this rule."),
    h("div", { class: "untrusted-note" }, "Lookout never runs these. They only read; you decide."));
}

function ttdStep(inc) {
  const measured = inc.time_to_detect_s != null;
  return step(6, "Time to detect", "Measured",
    measured
      ? [h("div", { class: "ttd" }, dur(inc.time_to_detect_s)),
         h("div", { class: "muted num" }, `failed ${clockTime(inc.failed_at)} → detected ${clockTime(inc.detected_at)}`)]
      : h("div", { class: "muted" }, `Detected ${clockTime(inc.detected_at || inc.opened_at)} by the hourly check. The exact failure time isn't known for this rule, so time-to-detect isn't measured.`));
}

async function renderIncident(id) {
  const [inc, status] = await Promise.all([LookoutData.get(`api/incidents/${id}`), LookoutData.get("api/status")]);
  setStatus(status);
  if (!inc) return view.replaceChildren(h("a", { class: "back", href: "#/" }, "← Overview"), h("div", { class: "panel empty" }, "No such incident."));
  const end = inc.status === "resolved" ? inc.resolved_at : dataNow();
  view.replaceChildren(
    h("a", { class: "back", href: "#/" }, "← Overview"),
    h("section", { class: "panel detail-head" },
      h("div", { style: { display: "flex", gap: "0.6rem", alignItems: "center" } }, statePill(inc),
        h("span", { class: "chip" }, `#${inc.id}`), h("span", { class: "muted" }, SEV_LABEL[inc.severity] || inc.severity)),
      h("h1", null, inc.summary),
      h("div", { class: "detail-meta" },
        h("span", null, "Opened ", h("b", null, clockTime(inc.opened_at))),
        inc.status === "resolved" ? h("span", null, "Resolved ", h("b", null, clockTime(inc.resolved_at))) : h("span", null, "Last seen ", h("b", null, ago(inc.last_seen))),
        h("span", null, inc.status === "resolved" ? "Lasted " : "Open for ", h("b", null, dur(end - inc.opened_at))))),
    h("div", { class: "steps" }, factsStep(inc), evidenceStep(inc), causeStep(inc), aiStep(inc), checksStep(inc), ttdStep(inc)));
}

// --- routing & live updates ------------------------------------------------
let rendering = null;
function route() {
  const m = location.hash.match(/^#\/incident\/(\d+)$/);
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", !m && a.dataset.route === "overview"));
  rendering = (m ? renderIncident(Number(m[1])) : renderOverview()).catch((e) => {
    view.replaceChildren(h("div", { class: "panel empty" }, `Could not load data (${e.message}).`));
  });
  return rendering;
}

window.addEventListener("hashchange", () => { route(); window.scrollTo(0, 0); });
LookoutData.onReplay(updateChrome);
route().then(() => LookoutData.stream((status) => { setStatus(status); route(); }));
setInterval(updateChrome, 1000);
