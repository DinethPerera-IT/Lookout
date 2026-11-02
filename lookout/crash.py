"""Crash evidence and deterministic cause classification.

When a container stops or restarts, or a systemd unit fails, code collects what
happened (exit code, OOM kill, the last log lines, kernel OOM messages, host
memory/disk, recent changes) and a fixed rule list classifies the cause. Each
classification keeps the evidence that matched, so the dashboard can show *why*.

The LLM later gets the classification as a given: it may explain it, never change it.
Suggested next checks come from a fixed allowlist of read-only command templates;
the model can at most pick among them by id.
"""
import json
import re
import subprocess

from . import changes
from .collectors import docker
from .redact import redact

LOG_LINES = 30          # last log lines before the stop
LOOKBACK = 6 * 3600     # how far back to look for those lines and for kernel OOM messages
DETAIL_WIDTH = 200

CAUSES = ("out_of_memory", "disk_full", "port_in_use", "dependency_unreachable",
          "after_recent_change", "app_crash", "unknown")

# Log patterns per cause. Checked in CAUSES order: the first cause with a match wins,
# so a stack trace *caused by* a refused connection is dependency_unreachable, not app_crash.
LOG_PATTERNS = {
    "out_of_memory": [r"(?i)out of memory", r"OutOfMemory", r"(?i)cannot allocate memory", r"(?i)heap out of memory",
                      r"\bMemoryError\b", r"\bENOMEM\b", r"(?i)\boom[-_ ]?kill"],
    "disk_full": [r"(?i)no space left on device", r"\bENOSPC\b", r"(?i)\bdisk (?:is )?full\b",
                  r"(?i)database or disk is full", r"(?i)disk quota exceeded"],
    "port_in_use": [r"\bEADDRINUSE\b", r"(?i)address already in use", r"(?i)\baddress in use\b",
                    r"(?i)port is already allocated", r"(?i)bind\(\) .*failed"],
    "dependency_unreachable": [r"\bECONNREFUSED\b", r"(?i)connection refused", r"\bETIMEDOUT\b", r"(?i)connection timed out",
                               r"\bENOTFOUND\b", r"\bEHOSTUNREACH\b", r"(?i)no route to host",
                               r"(?i)name or service not known", r"(?i)temporary failure in name resolution",
                               r"(?i)no such host", r"(?i)could not connect to", r"(?i)could not translate host name",
                               r"(?i)\bunreachable\b"],
    "app_crash": [r"Traceback \(most recent call last\)", r"(?:^|\s)panic: ", r"(?i)\buncaught\b",
                  r"(?i)segmentation fault", r"(?i)core dumped", r"^\s*at \S.*\(\S+:\d+:\d+\)\s*$",
                  r"Exception in thread", r"(?i)\bfatal error\b", r"^\s*(?:\w+\.)*\w*(?:Error|Exception): "],
}
_COMPILED = {cause: [re.compile(p) for p in pats] for cause, pats in LOG_PATTERNS.items()}
RECENT_CHANGE_WINDOW = 24 * 3600   # a change this long before the failure counts as "recent"

_EXIT = re.compile(r"^(?:Exited|Restarting) \((-?\d+)\)")
_SAFE_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,200}\.(?:service|socket|timer|mount|path|target)$")


# --- classification (pure) --------------------------------------------------

def _clip(text):
    return text.replace("\n", " ⏎ ")[:DETAIL_WIDTH]


def _log_matches(lines):
    """{cause: [line, ...]} for every cause whose patterns match some line."""
    found = {}
    for line in lines:
        for cause, patterns in _COMPILED.items():
            if any(p.search(line) for p in patterns):
                found.setdefault(cause, []).append(line)
    return found


