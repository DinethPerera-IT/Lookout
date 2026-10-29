"""Deterministic rules: the only part of ops-summary that decides what is unusual.

Each rule takes the collected facts (plus the error baseline) and returns Flags.
A Flag's summary is written by code from numbers and system metadata (unit
names, mountpoints). Log text never goes in the summary, only in `evidence`,
so later stages can always quote it as untrusted data.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from statistics import median

from .collectors.docker import uptime_bounds
from .redact import redact
from .state import RANK
from .templates import fingerprint

GIB = 1024 ** 3
LOG_SECTIONS = ("journal", "docker")
RESTART_SLACK = 120  # seconds of tolerance for clock jitter between runs


@dataclass
class Flag:
    key: str            # stable identity for dedup/cooldown, e.g. "disk:/"
    rule: str
    severity: str       # "warn" | "crit"
    summary: str        # code-generated, trusted
    kind: str = "state"  # "state" resolves when it clears; "event" just expires
    evidence: list = field(default_factory=list)  # redacted log lines: untrusted

    def to_dict(self):
        return asdict(self)


@dataclass
class Baseline:
    hours: int                 # hours of error history available
    error_counts: dict         # source -> hourly counts, zero-filled, len == hours
    known_templates: set       # {(source, template)}
    containers: dict = field(default_factory=dict)  # name -> snapshot from the last run
    previous_run_at: float = None


def ok(facts, section):
    data = facts.get(section)
    return data if data and data.get("ok") else None


def log_sections(facts):
    return [s for s in (ok(facts, name) for name in LOG_SECTIONS) if s]


def level(value, warn, crit, below=False):
    """Severity for a value against thresholds (higher is worse unless below=True)."""
    if value is None:
        return None
    if below:
        return "crit" if value < crit else "warn" if value < warn else None
    return "crit" if value >= crit else "warn" if value >= warn else None


# --- rules --------------------------------------------------------------

def blind_spots(facts, baseline, cfg):
    flags = []
    for name, data in facts.items():
        if isinstance(data, dict) and data.get("ok") is False:
            flags.append(Flag(f"blindspot:{name}", "blind_spot", "warn",
                              f"Collector '{name}' failed, so its checks did not run: "
                              f"{redact(data.get('error', 'unknown error'))[:200]}"))
    prom = ok(facts, "prometheus")
    for target in (prom or {}).get("targets", []):
        if not target["up"]:
            flags.append(Flag(f"target_down:{target['job']}:{target['instance']}", "blind_spot", "warn",
                              f"Prometheus target {target['job']} ({target['instance']}) is down"))
    return flags


def disk(facts, baseline, cfg):
    t = cfg["thresholds"]
    used = {}
    for d in (ok(facts, "host") or {}).get("disks", []):
        if "used_pct" in d:
            used[d["mountpoint"]] = d["used_pct"]
    prom = ok(facts, "prometheus") or {}
    used.update({m: v for m, v in prom.get("disk_used_pct", {}).items() if v is not None})

    flags = []
    for mount, pct in sorted(used.items()):
        sev = level(pct, t["disk_warn_pct"], t["disk_crit_pct"])
        if sev:
            flags.append(Flag(f"disk:{mount}", "disk", sev,
                              f"Disk {mount} is {pct:.1f}% full "
                              f"(warn at {t['disk_warn_pct']}%, crit at {t['disk_crit_pct']}%)"))
    for mount, predicted in sorted(prom.get("disk_avail_predicted_bytes", {}).items()):
        if predicted is not None and predicted < 0:
            flags.append(Flag(f"disk_fill:{mount}", "disk", "warn",
                              f"Disk {mount} is on track to fill within {t['disk_full_within_days']} days "
                              "(linear trend over the last 6 h)"))
    return flags


def memory(facts, baseline, cfg):
    t = cfg["thresholds"]
    prom, host = ok(facts, "prometheus") or {}, (ok(facts, "host") or {}).get("memory", {})
    window = facts.get("window_minutes", cfg["window_minutes"])
    flags = []

    avail, where = prom.get("mem_min_available_pct"), f"lowest in the last {window} min"
    if avail is None:
        avail, where = host.get("available_pct"), "now"
    sev = level(avail, t["mem_avail_warn_pct"], t["mem_avail_crit_pct"], below=True)
    if sev:
        flags.append(Flag("memory", "memory", sev,
                          f"Available memory {avail:.1f}% ({where}; warn below "
                          f"{t['mem_avail_warn_pct']}%, crit below {t['mem_avail_crit_pct']}%)"))

    swap = prom.get("swap_max_used_bytes")
    if swap is None:
        swap = host.get("swap_used_bytes")
    if swap is not None and swap > t["swap_warn_bytes"]:
        flags.append(Flag("swap", "memory", "warn",
                          f"Swap in use: {swap / GIB:.2f} GiB (warn above {t['swap_warn_bytes'] / GIB:.2f} GiB)"))
    return flags


def load(facts, baseline, cfg):
    t = cfg["thresholds"]
    value, where = (ok(facts, "prometheus") or {}).get("load15_max"), "highest in the window"
    if value is None:
        value, where = (ok(facts, "host") or {}).get("load", {}).get("load15"), "now"
    sev = level(value, t["load15_warn"], t["load15_crit"])
    if not sev:
        return []
    return [Flag("load", "load", sev,
                 f"15-min load average {value:.2f} ({where}; warn at {t['load15_warn']}, "
                 f"crit at {t['load15_crit']})")]


def failed_units(facts, baseline, cfg):
    return [Flag(f"failed_unit:{u['unit']}", "failed_unit", "crit",
                 f"systemd unit {u['unit']} has failed ({u['active']}/{u['sub']})")
            for u in (ok(facts, "systemd") or {}).get("failed_units", [])]


def wifi(facts, baseline, cfg):
    t = cfg["thresholds"]
    reauths = (ok(facts, "journal") or {}).get("wifi_reauths")
    if not reauths:
        return []
    sev = level(reauths["count"], t["wifi_reauths_warn"], t["wifi_reauths_crit"])
    if not sev:
        return []
    return [Flag("wifi", "wifi", sev,
                 f"WiFi re-authenticated {reauths['count']} times in {reauths['window_hours']} h "
                 f"(warn at {t['wifi_reauths_warn']}; normal is 0-2 per day)")]


def spike_threshold(history, cfg):
    """max(floor, median + k * MAD). MAD (median absolute deviation) shrugs off rare bursts."""
    b = cfg["baseline"]
    med = median(history) if history else 0
    mad = median(abs(c - med) for c in history) if history else 0
    return med, max(b["spike_floor"], med + b["spike_mad_k"] * mad)


def error_spikes(facts, baseline, cfg):
    b = cfg["baseline"]
    window = facts.get("window_minutes", cfg["window_minutes"])
    warming = baseline.hours < b["history_hours"]
    flags = []
    for section, source, counts in ((s, src, c) for s in log_sections(facts) for src, c in s["by_source"].items()):
        n = counts["errors"]
        if not n:
            continue
        if warming:
            med, threshold = None, b["warmup_floor"]
            normal = (f"baseline warming up: {baseline.hours}/{b['history_hours']} h of history, "
                      f"fixed threshold {threshold}")
        else:
            med, threshold = spike_threshold(baseline.error_counts.get(source, [0] * baseline.hours), cfg)
            normal = f"normal: median {med:g}/h, threshold {threshold:g}"
        if n <= threshold:
            continue
        sev = "crit" if n >= threshold * b["spike_crit_multiplier"] else "warn"
        flags.append(Flag(f"error_spike:{source}", "error_spike", sev,
                          f"{source}: {n} errors in the last {window} min ({normal})",
                          kind="event", evidence=examples(section, source)))
    return flags


def novel_errors(facts, baseline, cfg):
    window = facts.get("window_minutes", cfg["window_minutes"])
    novel = sorted(((slot["count"], source, template, slot["examples"])
                    for section in log_sections(facts)
                    for source, templates in section.get("error_templates", {}).items()
                    for template, slot in templates.items()
                    if (source, template) not in baseline.known_templates),
                   key=lambda x: (-x[0], x[1], x[2]))
    limit = cfg["baseline"]["novel_max_flags"]
    flags = [Flag(f"novel_error:{source}:{fingerprint(source, template)}", "novel_error", "warn",
                  f"{source}: an error never seen before ({count}x in the last {window} min)",
                  kind="event", evidence=[f"template: {template}", *exs])
             for count, source, template, exs in novel[:limit]]
    if len(novel) > limit:
        rest = novel[limit:]
        flags.append(Flag("novel_error:overflow", "novel_error", "warn",
                          f"{len(rest)} more never-seen-before errors from "
                          f"{len({s for _, s, _, _ in rest})} source(s)",
                          kind="event", evidence=[f"{s}: {t}" for _, s, t, _ in rest[:5]]))
    return flags


def examples(section, source, limit=3):
    lines = []
    for slot in sorted(section.get("error_templates", {}).get(source, {}).values(), key=lambda s: -s["count"]):
        lines.extend(slot["examples"])
    return lines[:limit]


def rebooted_since_last_run(facts, baseline):
    boot = (ok(facts, "host") or {}).get("boot_time")
    return boot if boot and baseline.previous_run_at and boot > baseline.previous_run_at else None


def host_reboot(facts, baseline, cfg):
    boot = rebooted_since_last_run(facts, baseline)
    if not boot:
        return []
    when = datetime.fromtimestamp(boot, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return [Flag(f"host_reboot:{int(boot)}", "host_reboot", "warn",
                 f"The server rebooted at {when}, since the last check", kind="event")]


def containers(facts, baseline, cfg):
    docker = ok(facts, "docker")
    if not docker:
        return []
    ignore = set(cfg["docker"]["ignore"])
    rebooted = rebooted_since_last_run(facts, baseline)
    flags, seen = [], set()
    for c in docker["containers"]:
        name = c["name"]
        seen.add(name)
        if name in ignore:
            continue
        prev = baseline.containers.get(name)
        if c["state"] == "restarting":
            flags.append(Flag(f"container_restarting:{name}", "container", "crit",
                              f"Container {name} is stuck restarting ({c['status']})"))
        elif c["state"] != "running" and prev and prev["was_running"]:
            flags.append(Flag(f"container_down:{name}", "container", "crit",
                              f"Container {name} is not running ({c['status']}); it was running before"))
        if c["health"] == "unhealthy":
            flags.append(Flag(f"container_unhealthy:{name}", "container", "warn",
                              f"Container {name} is failing its healthcheck ({c['status']})"))
        up = uptime_bounds(c["status"]) if c["state"] == "running" else None
        if (up and prev and not rebooted and prev["id"] == c["id"] and prev["state"] == "running"
                and prev["up_min"] is not None
                and up[1] < prev["up_min"] + (docker["checked_at"] - prev["seen_at"]) - RESTART_SLACK):
            flags.append(Flag(f"container_restart:{name}", "container", "warn",
                              f"Container {name} restarted since the last check (now: {c['status']})",
                              kind="event"))
    for name, prev in baseline.containers.items():
        if name not in seen and name not in ignore and prev["was_running"]:
            flags.append(Flag(f"container_gone:{name}", "container", "warn",
                              f"Container {name} no longer exists (it was running before)", kind="event"))
    return flags


RULES = [blind_spots, disk, memory, load, failed_units, wifi, host_reboot, containers,
         error_spikes, novel_errors]


def evaluate(facts, baseline, cfg, rules=RULES):
    flags = [flag for rule in rules for flag in rule(facts, baseline, cfg)]
    return sorted(flags, key=lambda f: (-RANK[f.severity], f.key))
