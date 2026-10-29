"""Every scenario fixture states, run by run, what must be reported, suppressed and resolved."""
from pathlib import Path

import pytest

from ops_summary.pipeline import run_scenario

SCENARIOS = sorted((Path(__file__).parent / "fixtures" / "scenarios").glob("*.json"))


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_scenario(path, cfg):
    scenario, results = run_scenario(path, cfg)
    for step, result in zip(scenario["runs"], results):
        expect = step["expect"]
        at = f"{path.stem} @ {step['minutes']} min"
        assert {r["key"]: r["reason"] for r in result["report"]} == expect.get("report", {}), at
        assert sorted(r["key"] for r in result["suppressed"]) == sorted(expect.get("suppressed", [])), at
        assert sorted(r["key"] for r in result["resolved"]) == sorted(expect.get("resolved", [])), at


def test_warmup_is_visible_in_the_summary(cfg):
    _, results = run_scenario(Path(__file__).parent / "fixtures" / "scenarios" / "warmup.json", cfg)
    assert results[1]["baseline"]["warming_up"] is True
    assert "baseline warming up: 11/168 h" in results[1]["report"][0]["summary"]


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.stem)
def test_never_last_said_resolved_while_broken(path, cfg):
    """Across every scenario: if an issue is flagged now, the last message about it isn't "resolved"."""
    _, results = run_scenario(path, cfg)
    last_said = {}
    for result in results:
        last_said.update({r["key"]: "problem" for r in result["report"]})
        last_said.update({r["key"]: "resolved" for r in result["resolved"]})
        for flag in result["flags"]:
            if flag["kind"] == "state":
                assert last_said.get(flag["key"]) == "problem", f"{path.stem} @ {result['minutes']}: {flag['key']}"