def classify(ev, disk_crit_pct=95):
    """Return (cause, matches). `ev` is the evidence dict built by the collectors below
    (or by a test fixture). matches: [{"signal", "detail"}], most telling first."""
    logs = _log_matches(ev.get("log_lines", []))
    hits = {}

    oom = []
    if ev.get("oom_killed"):
        oom.append({"signal": "OOMKilled", "detail": "the container was killed for exceeding its memory limit"})
    if ev.get("result") == "oom-kill":
        oom.append({"signal": "systemd Result=oom-kill", "detail": "systemd reports the unit was OOM-killed"})
    oom += [{"signal": "kernel OOM killer", "detail": _clip(l)} for l in ev.get("kernel_oom", [])[:2]]
    oom += [{"signal": "log line", "detail": _clip(l)} for l in logs.get("out_of_memory", [])[:2]]
    if oom and ev.get("exit_code") == 137:
        oom.append({"signal": "exit code 137", "detail": "128 + 9: killed with SIGKILL, which is what the OOM killer sends"})
    hits["out_of_memory"] = oom

    disk = [{"signal": "log line", "detail": _clip(l)} for l in logs.get("disk_full", [])[:2]]
    for mount, pct in (ev.get("host", {}).get("disk_used_pct") or {}).items():
        if pct is not None and pct >= disk_crit_pct:
            disk.append({"signal": "disk usage", "detail": f"{mount} was {pct:.0f}% full"})
    hits["disk_full"] = disk

    for cause in ("port_in_use", "dependency_unreachable"):
        hits[cause] = [{"signal": "log line", "detail": _clip(l)} for l in logs.get(cause, [])[:3]]

    hits["after_recent_change"] = [
        {"signal": "recent change", "detail": c["summary"]} for c in ev.get("recent_changes", [])
        if ev.get("failed_at") is None or 0 <= ev["failed_at"] - c["at"] <= RECENT_CHANGE_WINDOW][:3]

    crash = [{"signal": "log line", "detail": _clip(l)} for l in logs.get("app_crash", [])[:3]]
    if ev.get("result") in ("core-dump", "signal") and ev.get("exit_code") is not None:
        crash.append({"signal": f"systemd Result={ev['result']}", "detail": f"terminated by signal {ev['exit_code']}"})
    hits["app_crash"] = crash

    for cause in CAUSES[:-1]:
        if hits[cause]:
            matches = list(hits[cause])
            if cause != "after_recent_change" and hits["after_recent_change"]:
                matches += [{**m, "signal": "also: " + m["signal"]} for m in hits["after_recent_change"][:1]]
            return cause, _with_exit(matches, ev, cause)
    return "unknown", _with_exit([{"signal": "no rule matched",
                                   "detail": "no OOM, disk, port, dependency, change or crash signature in the evidence"}],
                                 ev, "unknown")


def _with_exit(matches, ev, cause):
    if ev.get("exit_code") is not None and not (cause == "out_of_memory" and ev["exit_code"] == 137):
        matches = matches + [{"signal": f"exit code {ev['exit_code']}", "detail": "from the container/unit state"}]
    return matches


# --- next checks: an allowlist of read-only templates ----------------------------

CONTAINER_CHECKS = {
    "logs": ("docker logs --tail 50 {name}", "the last log lines, including any stack trace"),
    "state": ("docker ps -a --filter name=^{name}$", "current state and exit status"),
    "stats": ("docker stats --no-stream {name}", "memory use against the limit (once it runs again)"),
    "kernel_oom": ('journalctl -k --since -2h --grep "out of memory"', "the kernel's OOM-killer messages"),
    "df": ("df -h", "free space per filesystem"),
    "docker_df": ("docker system df", "space used by images, containers and volumes"),
    "ports": ("ss -ltn", "which TCP ports are already being listened on"),
    "all_containers": ("docker ps -a --format '{{{{.Names}}}}\\t{{{{.Status}}}}'", "whether the dependency's container is up"),
    "image": ("docker image ls --format '{{{{.Repository}}}}:{{{{.Tag}}}}\\t{{{{.CreatedSince}}}}'", "when images were built or pulled"),
}
UNIT_CHECKS = {
    "status": ("systemctl status {name}", "state, exit status and the last log lines"),
    "logs": ("journalctl -u {name} --since -2h --no-pager", "the unit's log around the failure"),
    "kernel_oom": ('journalctl -k --since -2h --grep "out of memory"', "the kernel's OOM-killer messages"),
    "df": ("df -h", "free space per filesystem"),
    "ports": ("ss -ltn", "which TCP ports are already being listened on"),
    "deps": ("systemctl list-dependencies {name}", "what the unit depends on"),
    "apt": ("grep -A3 Start-Date /var/log/apt/history.log", "recent package installs and upgrades"),
    "cat": ("systemctl cat {name}", "the unit file and drop-ins that are in effect"),
}
PLAN = {  # per cause and target kind: which checks, most useful first
    ("container", "out_of_memory"): ["kernel_oom", "stats", "logs"],
    ("container", "disk_full"): ["df", "docker_df", "logs"],
    ("container", "port_in_use"): ["ports", "logs", "state"],
    ("container", "dependency_unreachable"): ["all_containers", "logs", "state"],
    ("container", "after_recent_change"): ["image", "logs", "state"],
    ("container", "app_crash"): ["logs", "state"],
    ("container", "unknown"): ["logs", "state"],
    ("unit", "out_of_memory"): ["kernel_oom", "status", "logs"],
    ("unit", "disk_full"): ["df", "status", "logs"],
    ("unit", "port_in_use"): ["ports", "status", "logs"],
    ("unit", "dependency_unreachable"): ["deps", "status", "logs"],
    ("unit", "after_recent_change"): ["apt", "cat", "logs"],
    ("unit", "app_crash"): ["logs", "status"],
    ("unit", "unknown"): ["status", "logs"],
}


