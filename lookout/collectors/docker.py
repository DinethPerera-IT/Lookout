"""Container state and logs through the read-only Docker API proxy.

Only GET /containers/json (list), /containers/<id>/logs and /_ping are allowed
by the proxy. Inspect is deliberately not used: it would expose every
container's environment variables (often passwords).
"""
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

from ..logstats import History, LogStats
from ..redact import redact
from . import CollectorError

HOUR = 3600
LIVE_STATES = ("running", "restarting")


def http_get(url, timeout, limit=None):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read(limit) if limit else resp.read()
    except urllib.error.HTTPError as e:
        raise CollectorError(f"Docker proxy returned HTTP {e.code} for {url.split('?')[0]}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise CollectorError(f"Docker proxy unreachable: {e}")


def list_containers(cfg, fetch):
    d = cfg["docker"]
    raw = json.loads(fetch(f"{d['proxy_url']}/containers/json?all=1", d["timeout_seconds"]))
    return [{"name": c["Names"][0].lstrip("/"), "id": c["Id"][:12], "image": c["Image"],
             "state": c["State"], "status": c["Status"],
             "health": (c.get("Health") or {}).get("Status", "none"),
             # for "what changed before it failed": when it was (re)created, and its compose file(s)
             "created": c.get("Created"),
             "compose_files": [f for f in ((c.get("Labels") or {}).get(
                 "com.docker.compose.project.config_files") or "").split(",") if f]}
            for c in raw]


# --- uptime from the Status text ("Up 13 days (healthy)") ------------------
# Docker rounds uptime for display (docker/go-units HumanDuration). Each phrase
# maps to the range of real durations that produce it, so restarts can be
# detected safely: a container restarted if its uptime now is *certainly*
# shorter than its uptime last time plus the time that has passed.

_FIXED = {"Less than a second": (0, 1), "About a minute": (60, 120), "About an hour": (HOUR, 1.5 * HOUR)}
_UNIT_BOUNDS = {
    "second": lambda n: (n, n + 1),
    "minute": lambda n: (60 * n, 60 * (n + 1)),
    "hour": lambda n: ((n - 0.5) * HOUR, (n + 0.5) * HOUR),
    "day": lambda n: ((24 * n - 0.5) * HOUR, (24 * n + 23.5) * HOUR),
    "week": lambda n: ((168 * n - 0.5) * HOUR, (168 * n + 167.5) * HOUR),
    "month": lambda n: ((720 * n - 0.5) * HOUR, (720 * n + 719.5) * HOUR),
    "year": lambda n: (8760 * n * HOUR, 8760 * (n + 1) * HOUR),
}


def uptime_bounds(status):
    """(min, max) seconds of uptime for a running container's Status, else None."""
    m = re.match(r"^Up (.+?)(?: \([^)]*\))*$", status or "")
    if not m:
        return None
    text = m.group(1)
    if text in _FIXED:
        return _FIXED[text]
    m = re.fullmatch(r"(\d+) (second|minute|hour|day|week|month|year)s?", text)
    return _UNIT_BOUNDS[m.group(2)](int(m.group(1))) if m else None


# --- logs -------------------------------------------------------------------

def demux(raw):
    """Docker multiplexes stdout/stderr with 8-byte frame headers unless the container uses a TTY."""
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\0\0\0":
        frames, i = [], 0
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            frames.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        raw = b"".join(frames)
    return raw.decode("utf-8", errors="replace")


_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d) ?")


def log_lines(text):
    """Yield (timestamp, message) from `timestamps=1` log output."""
    for line in text.splitlines():
        m = _TS.match(line)
        if not m:
            continue
        frac = (m.group(2) or ".0")[:7]  # Python handles microseconds, Docker gives nanoseconds
        tz = "+00:00" if m.group(3) == "Z" else m.group(3)
        yield datetime.fromisoformat(m.group(1) + frac + tz).timestamp(), line[m.end():]


def error_patterns(cfg):
    return [re.compile(p) for p in cfg["docker"]["error_patterns"]]


def noise_patterns(cfg):
    return [re.compile(n["pattern"]) for n in cfg["docker"].get("known_noise", [])]


def fetch_logs(cfg, container, fetch, since, until=None):
    d = cfg["docker"]
    url = (f"{d['proxy_url']}/containers/{container['id']}/logs"
           f"?stdout=1&stderr=1&timestamps=1&since={int(since)}")
    if until is not None:
        url += f"&until={int(until)}"
    return log_lines(demux(fetch(url, d["timeout_seconds"], d["max_log_bytes"])))


def watched(cfg, containers):
    return [c for c in containers if c["state"] in LIVE_STATES and c["name"] not in cfg["docker"]["ignore"]]


def collect(cfg, fetch=http_get):
    checked_at = time.time()
    containers = list_containers(cfg, fetch)
    errors = error_patterns(cfg)
    stats = LogStats(noise_patterns(cfg))
    for c in watched(cfg, containers):
        for _, message in fetch_logs(cfg, c, fetch, checked_at - cfg["window_minutes"] * 60):
            message = redact(message)
            stats.add(f"docker:{c['name']}", message, any(p.search(message) for p in errors))
    return {"ok": True, "checked_at": checked_at, "containers": containers, **stats.result()}


def history(cfg, now, fetch=http_get):
    """Hourly error counts and templates from container logs, to seed the baseline."""
    window_start = now - cfg["window_minutes"] * 60
    since = window_start - (cfg["baseline"]["history_hours"] + 1) * HOUR
    errors = error_patterns(cfg)
    hist = History(noise_patterns(cfg))
    for c in watched(cfg, list_containers(cfg, fetch)):
        for ts, message in fetch_logs(cfg, c, fetch, since, until=window_start):
            message = redact(message)
            if any(p.search(message) for p in errors):
                hist.add_error(f"docker:{c['name']}", ts, message)
    return {"samples": hist.samples, "templates": hist.templates}
