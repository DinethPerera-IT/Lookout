"""React in seconds: python -m lookout.watch [--shadow] [--no-llm] ...

Follows Docker's container events (die, oom, health_status, start) through the
read-only proxy and starts an immediate Lookout run when something happens.
Events are debounced, so one crash (die + oom + network teardown) is one run, and
every run still goes through the normal pipeline: the same rules, the same
dedup, the same report. The event only contributes *when* it happened (for
time-to-detect), the exit code and the OOM flag. The hourly timer stays as the
safety net, and a file lock keeps runs from overlapping.
"""
import argparse
import json
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

EVENTS = ("die", "oom", "health_status", "start")
DEBOUNCE = 3.0          # seconds to collect related events after the first one
READ_TIMEOUT = 300      # no event for this long: reconnect (the proxy may cut idle streams anyway)
RECONNECT_DELAY = 5


def events_url(cfg, since):
    filters = json.dumps({"type": ["container"], "event": list(EVENTS)})
    return (f"{cfg['docker']['proxy_url']}/events?"
            + urllib.parse.urlencode({"filters": filters, "since": f"{since:.3f}"}))


def parse(line):
    try:
        ev = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(ev, dict) or ev.get("Type") != "container":
        return None
    action = str(ev.get("Action", ""))
    attrs = (ev.get("Actor") or {}).get("Attributes") or {}
    name = attrs.get("name")
    if action.split(":")[0] not in EVENTS or not isinstance(name, str):
        return None
    at = ev.get("timeNano")
    at = at / 1e9 if isinstance(at, (int, float)) else float(ev.get("time", time.time()))
    exit_code = attrs.get("exitCode")
    return {"action": action, "name": name, "at": at,
            "exit_code": int(exit_code) if isinstance(exit_code, str) and exit_code.lstrip("-").isdigit() else None}


def relevant(ev, cfg):
    if ev is None or ev["name"] in cfg["docker"]["ignore"]:
        return False
    return ev["action"] != "health_status: starting"


def hints_for(events):
    """Collapse a burst of events into one hint per container: the failure time is the
    earliest die/oom, the exit code comes from die, and an oom event marks the OOM kill."""
    hints = {}
    for ev in sorted(events, key=lambda e: e["at"]):
        if ev["action"] not in ("die", "oom"):
            continue
        h = hints.setdefault(ev["name"], {"at": ev["at"]})
        if ev["action"] == "oom":
            h["oom"] = True
        if ev["exit_code"] is not None:
            h["exit_code"] = ev["exit_code"]
    return {"containers": hints}


def trigger_text(events):
    parts = list(dict.fromkeys(f"{e['action'].split(':')[0]} {e['name']}" for e in events))
    return "docker " + ", ".join(parts[:5]) + (" …" if len(parts) > 5 else "")


def follow(cfg, out, stop, opener=urllib.request.urlopen, sleep=time.sleep):
    """Reader thread: push parsed events onto `out`, reconnecting forever with since=<last seen>."""
    since = time.time()
    while not stop.is_set():
        try:
            with opener(events_url(cfg, since), timeout=READ_TIMEOUT) as resp:
                print("lookout.watch: following Docker events", flush=True)
                for line in resp:
                    ev = parse(line)
                    if ev:
                        since = max(since, ev["at"])
                        if relevant(ev, cfg):
                            out.put(ev)
                    if stop.is_set():
                        return
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            print(f"lookout.watch: event stream ended ({type(reason).__name__}); reconnecting", flush=True)
        sleep(RECONNECT_DELAY)


def run_lookout(passthrough, events):
    cmd = [sys.executable, "-m", "lookout", *passthrough,
           "--trigger", trigger_text(events), "--hints", json.dumps(hints_for(events))]
    return subprocess.run(cmd, check=False).returncode


def loop(cfg, passthrough, events, stop, runner=run_lookout, debounce=DEBOUNCE, clock=time.monotonic):
    """Main loop: wait for an event, gather the burst, run once."""
    while not stop.is_set():
        try:
            first = events.get(timeout=1)
        except queue.Empty:
            continue
        burst, deadline = [first], clock() + debounce
        while (left := deadline - clock()) > 0:
            try:
                burst.append(events.get(timeout=left))
            except queue.Empty:
                break
        print(f"lookout.watch: {trigger_text(burst)} -> run", flush=True)
        runner(passthrough, burst)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lookout.watch", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config")
    parser.add_argument("--state")
    parser.add_argument("--env-file")
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    passthrough = [f for flag, value in (("--config", args.config), ("--state", args.state),
                                         ("--env-file", args.env_file)) if value for f in (flag, value)]
    passthrough += ["--shadow"] * args.shadow + ["--no-llm"] * args.no_llm
    events, stop = queue.Queue(), threading.Event()
    threading.Thread(target=follow, args=(cfg, events, stop), daemon=True).start()
    try:
        loop(cfg, passthrough, events, stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
