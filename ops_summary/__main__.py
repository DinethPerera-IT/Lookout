"""Command-line entry point: python -m ops_summary --dry-run"""
import argparse
import json
import sys
from datetime import datetime, timezone

from . import config
from .collectors import host, journal, prometheus, systemd

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
            # a blind spot that detection reports later.
            facts[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return facts


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ops_summary", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print results instead of sending anything")
    parser.add_argument("--stage", choices=["collect"], default="collect",
                        help="how far to run the pipeline (more stages arrive in later phases)")
    parser.add_argument("--config", help="path to config.toml")
    args = parser.parse_args(argv)

    if not args.dry_run:
        parser.error("sending is not implemented yet (phase 5); use --dry-run")

    facts = collect_all(config.load(args.config))
    print(json.dumps(facts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
