"""Glue between facts, state and rules: baseline loading, seeding, detection, recording."""
import json

from . import detect
from .collectors import journal
from .state import HOUR, State, decide


def window_start(cfg, now):
    return now - cfg["window_minutes"] * 60


def seed_if_needed(state, cfg, now, history=journal.history):
    """First run: fill the baseline from journal history so run 1 isn't all 'novel'."""
    if state.baseline_start() is not None:
        return False
    h = history(cfg, now)
    state.set_baseline_start(h["start"])
    state.add_samples(h["samples"])
    state.add_templates(h["templates"])
    return True


def load_baseline(state, cfg, now):
    end_hour = int(window_start(cfg, now) // HOUR)  # current window excluded
    start = state.baseline_start()
    start_hour = end_hour if start is None else int(-(-start // HOUR))  # round up: first full hour
    start_hour = max(start_hour, end_hour - cfg["baseline"]["history_hours"])
    hours = range(start_hour, end_hour)
    counts = {source: [by_hour.get(h, 0) for h in hours]
              for source, by_hour in state.error_samples(start_hour, end_hour).items()}
    return detect.Baseline(len(hours), counts, state.known_templates())


def record_window(state, facts, cfg, now):
    """Add this window's error counts and templates to the baseline."""
    j = detect.ok(facts, "journal")
    if not j:
        return
    hour = int(window_start(cfg, now) // HOUR)
    state.add_samples({(source, hour): c["errors"] for source, c in j["by_source"].items() if c["errors"]})
    state.add_templates({(source, template): (now, now, slot["count"])
                         for source, templates in j.get("error_templates", {}).items()
                         for template, slot in templates.items()})


def run(facts, state, cfg, now):
    baseline = load_baseline(state, cfg, now)
    flags = detect.evaluate(facts, baseline, cfg)
    decision = decide(flags, state.issues(), now, cfg["cooldown"])
    state.apply(decision, now)
    record_window(state, facts, cfg, now)
    state.prune(now, cfg["state"]["sample_retention_days"], cfg["state"]["template_retention_days"])
    return {
        "baseline": {"hours": baseline.hours, "required_hours": cfg["baseline"]["history_hours"],
                     "warming_up": baseline.hours < cfg["baseline"]["history_hours"]},
        "flags": [f.to_dict() for f in flags],
        "report": [{"key": f.key, "reason": r, "severity": f.severity, "summary": f.summary}
                   for f, r in decision.report],
        "suppressed": [{"key": f.key, "reason": r} for f, r in decision.suppressed],
        "resolved": [{"key": row["key"], "summary": row["summary"]} for row in decision.resolved],
    }


# --- scenario fixtures ----------------------------------------------------
# A scenario is a JSON file with a baseline and a sequence of runs, each with
# facts and the expected outcome. Tests check the expectations; the CLI prints
# the outcome so the rules can be demonstrated without touching the server.

SCENARIO_EPOCH = 1_800_000_000  # fixed fake "now" for the first run


def seed_from_scenario(state, cfg, baseline, now):
    end_hour = int(window_start(cfg, now) // HOUR)
    hours = baseline.get("hours", 0)
    state.set_baseline_start((end_hour - hours) * HOUR)
    samples = {}
    for source, counts in baseline.get("error_counts", {}).items():
        for i, c in enumerate(reversed(counts)):  # last value is the most recent hour
            if c:
                samples[(source, end_hour - 1 - i)] = c
    state.add_samples(samples)
    state.add_templates({(s, t): (now, now, 1) for s, t in baseline.get("templates", [])})


def run_scenario(path, cfg):
    scenario = json.loads(open(path).read())
    state = State(":memory:")
    try:
        seed_from_scenario(state, cfg, scenario.get("baseline", {}), SCENARIO_EPOCH)
        results = []
        for step in scenario["runs"]:
            now = SCENARIO_EPOCH + step.get("minutes", 0) * 60
            facts = {"window_minutes": cfg["window_minutes"], **step["facts"]}
            results.append({"minutes": step.get("minutes", 0), **run(facts, state, cfg, now)})
        return scenario, results
    finally:
        state.close()
