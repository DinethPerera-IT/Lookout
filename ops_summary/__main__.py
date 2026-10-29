"""Command-line entry point: python -m ops_summary --dry-run"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, pipeline
from .collectors import host, journal, prometheus, systemd
from .state import State

# Metrics first, so later stages (the LLM in particular) can't distort them.
COLLECTORS = {
    "host": host.collect,
    "prometheus": prometheus.collect,
    "systemd": systemd.collect,
    "journal": journal.collect,
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


def detect(args, cfg):
    facts = collect_all(cfg)
    now = time.time()
    state = open_state(args, cfg)
    try:
        try:
            pipeline.seed_if_needed(state, cfg, now)
        except Exception as e:
            # No baseline yet: spike rules fall back to warm-up mode, and this is reported.
            facts["baseline_seed"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        result = pipeline.run(facts, state, cfg, now)
        if not args.dry_run:
            state.commit()
    finally:
        state.close()  # without commit() this rolls back
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ops_summary", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print results instead of sending; never changes stored state")
    parser.add_argument("--stage", choices=["collect", "detect"], default="detect",
                        help="how far to run the pipeline (more stages arrive in later phases)")
    parser.add_argument("--fixture", help="run a scenario file (tests/fixtures/scenarios/*.json) in memory")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--state", help="path to the SQLite state file")
    args = parser.parse_args(argv)
    cfg = config.load(args.config)

    if args.fixture:
        scenario, results = pipeline.run_scenario(args.fixture, cfg)
        print(json.dumps({"description": scenario.get("description"), "runs": results}, indent=2))
        return 0
    if not args.dry_run:
        parser.error("sending is not implemented yet (phase 5); use --dry-run")
    if args.stage == "collect":
        print(json.dumps(collect_all(cfg), indent=2))
    else:
        print(json.dumps(detect(args, cfg), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
