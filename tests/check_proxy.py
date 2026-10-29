"""Verify the read-only Docker proxy allows exactly what ops-summary needs.

Run: .venv/bin/python -m tests.check_proxy
"Must be blocked" checks target a container that doesn't exist, so even a
misconfigured proxy can't change anything: Docker would answer 404 instead of
the proxy's 403, and the check fails loudly. Response bodies are never printed.
"""
import json
import sys
import urllib.error
import urllib.request

from ops_summary import config

FAKE = "ops-summary-check-does-not-exist"


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
        ("blocked: events", "GET", "/events", 403),
    ]
    failures = 0
    for name, method, path, expected in checks:
        got = status(base, method, path)
        ok = got == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:36} {method:6} -> {got} (expected {expected})")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
