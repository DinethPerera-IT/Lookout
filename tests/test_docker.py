import json
from pathlib import Path

import pytest

from lookout.collectors import CollectorError, docker

FIXTURE = (Path(__file__).parent / "fixtures" / "docker" / "containers.json").read_bytes()
IDS = {c["Names"][0].lstrip("/"): c["Id"][:12] for c in json.loads(FIXTURE)}


def frames(*lines, stream=1):
    """Build Docker's multiplexed log format: 8-byte header + payload per frame."""
    out = b""
    for line in lines:
        payload = (line + "\n").encode()
        out += bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload
    return out


LOGS = {
    "grafana": frames(
        '2026-10-29T10:01:00.123456789Z logger=x t=now level=info msg="Request completed"',
        '2026-10-29T10:02:00.5Z logger=plugin level=error msg="Failed to install plugin" password=hunter2'),
    "uptime-kuma": frames("2026-10-29T10:03:00+13:00 [MONITOR] ERROR: Cannot send notification to Uptime Alert",
                          stream=2),
}


def fake_fetch(calls):
    ids = {c["Id"][:12]: c["Names"][0].lstrip("/") for c in json.loads(FIXTURE)}

    def fetch(url, timeout, limit=None):
        calls.append(url)
        if "/containers/json" in url:
            return FIXTURE
        cid = url.split("/containers/")[1].split("/")[0]
        return LOGS.get(ids[cid], b"")

    return fetch


def test_list_keeps_only_needed_fields(cfg):
    containers = docker.list_containers(cfg, fake_fetch([]))
    assert {tuple(sorted(c)) for c in containers} == {("health", "id", "image", "name", "state", "status")}
    kuma = next(c for c in containers if c["name"] == "uptime-kuma")
    assert kuma["health"] == "healthy" and kuma["state"] == "running"


def test_collect_counts_container_errors_and_redacts(cfg):
    calls = []
    facts = docker.collect(cfg, fetch=fake_fetch(calls))
    assert facts["by_source"]["docker:grafana"] == {"total": 2, "errors": 1, "noise": 0}
    assert facts["by_source"]["docker:uptime-kuma"]["errors"] == 1
    assert "hunter2" not in json.dumps(facts)
    # Logs are only fetched for live containers, never for the long-exited one
    assert not any(IDS["clever_swirles"] in u for u in calls)
    assert sum("/logs" in u for u in calls) == 3


def test_ignored_containers_are_not_read(cfg):
    cfg["docker"]["ignore"] = ["grafana"]
    calls = []
    docker.collect(cfg, fetch=fake_fetch(calls))
    assert any(IDS["uptime-kuma"] in u for u in calls)
    assert not any(IDS["grafana"] in u for u in calls)


@pytest.mark.parametrize("status, bounds", [
    ("Up 13 days (healthy)", (13 * 86400 - 1800, 14 * 86400 - 1800)),
    ("Up About an hour", (3600, 5400)),
    ("Up 5 minutes (health: starting)", (300, 360)),
    ("Up Less than a second", (0, 1)),
    ("Up 2 hours (Paused)", (5400, 9000)),
    ("Exited (1) 3 months ago", None),
    ("Restarting (1) 5 seconds ago", None),
])
def test_uptime_bounds(status, bounds):
    assert docker.uptime_bounds(status) == bounds


def test_demux_handles_both_formats():
    assert docker.demux(frames("a", "b")) == "a\nb\n"
    assert docker.demux(b"plain tty output\n") == "plain tty output\n"


def test_log_lines_parse_nanosecond_and_offset_timestamps():
    lines = list(docker.log_lines("2026-10-29T10:00:00.123456789Z hello\n2026-10-29T23:00:00+13:00 world\nno ts"))
    assert [m for _, m in lines] == ["hello", "world"]
    assert lines[0][0] == pytest.approx(lines[1][0] + 0.123456, abs=1e-5)  # same instant, different tz


def test_history_buckets_container_errors(cfg):
    calls = []
    h = docker.history(cfg, 1_793_300_000, fetch=fake_fetch(calls))
    assert ("docker:grafana", 1_793_268_120 // 3600) in h["samples"]
    assert all("until=" in u for u in calls if "/logs" in u)


def test_unreachable_proxy(cfg):
    cfg["docker"]["proxy_url"] = "http://127.0.0.1:1"
    with pytest.raises(CollectorError, match="unreachable"):
        docker.collect(cfg)
