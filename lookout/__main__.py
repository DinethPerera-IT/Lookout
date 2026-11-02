"""Command-line entry point: python -m lookout --dry-run"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, llm, notify, pipeline, report
from .collectors import docker, host, journal, prometheus, systemd
from .state import State

SHADOW_PREFIX = "👻 SHADOW · "

# Metrics first, so later stages (the LLM in particular) can't distort them.
COLLECTORS = {
    "host": host.collect,
    "prometheus": prometheus.collect,
    "systemd": systemd.collect,
    "journal": journal.collect,
    "docker": docker.collect,
}


def collect_all(cfg, collectors=COLLECTORS):
    facts = {
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_minutes": cfg["window_minutes"],
    }
    for name, collect in collectors.items():
        try:
            facts[name] = collect(cfg)
        except Exception as e:
            # A broken source must not stop the others. Its failure is itself a fact:
            # a blind spot that detection reports.
            facts[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return facts


def state_path(args, cfg):
    if args.state:
        return Path(args.state)
    if os.environ.get("STATE_DIRECTORY"):  # set by systemd's StateDirectory=
        return Path(os.environ["STATE_DIRECTORY"]) / "state.sqlite"
    return Path(cfg["state"]["default_path"]).expanduser()


def open_state(args, cfg):
    path = state_path(args, cfg)
    if args.dry_run and not path.exists():
        return State(":memory:")  # a dry run never creates files
    path.parent.mkdir(parents=True, exist_ok=True)
    return State(path)


def seed_baselines(state, cfg, facts, now):
    try:
        pipeline.seed_if_needed(state, cfg, now)
    except Exception as e:
        # No baseline yet: spike rules fall back to warm-up mode, and this is reported.
        facts["baseline_seed"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if facts.get("docker", {}).get("ok"):  # if the proxy is down, the docker blind spot already says so
        try:
            pipeline.seed_docker_if_needed(state, cfg, now)
        except Exception as e:
            # Without a baseline every container error would look "novel": skip log rules this run.
            facts["docker"].update(by_source={}, error_templates={})
            facts["docker_baseline_seed"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}


def counts(result, ai=None):
    text = (f"{len(result['report'])} reported, {len(result['suppressed'])} suppressed, "
            f"{len(result['resolved'])} resolved")
    if ai is not None:
        text += " · AI: " + ("ok" if ai["ok"] else ai["reason"])
    return text


def explain(args, cfg, result):
    """The optional AI layer: only for reported flags, and only after detection is done."""
    if args.no_llm or not result["report"]:
        return None
    return llm.explain(cfg, result["report"])


def run(args, cfg):
    """One full run. State is committed only after the report was delivered, so a
    failed send can never mark an issue as "already reported".

    In shadow mode everything is real (state, cooldowns, heartbeat) except the
    destination: the report goes to the journal and, if configured, a private test
    channel. The real webhook is never used."""
    started = time.time()
    status = "SHADOW · OK" if args.shadow else "OK"
    facts = collect_all(cfg, COLLECTORS)
    now = time.time()
    state = open_state(args, cfg)
    try:
        seed_baselines(state, cfg, facts, now)
        result = pipeline.run(facts, state, cfg, now)
        ai = explain(args, cfg, result)  # never raises; on any failure the facts go out alone
        payload = report.build(result, SHADOW_PREFIX if args.shadow else "", ai=ai)
        if args.dry_run:
            print(json.dumps({"result": result, "ai": ai, "discord_payload": payload,
                              "heartbeat": f"would push status=up msg='{status} · {counts(result, ai)}'"}, indent=2))
            return 0
        if payload and args.shadow:
            print("lookout: shadow report: " + json.dumps(payload, ensure_ascii=False))
        webhook = config.secret("DISCORD_SHADOW_WEBHOOK_URL" if args.shadow else "DISCORD_WEBHOOK_URL")
        if payload and webhook:
            try:
                notify.send_discord(webhook, payload)
            except notify.NotifyError as e:
                print(f"lookout: {e}; nothing marked as reported, the next run will retry", file=sys.stderr)
                try:  # tell Kuma right away instead of waiting for a missed heartbeat
                    notify.push_kuma(config.secret("KUMA_PUSH_URL"), "down", f"Lookout could not deliver: {e}")
                except notify.NotifyError as e2:
                    print(f"lookout: {e2}", file=sys.stderr)
                return 1
        state.commit()
    finally:
        state.close()  # without commit() this rolls back
    try:
        notify.push_kuma(config.secret("KUMA_PUSH_URL"), "up", f"{status} · {counts(result, ai)}",
                         (time.time() - started) * 1000)
    except notify.NotifyError as e:
        print(f"lookout: {e}", file=sys.stderr)
        return 1
    outcome = ("nothing to send" if not payload else "report sent" if webhook else "report logged only")
    print(f"lookout: {'shadow · ' if args.shadow else ''}{counts(result, ai)} ({outcome})")
    return 0


def run_fixture(args, cfg):
    scenario, results = pipeline.run_scenario(args.fixture, cfg)
    if args.send:
        result = next((r for r in reversed(results) if r["report"] or r["resolved"]), None)
        if result is None:
            print("lookout: this scenario produces no report to send", file=sys.stderr)
            return 1
        ai = explain(args, cfg, result)
        notify.send_discord(config.secret("DISCORD_WEBHOOK_URL"), report.build(result, "🧪 TEST · ", ai=ai))
        print(f"lookout: sent the scenario's last report to Discord, marked as a test ({counts(result, ai)})")
        return 0
    for r in results:
        r["ai"] = explain(args, cfg, r)
        r["discord_payload"] = report.build(r, "🧪 TEST · ", ai=r["ai"])
    print(json.dumps({"description": scenario.get("description"), "runs": results}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lookout", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the Discord payload instead of sending; never changes stored state")
    parser.add_argument("--stage", choices=["collect", "detect"], default="detect",
                        help="collect: print facts only; detect: the full run")
    parser.add_argument("--fixture", help="run a scenario file (tests/fixtures/scenarios/*.json) in memory")
    parser.add_argument("--send", action="store_true",
                        help="with --fixture: send the scenario's last report to Discord as a test")
    parser.add_argument("--shadow", action="store_true",
                        help="real run (state, heartbeat), but the report only goes to the journal and "
                             "DISCORD_SHADOW_WEBHOOK_URL if set; never to DISCORD_WEBHOOK_URL")
    parser.add_argument("--no-llm", action="store_true", help="skip the AI explanation (facts only)")
    parser.add_argument("--env-file", help="load secrets from this file (systemd uses EnvironmentFile= instead)")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--state", help="path to the SQLite state file")
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    if args.env_file:
        config.load_env_file(args.env_file)
    if args.send and not args.fixture:
        parser.error("--send is only for test messages with --fixture; real runs send automatically")

    if args.send:
        needs = ["DISCORD_WEBHOOK_URL"]
    elif args.dry_run or args.fixture:
        needs = []
    elif args.shadow:
        needs = ["KUMA_PUSH_URL"]  # the shadow channel is optional; the journal always gets the report
    else:
        needs = list(config.SECRETS)
    missing = config.missing_secrets(needs)
    if missing:
        print(f"lookout: missing {', '.join(missing)} (set by systemd EnvironmentFile= or --env-file)",
              file=sys.stderr)
        return 2

    if args.fixture:
        return run_fixture(args, cfg)
    if args.stage == "collect":
        print(json.dumps(collect_all(cfg), indent=2))
        return 0
    return run(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
