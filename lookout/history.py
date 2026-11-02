"""Run history and incidents, for the dashboard.

Written in the same transaction as the rest of the state, so a dry run or a
failed delivery leaves no trace here either. Everything stored is code-built:
flag summaries, already-redacted evidence, and the (validated) AI text.

An incident is one occurrence of an issue: it opens when a flag is reported as
new or reappeared, collects updates (worse, reminder), and closes when the
"resolved" notice goes out.
"""
import json

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, started_at REAL, finished_at REAL, mode TEXT,
        collect_ms REAL, detect_ms REAL, llm_ms REAL, report_ms REAL,
        flags INTEGER, reported INTEGER, suppressed INTEGER, resolved INTEGER,
        delivered INTEGER, ai_status TEXT, model TEXT,
        snapshot TEXT,               -- JSON: the status grid at that run (see snapshot())
        trigger TEXT,                -- what started it: timer, docker event, OnFailure hook
        discord INTEGER)""",         # 1 = Discord delivery on, 0 = dashboard only
    """CREATE TABLE IF NOT EXISTS incidents (
        id INTEGER PRIMARY KEY, issue_key TEXT, rule TEXT, kind TEXT, severity TEXT, summary TEXT,
        status TEXT,                 -- open | resolved
        opened_run INTEGER, opened_at REAL, last_seen REAL, resolved_at REAL,
        failed_at REAL, detected_at REAL, time_to_detect_s REAL,
        evidence TEXT,               -- JSON list of redacted log lines
        cause TEXT, cause_matches TEXT,  -- deterministic classification + what matched (JSON)
        ai_status TEXT, ai_model TEXT, ai_summary TEXT, ai_explanation TEXT,
        next_checks TEXT,            -- JSON list of {id, command, why}: read-only, from an allowlist
        updates TEXT,                -- JSON list of {at, reason, severity}
        crash TEXT)""",              # JSON: exit code, OOM, kernel lines, host, recent changes
    "CREATE INDEX IF NOT EXISTS incidents_open ON incidents (issue_key, status)",
]


# Columns added after the first release: (table, column, type). Old state files get them on open.
MIGRATIONS = [("incidents", "crash", "TEXT"), ("runs", "trigger", "TEXT"), ("runs", "discord", "INTEGER")]
PENDING = {"ok": False, "reason": "pending"}   # facts sent; the explanation is still being written


def init(conn):
    for statement in SCHEMA:
        conn.execute(statement)
    for table, column, kind in MIGRATIONS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def snapshot(facts, known_containers=None):
    """The status grid, built from facts by code. Only names, states and numbers:
    no addresses, no log text. Containers are listed if running now or ever seen
    running (so long-dead experiments don't clutter the grid)."""
    known = known_containers or {}
    host = facts.get("host") or {}
    docker = facts.get("docker") or {}
    containers = [
        {"name": c["name"], "state": c["state"], "health": c.get("health", "none"), "status": c.get("status", "")}
        for c in docker.get("containers", [])
        if c["state"] == "running" or known.get(c["name"], {}).get("was_running")]
    return {
        "log_lines": sum((facts.get(n) or {}).get("total", 0) or 0 for n in ("journal", "docker")),
        "collectors": {name: bool((facts.get(name) or {}).get("ok"))
                       for name in ("host", "prometheus", "systemd", "journal", "docker")},
        "host": {
            "memory_available_pct": (host.get("memory") or {}).get("available_pct"),
            "load15": (host.get("load") or {}).get("load15"),
            "cpus": (host.get("load") or {}).get("cpus"),
            "disks": [{"mountpoint": d["mountpoint"], "used_pct": d["used_pct"]} for d in host.get("disks", [])],
            "boot_time": host.get("boot_time"),
        } if host.get("ok") else None,
        "failed_units": [u["unit"] for u in (facts.get("systemd") or {}).get("failed_units", [])],
        "containers": sorted(containers, key=lambda c: c["name"]),
    }


def record(conn, *, started_at, finished_at, mode, timings, result, ai, delivered, snapshot, trigger=None,
           discord=True):
    """Store one run and update incidents. Returns the run id."""
    ai = ai or {}
    run_id = conn.execute(
        "INSERT INTO runs (started_at, finished_at, mode, collect_ms, detect_ms, llm_ms, report_ms, flags, "
        "reported, suppressed, resolved, delivered, ai_status, model, snapshot, trigger, discord) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (started_at, finished_at, mode, timings.get("collect_ms"), timings.get("detect_ms"),
         timings.get("llm_ms"), timings.get("report_ms"), len(result["flags"]), len(result["report"]),
         len(result["suppressed"]), len(result["resolved"]), int(delivered),
         None if not ai else ("ok" if ai.get("ok") else ai.get("reason")), ai.get("model"),
         json.dumps(snapshot), trigger, int(bool(discord)))).lastrowid

    explanations = dict(ai.get("alerts", [])) if ai.get("ok") else {}
    now = finished_at
    for entry in result["report"]:
        now = entry.get("detected_at", finished_at)  # when code saw it, not when the (slow) LLM finished
        row = _open(conn, entry["key"])
        update = {"at": now, "reason": entry["reason"], "severity": entry["severity"]}
        if row is None or entry["reason"] in ("new", "reappeared"):
            if row is not None:  # shouldn't happen, but never leave two open incidents for one issue
                _close(conn, row, now, "superseded")
            failed_at = entry.get("failed_at")
            conn.execute(
                "INSERT INTO incidents (issue_key, rule, kind, severity, summary, status, opened_run, opened_at, "
                "last_seen, failed_at, detected_at, time_to_detect_s, evidence, cause, cause_matches, next_checks, "
                "updates, crash) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (entry["key"], entry.get("rule"), entry.get("kind"), entry["severity"], entry["summary"], run_id,
                 now, now, failed_at, entry.get("detected_at", now), _ttd(entry, now),
                 json.dumps(entry.get("evidence", [])), entry.get("cause"),
                 json.dumps(entry.get("cause_matches", [])), json.dumps(entry.get("next_checks", [])),
                 json.dumps([update]), json.dumps(entry.get("crash")) if entry.get("crash") else None))
            row = _open(conn, entry["key"])
        else:
            conn.execute(
                "UPDATE incidents SET severity = ?, summary = ?, last_seen = ?, evidence = ?, updates = ? "
                "WHERE id = ?",
                (entry["severity"], entry["summary"], now, json.dumps(entry.get("evidence", [])),
                 json.dumps(json.loads(row["updates"]) + [update]), row["id"]))
            if entry.get("cause"):  # a fresh investigation (e.g. it got worse): keep the latest
                conn.execute(
                    "UPDATE incidents SET cause = ?, cause_matches = ?, next_checks = ?, crash = ? WHERE id = ?",
                    (entry["cause"], json.dumps(entry.get("cause_matches", [])),
                     json.dumps(entry.get("next_checks", [])), json.dumps(entry.get("crash")), row["id"]))
        if ai:
            conn.execute(
                "UPDATE incidents SET ai_status = ?, ai_model = ?, ai_summary = ?, ai_explanation = ?, "
                "next_checks = ? WHERE id = ?",
                ("ok" if ai.get("ok") else ai.get("reason"), ai.get("model"), ai.get("summary"),
                 explanations.get(entry["key"]), json.dumps(entry.get("next_checks", [])), row["id"]))
    now = finished_at
    for entry in result["suppressed"]:
        conn.execute("UPDATE incidents SET last_seen = ? WHERE issue_key = ? AND status = 'open'",
                     (now, entry["key"]))
    for entry in result["resolved"]:
        row = _open(conn, entry["key"])
        if row is not None:
            _close(conn, row, now, "resolved")
    for key in result.get("forgotten", []):
        row = _open(conn, key)  # an event (spike, novel error) that stopped and expired
        if row is not None:
            _close(conn, row, now, "expired")
    return run_id


