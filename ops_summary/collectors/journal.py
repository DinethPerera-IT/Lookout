"""Journal error counts per source, plus WiFi re-authentication count.

Phase 1 returns counts only. Message text stays inside this module until the
redactor exists (phase 2), so no raw log line can reach stdout.
"""
import json

from . import run

ERROR_PRIORITY = 3  # syslog: 0 emerg .. 3 err


def source_of(entry):
    """Group entries by systemd unit, falling back to the syslog identifier."""
    if entry.get("_TRANSPORT") == "kernel":
        return "kernel"
    return entry.get("_SYSTEMD_UNIT") or entry.get("SYSLOG_IDENTIFIER") or "unknown"


def message_of(entry):
    msg = entry.get("MESSAGE", "")
    # journald encodes non-UTF-8 messages as a list of byte values
    if isinstance(msg, list):
        return bytes(msg).decode("utf-8", errors="replace")
    return msg or ""


def parse(stdout):
    entries = []
    for line in stdout.splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def read(since_minutes, timeout, kernel_only=False):
    cmd = ["journalctl", f"--since=-{since_minutes}min", "-o", "json", "--no-pager", "-q",
           "--output-fields=PRIORITY,_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,_TRANSPORT,MESSAGE"]
    if kernel_only:
        cmd.append("-k")
    return parse(run(cmd, timeout))


def summarise(entries):
    by_source = {}
    for entry in entries:
        counts = by_source.setdefault(source_of(entry), {"total": 0, "errors": 0})
        counts["total"] += 1
        if int(entry.get("PRIORITY", 6)) <= ERROR_PRIORITY:
            counts["errors"] += 1
    return {"total": len(entries), "by_source": dict(sorted(by_source.items()))}


def count_matching(entries, pattern):
    return sum(1 for e in entries if pattern in message_of(e))


def collect(cfg):
    timeout = cfg["journal"]["timeout_seconds"]
    wifi = cfg["wifi"]
    kernel = read(wifi["window_hours"] * 60, timeout, kernel_only=True)
    return {
        "ok": True,
        **summarise(read(cfg["window_minutes"], timeout)),
        "wifi_reauths": {"window_hours": wifi["window_hours"],
                         "count": count_matching(kernel, wifi["reauth_pattern"])},
    }
