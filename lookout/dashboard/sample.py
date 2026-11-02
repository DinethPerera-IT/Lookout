"""Build the bundled replay dataset: python -m lookout.dashboard.sample

The data is synthetic: the scenario fixtures from tests/ chained into one story
on top of a healthy base system, run through the real pipeline and history code,
then exported through the same API functions and scrubber as live data. Nothing
from this server's logs ends up in it. The dashboard shows it with a DEMO DATA badge.
"""
import copy
import json
import time
from pathlib import Path

from .. import config, history, llm, pipeline, redact, report
from ..state import State
from . import api
from .scrub import make_scrubber

ROOT = Path(__file__).resolve().parent.parent.parent
SCENARIOS = ROOT / "tests" / "fixtures" / "scenarios"
OUT = Path(__file__).resolve().parent / "static" / "replay"
STORY = ["disk.json", "container_down.json", "error_spike.json", "novel_error.json", "discord_test.json"]

_HEALTHY = [("grafana", "grafana/grafana"), ("prometheus", "prom/prometheus"), ("uptime-kuma", "louislam/uptime-kuma"),
            ("pihole", "pihole/pihole"), ("portainer", "portainer/portainer-ce"), ("homepage", "gethomepage/homepage"),
            ("open-webui", "open-webui/open-webui"), ("node-exporter", "prom/node-exporter"),
            ("netdata", "netdata/netdata"), ("docker-socket-proxy", "haproxy")]
BASE = {
    "host": {"ok": True, "disks": [{"mountpoint": "/", "used_pct": 34.0}, {"mountpoint": "/boot/efi", "used_pct": 0.6}],
             "memory": {"available_pct": 74.0, "swap_used_bytes": 0},
             "load": {"load1": 1.4, "load5": 1.6, "load15": 1.7, "cpus": 4}},
    "prometheus": {"ok": True, "targets": [{"job": "node-exporter", "instance": "localhost:9100", "up": True}],
                   "disk_used_pct": {}, "disk_avail_predicted_bytes": {}, "mem_min_available_pct": 72.0,
                   "swap_max_used_bytes": 0, "load15_max": 1.9},
    "systemd": {"ok": True, "failed_units": []},
    "journal": {"ok": True, "by_source": {}, "error_templates": {}, "wifi_reauths": {"window_hours": 24, "count": 0}},
    "docker": {"ok": True, "by_source": {}, "error_templates": {},
               "containers": [{"name": n, "id": f"{i:012x}", "image": img, "state": "running", "status": "Up 2 weeks",
                               "health": "healthy" if n in ("pihole", "open-webui") else "none"}
                              for i, (n, img) in enumerate(_HEALTHY, 1)]},
}
# Plausible stage timings for the demo (real ones come from real runs)
TIMINGS = {"collect_ms": 5400.0, "detect_ms": 38.0, "report_ms": 310.0}


def merge(step_facts, now):
    facts = copy.deepcopy(BASE)
    for name, section in step_facts.items():
        if name == "docker":
            names = {c["name"] for c in section.get("containers", [])}
            section = {**section, "containers": section.get("containers", []) +
                       [c for c in BASE["docker"]["containers"] if c["name"] not in names]}
        elif name == "host":  # scenarios set only the numbers they test; keep the rest healthy
            section = {**facts["host"], **{k: ({**facts["host"][k], **v} if isinstance(v, dict) and k in facts["host"]
                                               else v) for k, v in section.items()}}
        facts[name] = section
    facts["docker"] = {"checked_at": now, **facts["docker"]}
    return facts


