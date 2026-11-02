"""The dashboard's data: pure functions from a read-only SQLite connection to dicts.

The live server and the replay exporter both call these, so the demo dataset has
exactly the shape (and goes through exactly the same scrubbing) as live data.
"""
import json
from pathlib import Path

from .. import llm

STALE_AFTER = 2 * 3600 + 600   # the timer runs hourly; two missed runs = stale
BENCH_DIR = Path(__file__).resolve().parent.parent.parent / "bench" / "results"
REPLAY_BENCH = Path(__file__).resolve().parent / "static" / "replay" / "benchmark.json"
_JSON_FIELDS = ("evidence", "cause_matches", "next_checks", "updates", "crash")


def _incident(row, full=False):
    d = dict(row)
    for f in _JSON_FIELDS:
        d[f] = json.loads(d[f]) if d.get(f) else ([] if f != "crash" else None)
    if not full:
        d = {k: d[k] for k in ("id", "issue_key", "rule", "kind", "severity", "summary", "status", "opened_at",
                               "last_seen", "resolved_at", "time_to_detect_s", "cause")}
    return d


def _run(row):
    if row is None:
        return None
    d = dict(row)
    d["snapshot"] = json.loads(d["snapshot"]) if d["snapshot"] else None
    d["duration_ms"] = (d["finished_at"] - d["started_at"]) * 1000
    return d


def latest_run(conn, hide_containers=()):
    """The newest run. Containers listed in hide_containers are left out of its snapshot,
    whichever endpoint returns it."""
    run = _run(conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone())
    if run and run["snapshot"] and hide_containers:
        run["snapshot"]["containers"] = [c for c in run["snapshot"].get("containers", [])
                                         if c["name"] not in hide_containers]
    return run


def status(conn, now, hide_containers=()):
    run = latest_run(conn, hide_containers)
    counts = {r["severity"]: r["n"] for r in conn.execute(
        "SELECT severity, count(*) AS n FROM incidents WHERE status = 'open' GROUP BY severity")}
    age = None if run is None else max(0.0, now - run["finished_at"])
    return {
        "now": now,
        "heartbeat": {"last_run_at": run and run["finished_at"], "age_s": age,
                      "stale": age is None or age > STALE_AFTER, "mode": run and run["mode"]},
        "open": {"crit": counts.get("crit", 0), "warn": counts.get("warn", 0)},
        "runs_24h": conn.execute("SELECT count(*) FROM runs WHERE finished_at > ?", (now - 86400,)).fetchone()[0],
        "snapshot": run and run["snapshot"],
    }


def incidents(conn, limit=100):
    # Newest event first: when it opened, or when it resolved. Not last_seen: every later run
    # refreshes an open incident's last_seen, which would push new incidents below old ones.
    rows = conn.execute("SELECT * FROM incidents ORDER BY coalesce(resolved_at, opened_at) DESC, id DESC LIMIT ?",
                        (limit,))
    return {"incidents": [_incident(r) for r in rows]}


def incident(conn, incident_id):
    row = conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
    return None if row is None else _incident(row, full=True)


def runs_latest(conn, recent=24, hide_containers=()):
    run = latest_run(conn, hide_containers)
    if run is not None:
        run["classified"] = conn.execute("SELECT count(*) FROM incidents WHERE opened_run = ? AND cause IS NOT NULL",
                                         (run["id"],)).fetchone()[0]
    rows = conn.execute("SELECT id, finished_at, started_at, flags, reported, suppressed, resolved, ai_status, "
                        "collect_ms, detect_ms, llm_ms, report_ms, trigger FROM runs ORDER BY id DESC LIMIT ?", (recent,))
    return {"run": run, "recent": [dict(r) for r in rows][::-1]}


def signature(conn):
    """Changes whenever a run finishes, an incident changes or an AI explanation arrives
    (phase 2 of a run, about a minute later): the SSE stream's trigger."""
    return tuple(conn.execute(
        "SELECT (SELECT max(id) FROM runs), (SELECT count(*) FROM incidents), "
        "(SELECT max(coalesce(resolved_at, last_seen)) FROM incidents), "
        "(SELECT count(*) FROM incidents WHERE ai_status = 'pending'), "
        "(SELECT total(llm_ms) FROM runs)").fetchone())


def benchmark(bench_dir=BENCH_DIR):
    """The Phase 6 benchmark: summary rows, the pick, per-case failures, and every saved
    answer re-checked with today's validator (the checker improved after the run).
    Without the bench folder (e.g. the installed copy in /opt), the bundled copy is used."""
    files = sorted(Path(bench_dir).glob("*.json"))
    if not files:
        return json.loads(REPLAY_BENCH.read_text()) if REPLAY_BENCH.exists() else None
    data = json.loads(files[-1].read_text())
    cases = {p.name: json.loads(p.read_text()) for p in (Path(bench_dir).parent / "cases").glob("*.json")}
    keep = ("model", "mode", "calls", "median_s", "max_s", "load_s", "gen_tps", "ram_gb", "raw_json",
            "parsed_json", "passed", "invented", "missed_alert", "dismissive", "command", "injection_echo")
    rows, failures = [{k: r.get(k) for k in keep} for r in data["rows"]], []
    for r in data["records"]:
        then = json.loads(r["problems"]) if isinstance(r.get("problems"), str) else (r.get("problems") or [])
        now = None
        if r["case"] in cases and isinstance(r.get("output"), str):
            now = [llm.problem_kind(p) + (": " + p.split(": ", 1)[1] if ": " in p else "")
                   for p in llm.validate(r["output"], cases[r["case"]]["flags"])[1]]
        if then or now:
            failures.append({"model": r["model"], "case": r["case"], "then": then, "now": now})
    for row in rows:
        mine = [r for r in data["records"] if r["model"] == row["model"] and r["mode"] == row["mode"]]
        bad = {f["case"] for f in failures if f["model"] == row["model"] and f["now"]}
        row["passed_now"] = (len(mine) - len(bad)) / len(mine) if mine else None
    return {"meta": data["meta"], "pick": data["pick"], "rows": rows, "failures": failures}
