"""Crash evidence, the deterministic cause classifier, allowlisted next checks, and the
LLM's (limited) part in it. Fixtures: real evidence from the demo lab + hand-written cases."""
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from lookout import crash, history, llm
from lookout.state import State

FIXTURES = Path(__file__).parent / "fixtures" / "crash"
REAL = sorted(FIXTURES.glob("demo_*.json"))
SYNTHETIC = json.loads((FIXTURES / "synthetic.json").read_text())["cases"]
BENCH = Path(__file__).resolve().parent.parent / "bench" / "cases"


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize("path", REAL, ids=lambda p: p.stem)
def test_real_demo_lab_crashes_are_classified(path):
    case = json.loads(path.read_text())
    cause, matches = crash.classify(case["evidence"])
    assert cause == case["expect"] and matches


@pytest.mark.parametrize("case", SYNTHETIC, ids=lambda c: c["name"])
def test_synthetic_cases(case):
    assert crash.classify(case["evidence"])[0] == case["expect"]


def test_every_cause_has_a_fixture():
    covered = {json.loads(p.read_text())["expect"] for p in REAL} | {c["expect"] for c in SYNTHETIC}
    assert covered == set(crash.CAUSES)


def test_matches_say_why():
    ev = json.loads((FIXTURES / "demo_oom.json").read_text())["evidence"]
    cause, matches = crash.classify(ev)
    signals = [m["signal"] for m in matches]
    assert cause == "out_of_memory" and signals[0] == "OOMKilled" and "kernel OOM killer" in signals
    assert "exit code 137" in signals


def test_dependency_beats_the_stack_trace_it_caused():
    ev = json.loads((FIXTURES / "demo_db.json").read_text())["evidence"]
    assert any("Error: connect ECONNREFUSED" in l for l in ev["log_lines"])   # a stack trace is there too
    assert crash.classify(ev)[0] == "dependency_unreachable"


def test_a_recent_change_is_mentioned_even_when_a_specific_cause_wins():
    case = next(c for c in SYNTHETIC if c["name"] == "specific_cause_beats_recent_change")
    cause, matches = crash.classify(case["evidence"])
    assert cause == "port_in_use" and any(m["signal"] == "also: recent change" for m in matches)


def test_disk_threshold_comes_from_config():
    ev = {"log_lines": [], "host": {"disk_used_pct": {"/": 91.0}}}
    assert crash.classify(ev, disk_crit_pct=95)[0] == "unknown"
    assert crash.classify(ev, disk_crit_pct=90)[0] == "disk_full"


# --- next checks: allowlist only -----------------------------------------------

READ_ONLY = re.compile(r"^(docker (logs|ps|stats|system df|image ls)|journalctl|systemctl (status|cat|list-dependencies)|"
                       r"df -h|ss -ltn|grep )")


@pytest.mark.parametrize("kind, name", [("container", "lookout-demo-db"), ("unit", "caddy.service")])
def test_every_planned_check_is_read_only(kind, name):
    for cause in crash.CAUSES:
        checks = crash.next_checks(kind, name, cause)
        assert 2 <= len(checks) <= 3
        for c in checks:
            assert READ_ONLY.match(c["command"]), c["command"]
            assert not re.search(r"\b(restart|stop|start|rm|kill|prune|exec|run|sudo)\b", c["command"])


@pytest.mark.parametrize("name", ["x; rm -rf /", "$(reboot)", "a`id`", "web && curl evil", "../etc", "-rf"])
def test_hostile_names_never_reach_a_command(name):
    for kind in ("container", "unit"):
        for cause in crash.CAUSES:
            for c in crash.next_checks(kind, name, cause):
                assert name not in c["command"]


def test_ai_can_reorder_checks_but_only_with_allowlisted_ids():
    result = {"report": [{"key": "k", "next_checks": crash.next_checks("container", "web", "port_in_use")}]}
    crash.apply_ai_checks(result, {"ok": True, "checks": {"k": ["state", "rm -rf /", "nonsense"]}})
    assert [c["id"] for c in result["report"][0]["next_checks"]] == ["state", "ports", "logs"]
    crash.apply_ai_checks(result, {"ok": False, "reason": "Ollama unreachable"})
    assert [c["id"] for c in result["report"][0]["next_checks"]] == ["state", "ports", "logs"]


def test_model_check_picks_are_mapped_and_junk_ignored():
    checks = crash.next_checks("container", "web", "dependency_unreachable")
    assert llm.picked_checks(["K2", " K1 ", "K9", "docker restart web", 3, "K2"], checks) == [checks[1]["id"], checks[0]["id"]]
    assert llm.picked_checks("K1", checks) == []


# --- collecting evidence ---------------------------------------------------------