def next_checks(kind, name, cause):
    """Rendered read-only commands. A name that isn't a plain container/unit name gets no
    name-specific command at all, so nothing from a log or API can inject shell syntax."""
    safe = (_SAFE_CONTAINER if kind == "container" else _SAFE_UNIT).match(name or "")
    table = CONTAINER_CHECKS if kind == "container" else UNIT_CHECKS
    checks = []
    for key in PLAN[(kind, cause)]:
        template, why = table[key]
        if "{name}" in template and not safe:
            continue
        checks.append({"id": key, "command": template.format(name=name), "why": why})
    return checks


# --- collecting evidence (read-only I/O) --------------------------------------

def _run(cmd, timeout=20):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def kernel_oom(since, until, needle, run=_run):
    """Kernel OOM-killer lines that name `needle` (a container id or unit), redacted.

    Matching happens on the raw text because redaction would turn the container id
    into <token>; only redacted lines leave this function."""
    raw = run(["journalctl", "-k", "-o", "json", "--no-pager", "-q", "--output-fields=MESSAGE",
               f"--since=@{int(since)}", f"--until=@{int(until)}"])
    messages = []
    for line in raw.splitlines():
        try:
            msg = json.loads(line).get("MESSAGE", "")
        except json.JSONDecodeError:
            continue
        if isinstance(msg, str):
            messages.append(msg)
    hits, pids = [], set()
    for msg in messages:
        if "oom" in msg.lower() and needle in msg:
            hits.append(msg)
            pids.update(re.findall(r"\bpid=(\d+)", msg))
    for msg in messages:  # "Killed process 1234 (node)" doesn't name the cgroup, but its pid does
        m = re.search(r"Killed process (\d+)", msg)
        if m and m.group(1) in pids:
            hits.append(msg)
    return [redact(m) for m in hits]


def host_at(facts):
    host = facts.get("host") or {}
    return {"memory_available_pct": (host.get("memory") or {}).get("available_pct"),
            "disk_used_pct": {d["mountpoint"]: d["used_pct"] for d in host.get("disks", []) if "used_pct" in d}}


def container_evidence(cfg, container, facts, now, fetch=docker.http_get, run=_run):
    m = _EXIT.match(container.get("status", ""))
    lines = list(docker.fetch_logs(cfg, container, fetch, since=now - LOOKBACK))[-LOG_LINES:]
    failed_at = lines[-1][0] if lines and container["state"] != "running" else None
    oom_lines = kernel_oom((failed_at or now) - LOOKBACK, now + 60, f"docker-{container['id']}", run)
    return {
        "target": {"kind": "container", "name": container["name"], "image": container.get("image")},
        "state": container["state"], "status": container.get("status"),
        "exit_code": int(m.group(1)) if m else None,
        "oom_killed": bool(oom_lines) or None,   # inspect isn't allowed by the proxy: the kernel line is the proof
        "restart_count": None,
        "failed_at": failed_at,
        "log_lines": [redact(msg) for _, msg in lines],
        "kernel_oom": oom_lines,
        "host": host_at(facts),
    }


def _unix(value):
    try:
        return float(value.strip().lstrip("@")) if value and value.strip() not in ("", "0", "n/a") else None
    except ValueError:
        return None


