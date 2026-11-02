"""History recording, the read-only API server, and the no-leak guarantee."""
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lookout import history
from lookout.dashboard import api, sample
from lookout.dashboard.scrub import host_names
from lookout.dashboard.server import connect_ro, make_server
from lookout.state import State

REPLAY = Path(__file__).resolve().parent.parent / "lookout" / "dashboard" / "static" / "replay"


def result(report=(), suppressed=(), resolved=(), forgotten=()):
    return {"flags": list(report), "report": list(report), "suppressed": list(suppressed),
            "resolved": list(resolved), "forgotten": list(forgotten)}


def entry(key="failed_unit:caddy.service", reason="new", severity="crit", evidence=()):
    return {"key": key, "reason": reason, "severity": severity, "summary": f"{key} is broken",
            "rule": "failed_units", "kind": "state", "evidence": list(evidence)}


def record(state, res, now, ai=None):
    return history.record(state.conn, started_at=now - 5, finished_at=now, mode="live",
                          timings={"collect_ms": 4000.0, "detect_ms": 20.0}, result=res, ai=ai,
                          delivered=bool(res["report"]), snapshot={"containers": []})


def incidents(state):
    return [dict(r) for r in state.conn.execute("SELECT * FROM incidents ORDER BY id")]


# --- history ----------------------------------------------------------------

def test_incident_lifecycle_open_update_resolve_reappear():
    s = State(":memory:")
    record(s, result([entry()]), 1000)
    record(s, result(suppressed=[{"key": entry()["key"], "reason": "cooldown"}]), 2000)
    record(s, result([entry(reason="reminder")]), 3000)
    record(s, result(resolved=[{"key": entry()["key"], "summary": "x"}]), 4000)
    record(s, result([entry(reason="reappeared")]), 5000)
    rows = incidents(s)
    assert [(r["status"], r["opened_at"]) for r in rows] == [("resolved", 1000), ("open", 5000)]
    assert rows[0]["resolved_at"] == 4000 and rows[0]["last_seen"] == 3000
    assert [u["reason"] for u in json.loads(rows[0]["updates"])] == ["new", "reminder", "resolved"]


def test_expired_event_closes_its_incident():
    s = State(":memory:")
    record(s, result([entry("error_spike:x", severity="warn")]), 1000)
    record(s, result(forgotten=["error_spike:x"]), 9000)
    (row,) = incidents(s)
    assert row["status"] == "resolved" and json.loads(row["updates"])[-1]["reason"] == "expired"


def test_ai_text_and_timings_are_stored():
    s = State(":memory:")
    ai = {"ok": True, "model": "llama3.2:3b", "summary": "Caddy stopped.",
          "alerts": [("failed_unit:caddy.service", "The web server stopped.")]}
    record(s, result([entry()]), 1000, ai=ai)
    (row,) = incidents(s)
    assert (row["ai_status"], row["ai_model"], row["ai_explanation"]) == ("ok", "llama3.2:3b", "The web server stopped.")
    run = dict(s.conn.execute("SELECT * FROM runs").fetchone())
    assert run["collect_ms"] == 4000.0 and run["ai_status"] == "ok" and run["reported"] == 1


def test_failed_ai_is_recorded_with_its_reason_not_text():
    s = State(":memory:")
    record(s, result([entry()]), 1000, ai={"ok": False, "reason": "Ollama unreachable"})
    (row,) = incidents(s)
    assert row["ai_status"] == "Ollama unreachable" and row["ai_explanation"] is None


def test_snapshot_lists_only_containers_ever_seen_running():
    facts = {"docker": {"ok": True, "containers": [
        {"name": "grafana", "state": "running", "status": "Up 2 days", "health": "none"},
        {"name": "commoncents", "state": "exited", "status": "Exited (137)", "health": "none"},
        {"name": "clever_swirles", "state": "exited", "status": "Exited (1) 3 months ago", "health": "none"}]}}
    snap = history.snapshot(facts, {"commoncents": {"was_running": 1}})
    assert [c["name"] for c in snap["containers"]] == ["commoncents", "grafana"]
    assert snap["collectors"]["docker"] is True and snap["collectors"]["host"] is False


# --- server -----------------------------------------------------------------

LEAKY = [  # what a hostile or careless log line might carry
    "connect to 192.168.1.50:5432 failed", "listen tcp [fe80::1ff:fe23:4567:890a]:80",
    "admin@example.com logged in", "wlan0: 3c:22:fb:12:34:56 associated with SSID 'Flat 12 Smith St'",
    "Authorization: Bearer abcdefghijklmnop1234", "password=hunter2 rejected",
    "resolved grafana.myhomelab.internal", "peer laptop.tail1a2b3c.ts.net", f"/home/{host_names()[-1]}/app",
]