def update_ai(conn, run_id, result, ai, llm_ms):
    """Phase 2 of a run: store the explanation (or why there is none) once the model is done."""
    ai = ai or {}
    status = "ok" if ai.get("ok") else ai.get("reason", "not run")
    conn.execute("UPDATE runs SET llm_ms = ?, ai_status = ?, model = ? WHERE id = ?",
                 (llm_ms, status, ai.get("model"), run_id))
    explanations = dict(ai.get("alerts", [])) if ai.get("ok") else {}
    for entry in result["report"]:
        conn.execute(
            "UPDATE incidents SET ai_status = ?, ai_model = ?, ai_summary = ?, ai_explanation = ?, next_checks = ? "
            "WHERE issue_key = ? AND status = 'open'",
            (status, ai.get("model"), ai.get("summary"), explanations.get(entry["key"]),
             json.dumps(entry.get("next_checks", [])), entry["key"]))


def _ttd(entry, now):
    """Time to detect: when code noticed minus when it broke (only when the failure time is known)."""
    failed_at = entry.get("failed_at")
    return None if failed_at is None else max(0.0, entry.get("detected_at", now) - failed_at)


def _open(conn, key):
    return conn.execute("SELECT * FROM incidents WHERE issue_key = ? AND status = 'open' ORDER BY id DESC",
                        (key,)).fetchone()


def _close(conn, row, now, reason):
    updates = json.loads(row["updates"]) + [{"at": now, "reason": reason, "severity": None}]
    conn.execute("UPDATE incidents SET status = 'resolved', resolved_at = ?, updates = ? WHERE id = ?",
                 (now, json.dumps(updates), row["id"]))