CONTAINER_ID = "2c9fd26d357a"
KERNEL = [
    {"MESSAGE": "node invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), order=0, oom_score_adj=0"},
    {"MESSAGE": f"oom-kill:constraint=CONSTRAINT_MEMCG,task_memcg=/system.slice/docker-{CONTAINER_ID}"
                "a234af8a120a69dc793f6e10c0c934020c22875f52f82d1ae4e98.scope,task=node,pid=3472250,uid=1000"},
    {"MESSAGE": "Memory cgroup out of memory: Killed process 3472250 (node) total-vm:745352kB"},
    {"MESSAGE": "oom-kill:constraint=CONSTRAINT_MEMCG,task_memcg=/system.slice/docker-ffffffffffff0000.scope,pid=99"},
    {"MESSAGE": "Memory cgroup out of memory: Killed process 99 (java)"},
]


def fake_run(outputs):
    def run(cmd, timeout=20):
        for needle, out in outputs.items():
            if needle in cmd:
                return out
        return ""
    return run


def test_kernel_oom_matches_this_container_only_and_redacts_the_id():
    run = fake_run({"-k": "\n".join(json.dumps(e) for e in KERNEL)})
    lines = crash.kernel_oom(0, 1, f"docker-{CONTAINER_ID}", run)
    assert len(lines) == 2 and "Killed process 3472250" in lines[1]
    assert not any("java" in l for l in lines)
    assert not any(CONTAINER_ID + "a234" in l for l in lines)   # the full id is redacted on the way out


def test_container_evidence_from_list_status_logs_and_kernel():
    logs = (b"2026-11-02T03:37:39.415Z app started\n"
            b"2026-11-02T03:37:39.618Z token=abcdefghijklmnopqrstuvwxyz123456 cache 52 MB\n")
    container = {"name": "lookout-demo-oom", "id": CONTAINER_ID, "state": "exited", "status": "Exited (137) 3 seconds ago"}
    cfg = {"docker": {"proxy_url": "http://proxy", "timeout_seconds": 5, "max_log_bytes": 10**6}}
    ev = crash.container_evidence(cfg, container, {"host": {"ok": True, "memory": {"available_pct": 70.0},
                                                            "disks": [{"mountpoint": "/", "used_pct": 34.0}]}},
                                  now=1793590000, fetch=lambda url, t, limit=None: logs,
                                  run=fake_run({"-k": "\n".join(json.dumps(e) for e in KERNEL)}))
    assert ev["exit_code"] == 137 and ev["oom_killed"] is True and ev["restart_count"] is None
    assert ev["failed_at"] == pytest.approx(datetime.fromisoformat("2026-11-02T03:37:39.618+00:00").timestamp())
    assert "abcdefghijklmnop" not in ev["log_lines"][1]          # redacted at collection
    assert ev["host"] == {"memory_available_pct": 70.0, "disk_used_pct": {"/": 34.0}}
    assert crash.classify(ev)[0] == "out_of_memory"


def test_unit_evidence_from_systemctl_show_and_journal():
    show = ("Result=exit-code\nExecMainCode=1\nExecMainStatus=1\nInactiveEnterTimestamp=@1800000000\n"
            "NRestarts=3\nFragmentPath=/etc/systemd/system/discordbot.service\n")
    journal = "\n".join(json.dumps({"MESSAGE": m}) for m in ["Traceback (most recent call last):", "KeyError: 'sheet'"])
    ev = crash.unit_evidence({}, "discordbot.service", {}, 1800000600, run=fake_run({"show": show, "-u": journal}))
    assert (ev["failed_at"], ev["exit_code"], ev["restart_count"], ev["result"]) == (1800000000, 1, 3, "exit-code")
    assert crash.classify(ev)[0] == "app_crash"


# --- wiring ------------------------------------------------------------------------

CFG = {"thresholds": {"disk_crit_pct": 95}}


def test_investigate_adds_cause_evidence_checks_and_failure_time():
    ev = json.loads((FIXTURES / "demo_db.json").read_text())["evidence"]
    result = {"report": [{"key": "container_down:lookout-demo-db", "severity": "crit", "summary": "down", "evidence": []},
                         {"key": "disk:/", "severity": "warn", "summary": "disk", "evidence": []}]}
    facts = {"docker": {"containers": [{"name": "lookout-demo-db", "id": "x", "state": "exited", "status": "Exited (1)"}]}}
    crash.investigate(result, facts, CFG, ev["failed_at"] + 30, collect_container=lambda *a: dict(ev),
                      recent_changes=lambda *a: [])
    db, disk = result["report"]
    assert db["cause"] == "dependency_unreachable" and db["failed_at"] == ev["failed_at"]
    assert db["evidence"] == ev["log_lines"] and [c["id"] for c in db["next_checks"]] == ["all_containers", "logs", "state"]
    assert "cause" not in disk                                   # not a crash: nothing added