@pytest.fixture
def server(tmp_path):
    db = tmp_path / "state.sqlite"
    s = State(db)
    record(s, result([entry(evidence=LEAKY), entry("target_down:node:10.0.0.7:9100", severity="warn")]), 1000)
    s.commit()
    s.close()
    srv = make_server(db, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.stopping.set()
    srv.shutdown()
    srv.server_close()


def get(srv, path, method="GET"):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


ENDPOINTS = ["/api/status", "/api/incidents", "/api/incidents/1", "/api/incidents/2", "/api/runs/latest",
             "/api/benchmark"]


@pytest.mark.parametrize("path", ENDPOINTS)
def test_endpoints_answer_json(server, path):
    code, headers, body = get(server, path)
    assert code == 200 and headers["Content-Type"].startswith("application/json")
    assert "default-src 'self'" in headers["Content-Security-Policy"]
    json.loads(body)


def test_unknown_incident_is_404_and_writes_are_405(server):
    assert get(server, "/api/incidents/999")[0] == 404
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert get(server, "/api/incidents", method)[0] == 405


def test_static_files_cannot_escape_the_static_dir(server):
    assert get(server, "/")[0] == 200
    for path in ("/../server.py", "/%2e%2e/server.py", "/..%2f..%2fconfig.toml", "/replay/../../api.py"):
        assert get(server, path)[0] == 404


def test_database_is_opened_read_only(tmp_path):
    db = tmp_path / "state.sqlite"
    s = State(db)
    s.commit()
    s.close()
    conn = connect_ro(db)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO runs (mode) VALUES ('x')")
    with pytest.raises(sqlite3.OperationalError):
        connect_ro(tmp_path / "missing.sqlite")  # never creates a database
    assert not (tmp_path / "missing.sqlite").exists()


def test_missing_database_means_503_not_a_crash(tmp_path):
    srv = make_server(tmp_path / "missing.sqlite", "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert get(srv, "/api/status")[0] == 503
    finally:
        srv.shutdown()
        srv.server_close()


def test_stream_sends_an_update_event(server):
    url = f"http://127.0.0.1:{server.server_address[1]}/api/stream"
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.headers["Content-Type"] == "text/event-stream"
        assert r.readline() == b"event: update\n"
        assert json.loads(r.readline().removeprefix(b"data: "))["open"] == {"crit": 1, "warn": 1}


# --- nothing sensitive on screen --------------------------------------------

SENSITIVE = {
    "ipv4": re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"),
    "ipv6": re.compile(r"(?i)\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){3,7}\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "mac": re.compile(r"(?i)\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b"),
    "internal_host": re.compile(r"(?i)\b[\w-]+\.(?:internal|local|lan|ts\.net)\b"),
    "bearer": re.compile(r"(?i)bearer\s+[\w.-]{8,}"),
    "password": re.compile(r"(?i)password\s*[=:]\s*(?!<)\w"),
    "ssid": re.compile(r"Smith St"),
    "own_hostname": re.compile(r"(?i)\b(?:" + "|".join(map(re.escape, host_names())) + r")\b"),
}


def leaks(text):
    return {name: p.findall(text) for name, p in SENSITIVE.items() if p.search(text)}


def test_the_leaky_fixture_really_is_leaky():
    assert set(leaks("\n".join(LEAKY))) == set(SENSITIVE)  # otherwise the scan below proves nothing


def test_no_api_response_leaks_anything(server):
    for path in ENDPOINTS:
        assert leaks(get(server, path)[2].decode()) == {}, path
    url = f"http://127.0.0.1:{server.server_address[1]}/api/stream"
    with urllib.request.urlopen(url, timeout=5) as r:
        r.readline()
        assert leaks(r.readline().decode()) == {}


def test_bundled_replay_dataset_leaks_nothing():
    files = list(REPLAY.glob("*.json"))
    assert files, "run: python -m lookout.dashboard.sample"
    for f in files:
        assert leaks(f.read_text()) == {}, f.name


def test_replay_dataset_is_current(tmp_path, cfg):
    """The bundled files match what the builder produces now (rebuild after changing the API).
    A recorded bundle (--from-db) can't be rebuilt here; its shape is checked instead."""
    manifest = json.loads((REPLAY / "manifest.json").read_text())
    if not manifest["synthetic"]:
        status = json.loads((REPLAY / "status.json").read_text())
        incidents = json.loads((REPLAY / "incidents.json").read_text())["incidents"]
        assert status["heartbeat"]["last_run_at"] and incidents
        assert all("lookout-demo" in i["issue_key"] for i in incidents)
        assert all((REPLAY / f"incident-{i['id']}.json").exists() for i in incidents)
        return
    state, now = sample.build(cfg, end=json.loads((REPLAY / "manifest.json").read_text())["now"] - 7 * 60 + 3600)
    try:
        sample.export(state, now, tmp_path)
    finally:
        state.close()
    for f in tmp_path.glob("*.json"):
        if f.name != "manifest.json":
            assert json.loads(f.read_text()) == json.loads((REPLAY / f.name).read_text()), f.name


# --- frontend guarantees ----------------------------------------------------

STATIC = REPLAY.parent


def test_frontend_never_injects_html():
    for f in STATIC.glob("*.js"):
        code = f.read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert sink not in code, f"{f.name} uses {sink}"


def test_frontend_works_offline_no_external_urls():
    for f in [*STATIC.glob("*.js"), *STATIC.glob("*.css"), *STATIC.glob("*.html")]:
        # SVG's fixed namespace identifies DOM elements; it makes no network request.
        code = f.read_text().replace('"http://www.w3.org/2000/svg"', '"SVG_NAMESPACE"')
        assert not re.search(r"(?:https?:)?//[\w.-]+\.\w", code), f"{f.name} references an external URL"


# --- step 7 pages -------------------------------------------------------------

def test_security_examples_are_built_by_the_real_code_and_the_after_side_is_clean():
    sec = sample.security()
    for r in sec["redaction"]:
        assert r["rules"] and leaks(r["after"]) == {}, r["after"]
        assert "hunter2" not in r["after"] and "FakePassw0rd" not in r["after"]
    for inj in sec["injections"]:
        assert {"dismissive wording", "repeats untrusted content", "suggests a command"} <= set(inj["rejected_because"])
        assert "@everyone" not in inj["discord"] and inj["discord"].startswith("```")
    assert any("events" in line for line in sec["proxy_allowlist"])
    assert json.loads((STATIC / "security.json").read_text()) == json.loads(json.dumps(sec, ensure_ascii=False)), \
        "rebuild: python -m lookout.dashboard.sample"


def test_benchmark_recheck_tells_the_honest_story():
    b = api.benchmark()
    by_model = {r["model"]: r for r in b["rows"]}
    assert b["pick"][0] == "llama3.2:3b" and by_model["llama3.2:3b"]["passed_now"] == 1
    tailscale = next(f for f in b["failures"] if f["case"] == "4_wifi_flapping.json")
    assert tailscale["then"] and tailscale["now"] == []                  # the old checker's false alarm
    echo = next(f for f in b["failures"] if f["case"] == "7_injection_caddy.json")
    assert any("repeats untrusted content" in p for p in echo["now"])  # qwen's real failure stays
    assert by_model["phi4-mini"]["passed_now"] < 1                       # caught by today's checker


def test_proxy_proof_is_recorded_not_run_from_the_dashboard():
    record = json.loads((STATIC / "proxy-check.json").read_text())
    assert record["passed"] == record["total"] and any(c["method"] == "POST" for c in record["checks"])
    server_code = (STATIC.parent / "server.py").read_text() + (STATIC.parent / "api.py").read_text()
    assert "2375" not in server_code and "proxy_url" not in server_code   # the dashboard never talks to Docker


def test_hidden_containers_stay_off_the_grid_but_nothing_else_changes(tmp_path):
    db = tmp_path / "state.sqlite"
    s = State(db)
    history.record(s.conn, started_at=1, finished_at=2, mode="live", timings={}, result=result(), ai=None,
                   delivered=False, snapshot={"containers": [{"name": "private-app", "state": "running"},
                                                              {"name": "grafana", "state": "running"}]})
    s.commit()
    s.close()
    srv = make_server(db, "127.0.0.1", 0, hide_containers=["private-app"])
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        bodies = {path: get(srv, path)[2].decode() for path in ENDPOINTS[:5]}
    finally:
        srv.shutdown()
        srv.server_close()
    for path, body in bodies.items():
        assert "private-app" not in body, path               # on every endpoint, not just the grid
    assert "grafana" in bodies["/api/status"]


def test_bundled_replay_respects_hide_containers(cfg):
    for name in cfg["dashboard"]["hide_containers"]:
        for f in REPLAY.glob("*.json"):
            assert name not in f.read_text(), (name, f.name)


def test_stream_signature_changes_when_the_ai_explanation_arrives():
    s = State(":memory:")
    e = entry()
    run_id = history.record(s.conn, started_at=1, finished_at=2, mode="live", timings={}, result=result([e]),
                            ai=history.PENDING, delivered=True, snapshot={})
    before = api.signature(s.conn)
    history.update_ai(s.conn, run_id, result([e]), {"ok": True, "model": "m", "summary": "x",
                                                    "alerts": [(e["key"], "y")]}, 61000.0)
    assert api.signature(s.conn) != before


def test_a_new_incident_is_listed_above_an_older_open_one():
    """Seen live: run 2 refreshed the old incident's last_seen, so the new one was listed second."""
    s = State(":memory:")
    old, new = entry("container_down:a"), entry("container_down:b")
    record(s, result([old]), 1000)
    record(s, result([new], suppressed=[{"key": old["key"], "reason": "cooldown"}]), 2000)
    assert [i["issue_key"] for i in api.incidents(s.conn)["incidents"]] == ["container_down:b", "container_down:a"]
