"""Verify the read-only Docker proxy allows exactly what Lookout needs.

Run: .venv/bin/python -m tests.check_proxy
"Must be blocked" checks target a container that doesn't exist, so even a
misconfigured proxy can't change anything: Docker would answer 404 instead of
the proxy's 403, and the check fails loudly. Response bodies are never printed.
"""
import json
import sys
from pathlib import Path
import time
import urllib.error
import urllib.request

from lookout import config

FAKE = "lookout-check-does-not-exist"
RECORD = Path(__file__).resolve().parent.parent / "lookout" / "dashboard" / "static" / "proxy-check.json"


def status(base, method, path):
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read(1)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    base = config.load()["docker"]["proxy_url"]
    with urllib.request.urlopen(base + "/containers/json", timeout=10) as resp:
        live = json.load(resp)[0]["Id"][:12]
    checks = [
        ("allowed: ping", "GET", "/_ping", 200),
        ("allowed: list containers", "GET", "/containers/json?all=1", 200),
        ("allowed: list (versioned API)", "GET", "/v1.52/containers/json", 200),
        ("allowed: logs", "GET", f"/containers/{live}/logs?stdout=1&tail=1", 200),
        ("blocked: inspect (env vars)", "GET", f"/containers/{FAKE}/json", 403),
        ("blocked: inspect (versioned)", "GET", f"/v1.52/containers/{FAKE}/json", 403),
        ("blocked: archive (copy files out)", "GET", f"/containers/{FAKE}/archive?path=/", 403),
        ("blocked: export (whole filesystem)", "GET", f"/containers/{FAKE}/export", 403),
        ("blocked: path trick", "GET", f"/containers/{FAKE}/../json", 403),
        ("blocked: follow logs forever", "GET", f"/containers/{live}/logs?stdout=1&follow=1", 403),
        ("blocked: restart", "POST", f"/containers/{FAKE}/restart", 403),
        ("blocked: create", "POST", "/containers/create", 403),
        ("blocked: exec", "POST", f"/containers/{FAKE}/exec", 403),
        ("blocked: delete", "DELETE", f"/containers/{FAKE}", 403),
        ("blocked: images", "GET", "/images/json", 403),
        ("blocked: system info", "GET", "/info", 403),
        # until= makes the stream end at once instead of following forever
        ("allowed: events (bounded)", "GET", f"/events?since={int(time.time()) - 5}&until={int(time.time())}", 200),
        ("blocked: events via POST", "POST", "/events", 403),
        ("blocked: system events path trick", "GET", "/system/events", 403),
    ]
    failures, results = 0, []
    for name, method, path, expected in checks:
        got = status(base, method, path)
        ok = got == expected
        failures += not ok
        results.append({"check": name, "method": method, "path": path.replace(live, "<container>"),
                        "expected": expected, "got": got, "ok": ok})
        print(f"{'PASS' if ok else 'FAIL'}  {name:36} {method:6} -> {got} (expected {expected})")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    if "--record" in sys.argv:  # for the dashboard's Security page: a recorded proof, not a live POST
        RECORD.write_text(json.dumps({"verified_at": time.time(), "passed": len(checks) - failures,
                                      "total": len(checks), "checks": results}, indent=1) + "\n")
        print(f"recorded to {RECORD.relative_to(RECORD.parents[3])}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
