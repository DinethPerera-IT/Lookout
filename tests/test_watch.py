"""The Docker events watcher: parsing, debouncing, hints, reconnecting."""
import io
import json
import queue
import threading
import urllib.error

import pytest

from lookout import watch

CFG = {"docker": {"proxy_url": "http://127.0.0.1:2375", "ignore": ["noisy"]}}


def event(action, name, t, exit_code=None):
    attrs = {"name": name, "image": "lookout-demo-app:local"}
    if exit_code is not None:
        attrs["exitCode"] = str(exit_code)
    return {"Type": "container", "Action": action, "Actor": {"ID": "abc", "Attributes": attrs},
            "time": int(t), "timeNano": int(t * 1e9)}


def test_parse_real_event_shapes():
    ev = watch.parse(json.dumps(event("die", "lookout-demo-oom", 1800000000.25, 137)))
    assert ev == {"action": "die", "name": "lookout-demo-oom", "at": pytest.approx(1800000000.25), "exit_code": 137}
    assert watch.parse(json.dumps(event("health_status: unhealthy", "web", 1.0)))["action"] == "health_status: unhealthy"
    for junk in ("", "not json", "[]", json.dumps({"Type": "network", "Action": "disconnect"}),
                 json.dumps(event("exec_start: sh -c healthcheck", "web", 1.0)),
                 json.dumps({"Type": "container", "Action": "die", "Actor": {"Attributes": {"name": ["x"]}}})):
        assert watch.parse(junk) is None


def test_hostile_exit_codes_are_dropped():
    ev = watch.parse(json.dumps(event("die", "web", 1.0, "1; rm -rf /")))
    assert ev["exit_code"] is None


def test_ignored_containers_and_starting_health_are_not_relevant():
    assert not watch.relevant(watch.parse(json.dumps(event("die", "noisy", 1.0, 1))), CFG)
    assert not watch.relevant(watch.parse(json.dumps(event("health_status: starting", "web", 1.0))), CFG)
    assert watch.relevant(watch.parse(json.dumps(event("die", "web", 1.0, 1))), CFG)


def test_a_burst_becomes_one_hint_per_container():
    burst = [watch.parse(json.dumps(e)) for e in [
        event("oom", "lookout-demo-oom", 100.0), event("die", "lookout-demo-oom", 100.2, 137),
        event("die", "lookout-demo-db", 101.0, 1), event("start", "grafana", 102.0)]]
    assert watch.hints_for(burst) == {"containers": {
        "lookout-demo-oom": {"at": 100.0, "oom": True, "exit_code": 137},
        "lookout-demo-db": {"at": 101.0, "exit_code": 1}}}
    assert watch.trigger_text(burst) == "docker oom lookout-demo-oom, die lookout-demo-oom, die lookout-demo-db, start grafana"


def test_debounce_turns_a_burst_into_one_run():
    q, stop, runs = queue.Queue(), threading.Event(), []
    for e in [event("oom", "a", 1.0), event("die", "a", 1.1, 137), event("die", "b", 1.2, 1)]:
        q.put(watch.parse(json.dumps(e)))

    def runner(passthrough, burst):
        runs.append((passthrough, [e["action"] for e in burst]))
        stop.set()

    watch.loop(CFG, ["--shadow"], q, stop, runner=runner, debounce=0.2)
    assert runs == [(["--shadow"], ["oom", "die", "die"])]


class Stream(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_follow_reconnects_with_since_the_last_event():
    urls, q, stop = [], queue.Queue(), threading.Event()
    streams = [Stream((json.dumps(event("die", "web", 1800000050.5, 1)) + "\n").encode()),
               urllib.error.URLError(TimeoutError())]

    def opener(url, timeout):
        urls.append(url)
        item = streams.pop(0) if streams else None
        if item is None:
            stop.set()
            raise urllib.error.URLError(ConnectionError())
        if isinstance(item, Exception):
            raise item
        return item

    watch.follow(CFG, q, stop, opener=opener, sleep=lambda s: None)
    assert q.get_nowait()["name"] == "web"
    assert "since=1800000050.500" in urls[1]                 # no event is missed after a reconnect
    assert "%22event%22%3A+%5B%22die%22" in urls[0]          # the filter asks only for the events we use


def test_the_run_command_passes_hints_as_data(monkeypatch):
    seen = {}
    monkeypatch.setattr(watch.subprocess, "run", lambda cmd, check: seen.setdefault("cmd", cmd) and type("R", (), {"returncode": 0}))
    watch.run_lookout(["--shadow"], [watch.parse(json.dumps(event("die", "x; reboot", 5.0, 1)))])
    cmd = seen["cmd"]
    assert cmd[1:4] == ["-m", "lookout", "--shadow"]
    assert json.loads(cmd[cmd.index("--hints") + 1]) == {"containers": {"x; reboot": {"at": 5.0, "exit_code": 1}}}
    # an argument list, never a shell string: the odd name is just data
    assert all(isinstance(part, str) for part in cmd)
