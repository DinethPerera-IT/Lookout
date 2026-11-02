"""What changed shortly before a failure: evidence for the after_recent_change cause.

Three read-only sources, all looked at by code:
- apt history (/var/log/apt/history.log): installs, upgrades, removals
- the container itself: when it was (re)created, e.g. by `docker compose up` after a pull
- config files: modification times of the unit file and its drop-ins, or the compose file

Limits, on purpose: an image's pull time would need /images/json, which the read-only
proxy blocks, so a container's (re)creation time stands in for it. Files under /home
(compose files) are invisible to the sandboxed service (ProtectHome) and are skipped.
"""
import os
import time
from datetime import datetime

APT_LOG = "/var/log/apt/history.log"
WINDOW = 24 * 3600          # look this far back before the failure
OWN_FILES = ("lookout-onfailure.conf",)   # Lookout's own hook isn't a change to the service
MAX_NAMES = 3


def ago_text(seconds):
    if seconds < 90:
        return f"{max(0, round(seconds))} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{round(seconds / 3600)} h"


def _local_ts(text):
    """apt writes local time like '2026-11-01  07:09:18'."""
    return time.mktime(datetime.strptime(" ".join(text.split()), "%Y-%m-%d %H:%M:%S").timetuple())


def _packages(value):
    """'a:amd64 (1, 2), b (3)' -> ['a', 'b']"""
    names, depth, current = [], 0, ""
    for ch in value:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += ch
    names.append(current)
    return [n.strip().split(" ")[0].split(":")[0] for n in names if n.strip()]


def apt_changes(since, until, path=APT_LOG):
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return []
    changes = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.strip().splitlines() if ": " in line)
        try:
            at = _local_ts(fields.get("Start-Date", ""))
        except ValueError:
            continue
        if not since <= at <= until:
            continue
        actions = []
        for key, verb in (("Upgrade", "upgraded"), ("Install", "installed"), ("Remove", "removed"), ("Purge", "purged")):
            if key in fields:
                pkgs = _packages(fields[key])
                more = f", +{len(pkgs) - MAX_NAMES} more" if len(pkgs) > MAX_NAMES else ""
                actions.append(f"{verb} {len(pkgs)} package{'s' * (len(pkgs) != 1)} ({', '.join(pkgs[:MAX_NAMES])}{more})")
        if actions:
            changes.append({"at": at, "kind": "apt", "what": "apt: " + "; ".join(actions)})
    return changes


def container_changes(target, since, until):
    created = target.get("created")
    if isinstance(created, (int, float)) and since <= created <= until:
        return [{"at": float(created), "kind": "container",
                 "what": f"container {target['name']} was (re)created from image {target.get('image') or '?'}"}]
    return []


def file_changes(paths, since, until, stat=os.stat):
    changes = []
    for path in paths:
        if not path or os.path.basename(path) in OWN_FILES:
            continue
        try:
            mtime = stat(path).st_mtime
        except OSError:      # missing, or hidden by the sandbox (ProtectHome)
            continue
        if since <= mtime <= until:
            changes.append({"at": mtime, "kind": "config", "what": f"{path} was modified"})
    return changes


def recent(cfg, evidence, now, apt_log=APT_LOG, stat=os.stat):
    """Changes in the WINDOW before the failure, newest first, each with a code-written summary."""
    until = evidence.get("failed_at") or now
    since = until - WINDOW
    target = evidence.get("target") or {}
    paths = [target.get("fragment_path"), *target.get("drop_ins", []), *target.get("compose_files", [])]
    found = (apt_changes(since, until, apt_log) + container_changes(target, since, until)
             + file_changes(paths, since, until, stat))
    for c in found:
        c["summary"] = f"{c['what']}, {ago_text(until - c['at'])} before the failure"
    return sorted(found, key=lambda c: -c["at"])