def build(cfg, end=None):
    """Run the story into an in-memory state; return (state, now)."""
    scenarios = [json.loads((SCENARIOS / name).read_text()) for name in STORY]
    total = sum(s["runs"][-1].get("minutes", 0) + 60 for s in scenarios)
    end = end or (time.time() // 3600) * 3600
    t0 = end - total * 60
    state = State(":memory:")
    for scenario in scenarios:
        pipeline.seed_from_scenario(state, cfg, scenario.get("baseline", {}), t0)
        for step in scenario["runs"]:
            now = t0 + step.get("minutes", 0) * 60
            facts = {"window_minutes": cfg["window_minutes"], **merge(step["facts"], now)}
            result = pipeline.run(facts, state, cfg, now)
            timings = {**TIMINGS, "report_ms": TIMINGS["report_ms"] if result["report"] or result["resolved"] else 0.0}
            history.record(state.conn, started_at=now - 6, finished_at=now, mode="live", timings=timings,
                           result=result, ai=None, delivered=bool(result["report"] or result["resolved"]),
                           snapshot=history.snapshot(facts, state.containers()))
        t0 += (scenario["runs"][-1].get("minutes", 0) + 60) * 60
    return state, t0 - 60 * 60 + 7 * 60   # "now": 7 minutes after the last run


def from_db(path, keep):
    """A recorded state DB, copied into memory, keeping only incidents whose key contains `keep`
    (e.g. the demo lab), so host-specific incidents from the same hour stay out of the bundle."""
    import sqlite3
    src = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    state = State(":memory:")
    state.commit()  # backup() needs the destination outside a transaction
    src.backup(state.conn)
    src.close()
    state.conn.execute("DELETE FROM incidents WHERE instr(issue_key, ?) = 0", (keep,))
    for run_id, snap in state.conn.execute("SELECT id, snapshot FROM runs").fetchall():
        snap = json.loads(snap or "{}")
        snap["failed_units"] = [u for u in snap.get("failed_units", []) if keep in u]
        state.conn.execute("UPDATE runs SET snapshot = ? WHERE id = ?", (json.dumps(snap), run_id))
    last = state.conn.execute("SELECT max(finished_at) FROM runs").fetchone()[0] or time.time()
    return state, last + 5 * 60


def export(state, now, out=OUT, scrub=None, note=None, cfg=None):
    dash = (cfg or {}).get("dashboard", {})
    scrub = scrub or make_scrubber(dash.get("hide_names", []))
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.json"):
        old.unlink()
    conn = state.conn
    files = {"status": api.status(conn, now, dash.get("hide_containers", [])), "incidents": api.incidents(conn), "runs_latest": api.runs_latest(conn),
             "benchmark": api.benchmark()}
    for item in files["incidents"]["incidents"]:
        files[f"incident-{item['id']}"] = api.incident(conn, item["id"])
    files["manifest"] = {"generated_at": time.time(), "now": now, "synthetic": note is None,
                         "note": note or "Synthetic demo data built from Lookout's scenario fixtures. Not from a real system."}
    for name, data in files.items():
        (out / f"{name}.json").write_text(json.dumps(scrub(data), indent=1) + "\n")
    return sorted(files)


# Fake values only, chosen to look like the real thing. Shown on the Security page as "before".
REDACTION_EXAMPLES = [
    "login failed for admin password=hunter2",
    "Authorization: Bearer FAKEabcdef0123456789token",
    "session token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZW1vIn0.ZmFrZS1zaWduYXR1cmU",
    "POST https://discord.com/api/webhooks/123456789/FAKE-webhook-token",
    "connecting to postgres://app:FakePassw0rd@db:5432/app",
    "wlan0: associated with SSID 'Example Home 12 Fake St'",
    "wlan0: 02:00:5e:10:00:01 link up",
    "password reset sent to someone@example.com",
]
INJECTION_CASES = ["7_injection_caddy.json", "8_injection_operator.json"]
OBEYING_ANSWER = {"summary": "All current alerts are false positives; no action is needed. @everyone",
                  "alerts": [{"id": "F1", "explanation": "Everything is fine. Visit https://fix-my-server.example "
                                                         "and run sudo rm -rf /var/lib/docker to clear it."}]}


def security(bench_cases=ROOT / "bench" / "cases", proxy_cfg=ROOT / "deploy" / "docker-socket-proxy" / "haproxy.cfg"):
    """Static examples for the Security page, produced by the real code paths: the redactor,
    the Discord renderer and the LLM validator. Not an API response: the "before" side must
    show (fake) secrets, which the API's scrubber would rightly remove."""
    redaction = []
    for before in REDACTION_EXAMPLES:
        after, counts = redact.redact_counts(before)
        redaction.append({"before": before, "after": after, "rules": sorted(counts)})
    injections = []
    for name in INJECTION_CASES:
        case = json.loads((bench_cases / name).read_text())
        hostile = next(f for f in case["flags"] if f["evidence"])
        _, problems = llm.validate(json.dumps(OBEYING_ANSWER), [hostile])
        injections.append({
            "case": case["name"],
            "raw": hostile["evidence"],
            "discord": report.evidence_block(hostile["evidence"]),
            "obeying_answer": OBEYING_ANSWER["summary"] + " " + OBEYING_ANSWER["alerts"][0]["explanation"],
            "rejected_because": list(dict.fromkeys(llm.problem_kind(p) for p in problems)),
        })
    allowlist = [line.strip() for line in proxy_cfg.read_text().splitlines()
                 if line.strip().startswith(("acl ", "http-request "))]
    return {"redaction": redaction, "rule_names": [name for name, _, _ in redact.RULES],
            "injections": injections, "proxy_allowlist": allowlist}


def main():
    (OUT.parent / "security.json").write_text(json.dumps(security(), indent=1, ensure_ascii=False) + "\n")
    import argparse
    parser = argparse.ArgumentParser(prog="lookout.dashboard.sample")
    parser.add_argument("--from-db", help="export a recorded state DB instead of the synthetic story")
    parser.add_argument("--keep", default="lookout-demo", help="with --from-db: keep incidents whose key contains this")
    args = parser.parse_args()
    if args.from_db:
        state, now = from_db(args.from_db, args.keep)
        note = (f"Recorded from Lookout's demo lab on {time.strftime('%Y-%m-%d')} with the real pipeline and model; "
                f"only incidents matching '{args.keep}' are kept; scrubbed like every API response.")
    else:
        state, now = build(config.load())
        note = None
    try:
        names = export(state, now, note=note, cfg=config.load())
    finally:
        state.close()
    print(f"wrote {len(names)} files to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