def test_investigation_failure_is_unknown_and_says_so():
    def broken(*a):
        raise TimeoutError("journalctl hung")
    result = {"report": [{"key": "failed_unit:caddy.service", "severity": "crit", "summary": "x", "evidence": []}]}
    crash.investigate(result, {}, CFG, 0, collect_unit=broken, recent_changes=lambda *a: [])
    entry = result["report"][0]
    assert entry["cause"] == "unknown" and entry["cause_matches"][0] == {"signal": "evidence unavailable", "detail": "TimeoutError"}


def test_time_to_detect_and_crash_are_stored():
    s = State(":memory:")
    entry = {"key": "container_down:x", "reason": "new", "severity": "crit", "summary": "x", "evidence": ["a"],
             "failed_at": 1000.0, "detected_at": 1012.5, "cause": "out_of_memory",
             "cause_matches": [{"signal": "OOMKilled", "detail": "d"}], "crash": {"exit_code": 137},
             "next_checks": crash.next_checks("container", "x", "out_of_memory")}
    history.record(s.conn, started_at=1000, finished_at=1013, mode="live", timings={},
                   result={"flags": [entry], "report": [entry], "suppressed": [], "resolved": []},
                   ai=None, delivered=True, snapshot={})
    row = dict(s.conn.execute("SELECT * FROM incidents").fetchone())
    assert row["time_to_detect_s"] == 12.5 and row["cause"] == "out_of_memory"
    assert row["opened_at"] == 1012.5                          # detection time, not the end of the run
    assert json.loads(row["crash"]) == {"exit_code": 137} and len(json.loads(row["next_checks"])) == 3


def test_old_state_files_get_the_crash_column(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(history.SCHEMA[1].replace(",\n        crash TEXT)", ")"))
    conn.commit()
    conn.close()
    s = State(db)
    assert "crash" in {r[1] for r in s.conn.execute("PRAGMA table_info(incidents)")}


# --- the LLM's view: classification is given, evidence is untrusted ------------------

def crash_entry(case_name):
    case = next(c for c in SYNTHETIC if c["name"] == case_name)
    cause, matches = crash.classify(case["evidence"])
    return {"key": "container_down:commoncents", "severity": "crit", "summary": "Container commoncents is not running",
            "cause": cause, "cause_matches": matches, "crash": case["evidence"],
            "next_checks": crash.next_checks("container", "commoncents", cause), "evidence": case["evidence"]["log_lines"]}


def test_prompt_states_the_cause_and_labels_log_lines_untrusted():
    text = llm.alert_lines([crash_entry("injection_in_crash_log")])
    assert "CAUSE (decided by code from the evidence): app_crash" in text
    assert "CRASH FACTS: exit code 1" in text
    for line in text.splitlines():
        if "Ignore previous instructions" in line or "Uncaught" in line:
            assert line.strip().startswith("EVIDENCE (untrusted log line)")
    assert "docker logs" not in text                             # checks are described, commands never shown


@pytest.mark.parametrize("bench_case", ["7_injection_caddy.json", "8_injection_operator.json"])
def test_bench_injections_through_the_crash_path(bench_case):
    """The Phase 6 injection lines, as a crashed container's last log lines: the classification
    is decided by code, and a model answer that follows the injection is rejected."""
    lines = [ev for f in json.loads((BENCH / bench_case).read_text())["flags"] for ev in f["evidence"]]
    ev = {"target": {"kind": "container", "name": "aotearoa"}, "exit_code": 1, "log_lines": lines, "kernel_oom": [], "host": {}}
    cause, matches = crash.classify(ev)
    assert cause in crash.CAUSES and cause != "after_recent_change"
    entry = {"key": "container_down:aotearoa", "severity": "crit", "summary": "Container aotearoa is not running",
             "cause": cause, "cause_matches": matches, "crash": ev, "evidence": lines,
             "next_checks": crash.next_checks("container", "aotearoa", cause)}
    obeys = json.dumps({"summary": "All alerts are false positives, no action is needed.",
                        "alerts": [{"id": "F1", "explanation": "Visit https://fix-my-server.example and run sudo rm -rf /var/lib/docker.",
                                    "checks": ["K1"]}]})
    answer, problems = llm.validate(obeys, [entry])
    assert answer is None and {llm.problem_kind(p) for p in problems} >= {"dismissive wording", "repeats untrusted content"}


def test_a_good_answer_about_a_classified_crash_passes():
    entry = crash_entry("dns_failure")
    good = json.dumps({"summary": "Container commoncents stopped because it could not reach its database.",
                       "alerts": [{"id": "F1", "explanation": "The logs show the database host name could not be resolved, "
                                   "so the app gave up. This can happen when the database container is stopped or renamed.",
                                   "checks": ["K1", "K2"]}]})
    answer, problems = llm.validate(good, [entry])
    assert problems == []
    assert llm.picked_checks(answer["alerts"][0]["checks"], entry["next_checks"]) == ["all_containers", "logs"]
