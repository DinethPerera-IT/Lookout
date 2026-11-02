"""Re-grade the real answers saved by the phase 6 benchmark with the current validator.

These are actual model outputs, so they pin down what the validator must catch
and, just as important, what it must let through.
"""
import json
from pathlib import Path

from lookout import llm

ROOT = Path(__file__).parent.parent
RESULTS = json.loads((ROOT / "bench" / "results" / "2026-10-29-1307.json").read_text())
CASES = {p.name: json.loads(p.read_text()) for p in (ROOT / "bench" / "cases").glob("*.json")}


def verdicts():
    return {(r["model"], r["case"]): llm.validate(r["output"], CASES[r["case"]]["flags"])[1]
            for r in RESULTS["records"]}


def test_every_llama_answer_still_passes():
    v = verdicts()
    assert all(v[k] == [] for k in v if k[0] == "llama3.2:3b")


def test_problems_found_by_reading_the_answers_are_caught():
    v = verdicts()
    assert any("hedges a fact" in p for p in v[("phi4-mini", "6_multi.json")])               # "might also be down"
    assert any("quantities not in" in p for p in v[("phi4-mini", "2_restart_loop.json")])    # "five times"
    assert any("repeats untrusted" in p for p in v[("qwen3.5:4b", "7_injection_caddy.json")])  # "@everyone"


def test_alias_false_positive_is_gone():
    assert verdicts()[("qwen3.5:4b", "4_wifi_flapping.json")] == []                          # "Tailscale"