def unit_evidence(cfg, unit, facts, now, run=_run):
    props = dict(line.split("=", 1) for line in run(
        ["systemctl", "show", unit, "--timestamp=unix", "-p", "Result", "-p", "ExecMainCode", "-p", "ExecMainStatus",
         "-p", "InactiveEnterTimestamp", "-p", "NRestarts", "-p", "FragmentPath"]).splitlines() if "=" in line)
    failed_at = _unix(props.get("InactiveEnterTimestamp"))
    until = int(failed_at + 5) if failed_at else int(now)
    raw = run(["journalctl", "-u", unit, "-o", "json", "--no-pager", "-q", "--output-fields=MESSAGE",
               "-n", str(LOG_LINES), f"--until=@{until}"])
    lines = []
    for line in raw.splitlines():
        try:
            msg = json.loads(line).get("MESSAGE", "")
        except json.JSONDecodeError:
            continue
        if isinstance(msg, list):
            msg = bytes(msg).decode("utf-8", errors="replace")
        lines.append(redact(msg))
    status = props.get("ExecMainStatus")
    return {
        "target": {"kind": "unit", "name": unit, "fragment_path": props.get("FragmentPath") or None},
        "result": props.get("Result"),
        "exit_code": int(status) if status and status.lstrip("-").isdigit() and status != "0" else None,
        "oom_killed": props.get("Result") == "oom-kill" or None,
        "restart_count": int(props["NRestarts"]) if props.get("NRestarts", "").isdigit() else None,
        "failed_at": failed_at,
        "log_lines": lines[-LOG_LINES:],
        "kernel_oom": kernel_oom((failed_at or now) - LOOKBACK, now + 60, f"/{unit}", run),
        "host": host_at(facts),
    }


# --- wiring into a run ----------------------------------------------------------

def target_of(entry, facts):
    """(kind, name, container_dict_or_None) for flags with a crash to investigate."""
    key = entry["key"]
    for prefix in ("container_down:", "container_restarting:", "container_restart:"):
        if key.startswith(prefix):
            name = key[len(prefix):]
            found = next((c for c in (facts.get("docker") or {}).get("containers", []) if c["name"] == name), None)
            return ("container", name, found) if found else None
    if key.startswith("failed_unit:"):
        return ("unit", key[len("failed_unit:"):], None)
    return None


def apply_hints(ev, hint):
    """An event saw the failure happen: its exact time, exit code and OOM flag beat our estimates."""
    if not hint:
        return ev
    if isinstance(hint.get("at"), (int, float)):
        ev["failed_at"] = float(hint["at"])
    if isinstance(hint.get("exit_code"), int):
        ev["exit_code"] = hint["exit_code"]
    if hint.get("oom") is True:
        ev["oom_killed"] = True
    return ev


def investigate(result, facts, cfg, now, collect_container=None, collect_unit=None, recent_changes=None, hints=None):
    """Add crash evidence, a cause and next checks to each reported crash. Never raises:
    a failure to collect evidence becomes a cause of "unknown" that says so."""
    hints = (hints or {}).get("containers", {}) if isinstance(hints, dict) else {}
    collect_container = collect_container or container_evidence
    collect_unit = collect_unit or unit_evidence
    recent_changes = recent_changes or changes.recent
    crit = cfg["thresholds"]["disk_crit_pct"]
    for entry in result["report"]:
        target = target_of(entry, facts)
        if target is None:
            continue
        kind, name, container = target
        try:
            ev = (apply_hints(collect_container(cfg, container, facts, now), hints.get(name)) if kind == "container"
                  else collect_unit(cfg, name, facts, now))
            ev["recent_changes"] = recent_changes(cfg, ev, now)
            cause, matches = classify(ev, crit)
        except Exception as e:
            ev = {"target": {"kind": kind, "name": name}, "log_lines": []}
            cause, matches = "unknown", [{"signal": "evidence unavailable", "detail": f"{type(e).__name__}"}]
        entry["cause"], entry["cause_matches"] = cause, matches
        entry["failed_at"] = ev.get("failed_at")
        entry["crash"] = {k: ev.get(k) for k in ("target", "exit_code", "oom_killed", "restart_count", "result",
                                                 "kernel_oom", "host", "recent_changes")}
        if ev.get("log_lines"):
            entry["evidence"] = ev["log_lines"]
        entry["next_checks"] = next_checks(kind, name, cause)
    return result


def apply_ai_checks(result, ai, limit=3):
    """Order each entry's checks by the model's picks. Only ids from the allowlist count;
    anything else the model says is ignored, and without picks the code's order stands."""
    picks = ai.get("checks", {}) if ai and ai.get("ok") else {}
    for entry in result["report"]:
        checks = entry.get("next_checks")
        if not checks:
            continue
        by_id = {c["id"]: c for c in checks}
        chosen = [i for i in dict.fromkeys(picks.get(entry["key"], [])) if i in by_id]
        entry["next_checks"] = ([by_id[i] for i in chosen] + [c for c in checks if c["id"] not in chosen])[:limit]
    return result
