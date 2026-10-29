"""Journal error counts and templates per source, plus WiFi re-authentication count.

Messages are redacted as they are parsed, so no unredacted log text exists
outside this module.
"""
import json
import re
import subprocess

from ..redact import redact
from ..templates import normalize_source, template_of
from . import run

ERROR_PRIORITY = 3  # syslog: 0 emerg .. 3 err
MAX_EXAMPLES = 3
MAX_EXAMPLE_LENGTH = 300


def source_of(entry):
    """Group entries by systemd unit, falling back to the syslog identifier."""
    if entry.get("_TRANSPORT") == "kernel":
        return "kernel"
    return normalize_source(entry.get("_SYSTEMD_UNIT") or entry.get("SYSLOG_IDENTIFIER") or "unknown")


def message_of(entry):
    msg = entry.get("MESSAGE", "")
    # journald encodes non-UTF-8 messages as a list of byte values
    if isinstance(msg, list):
        return bytes(msg).decode("utf-8", errors="replace")
    return msg or ""


def is_error(entry):
    return int(entry.get("PRIORITY", 6)) <= ERROR_PRIORITY


def timestamp_of(entry):
    return int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000


def noise_patterns(cfg):
    return [re.compile(n["pattern"]) for n in cfg["journal"].get("known_noise", [])]


def parse(stdout):
    entries = []
    for line in stdout.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        entry["MESSAGE"] = redact(message_of(entry))
        entries.append(entry)
    return entries


def read(since_minutes, timeout, kernel_only=False, until_minutes=None, errors_only=False):
    cmd = ["journalctl", f"--since=-{since_minutes}min", "-o", "json", "--no-pager", "-q",
           "--output-fields=PRIORITY,_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,_TRANSPORT,MESSAGE"]
    if until_minutes is not None:
        cmd.append(f"--until=-{until_minutes}min")
    if errors_only:
        cmd.append(f"--priority={ERROR_PRIORITY}")
    if kernel_only:
        cmd.append("-k")
    return parse(run(cmd, timeout))


def summarise(entries, noise=()):
    """Count entries per source; group errors (minus known noise) into templates."""
    by_source = {}
    error_templates = {}
    for entry in entries:
        source = source_of(entry)
        counts = by_source.setdefault(source, {"total": 0, "errors": 0, "noise": 0})
        counts["total"] += 1
        if not is_error(entry):
            continue
        message = entry["MESSAGE"]
        if any(p.search(message) for p in noise):
            counts["noise"] += 1
            continue
        counts["errors"] += 1
        slot = error_templates.setdefault(source, {}).setdefault(
            template_of(message), {"count": 0, "examples": []})
        slot["count"] += 1
        if len(slot["examples"]) < MAX_EXAMPLES:
            slot["examples"].append(message[:MAX_EXAMPLE_LENGTH])
    return {"total": len(entries), "by_source": dict(sorted(by_source.items())),
            "error_templates": error_templates}


def count_matching(entries, pattern):
    return sum(1 for e in entries if pattern in message_of(e))


def collect(cfg):
    timeout = cfg["journal"]["timeout_seconds"]
    wifi = cfg["wifi"]
    kernel = read(wifi["window_hours"] * 60, timeout, kernel_only=True)
    return {
        "ok": True,
        **summarise(read(cfg["window_minutes"], timeout), noise_patterns(cfg)),
        "wifi_reauths": {"window_hours": wifi["window_hours"],
                         "count": count_matching(kernel, wifi["reauth_pattern"])},
    }


def oldest_timestamp(timeout=30):
    """Timestamp of the oldest journal entry, or None if the journal is empty."""
    proc = subprocess.Popen(["journalctl", "-o", "json", "--no-pager", "-q",
                             "--output-fields=PRIORITY"], stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
    finally:
        proc.kill()
        proc.wait(timeout)
    return timestamp_of(json.loads(line)) if line else None


def history(cfg, now):
    """Hourly error counts and known templates from journal history, to seed the baseline.

    Covers [window start - history_hours, window start), so the current window is
    never part of its own baseline.
    """
    window = cfg["window_minutes"]
    hours = cfg["baseline"]["history_hours"]
    noise = noise_patterns(cfg)
    # One extra hour so the oldest hour bucket is complete
    entries = read(hours * 60 + window + 60, cfg["journal"]["timeout_seconds"],
                   until_minutes=window, errors_only=True)
    samples, templates = {}, {}
    for entry in entries:
        if any(p.search(entry["MESSAGE"]) for p in noise):
            continue
        source, ts = source_of(entry), timestamp_of(entry)
        key = (source, int(ts // 3600))
        samples[key] = samples.get(key, 0) + 1
        tkey = (source, template_of(entry["MESSAGE"]))
        first, last, count = templates.get(tkey, (ts, ts, 0))
        templates[tkey] = (min(first, ts), max(last, ts), count + 1)
    window_start = now - window * 60
    first_full_hour = (int(window_start // 3600) - hours) * 3600
    oldest = oldest_timestamp()
    if oldest is None:
        oldest = window_start
    return {"start": max(oldest, first_full_hour), "samples": samples, "templates": templates}
