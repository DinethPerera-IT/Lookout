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
import subprocess
import time
from datetime import datetime

APT_LOG = "/var/log/apt/history.log"
# A container can only be affected by its own changes, or by Docker itself being upgraded.
DOCKER_PACKAGES = {"docker-ce", "docker-ce-cli", "docker.io", "containerd", "containerd.io", "runc",
                   "docker-compose-plugin", "docker-buildx-plugin", "moby-engine"}
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
            touched = [p for key in ("Upgrade", "Install", "Remove", "Purge") if key in fields
                       for p in _packages(fields[key])]
            changes.append({"at": at, "kind": "apt", "what": "apt: " + "; ".join(actions), "packages": touched})
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


def dpkg_owner(path):
    """The package that installed a file (`dpkg -S`), or None for files no package owns."""
    if not path:
        return None
    # Packages record unit files under /lib, but systemd reports /usr/lib (merged /usr): try both.
    for candidate in dict.fromkeys([path, path.removeprefix("/usr") if path.startswith("/usr/lib/") else path]):
        try:
            out = subprocess.run(["dpkg", "-S", candidate], capture_output=True, text=True, timeout=10,
                                 check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        if ":" in out:
            return out.split(":")[0].strip() or None
    return None


def relevant_apt(change, target, owner):
    """An apt run only counts if it touched something this target depends on: for a container,
    Docker itself; for a unit, the package that installed the unit. `apt install gh` two hours
    before a planned Grafana stop is not a cause (seen live)."""
    packages = set(change.get("packages", []))
    if target.get("kind") == "container":
        return bool(packages & DOCKER_PACKAGES)
    package = owner(target.get("fragment_path"))
    return package is not None and package in packages


def recent(cfg, evidence, now, apt_log=APT_LOG, stat=os.stat, owner=dpkg_owner):
    """Changes in the WINDOW before the failure that can affect this target, newest first,
    each with a code-written summary."""
    until = evidence.get("failed_at") or now
    since = until - WINDOW
    target = evidence.get("target") or {}
    paths = [target.get("fragment_path"), *target.get("drop_ins", []), *target.get("compose_files", [])]
    apt = [c for c in apt_changes(since, until, apt_log) if relevant_apt(c, target, owner)]
    found = apt + container_changes(target, since, until) + file_changes(paths, since, until, stat)
    for c in found:
        c["summary"] = f"{c['what']}, {ago_text(until - c['at'])} before the failure"
    return sorted(found, key=lambda c: -c["at"])
