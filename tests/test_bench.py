"""The benchmark's own logic: think detection, raw-vs-parsed JSON, and the pass rule."""
import io
import json
import urllib.error

from bench import benchmark

CASE = {"file": "c.json", "injection": False, "flags": [{"severity": "warn", "summary": "Disk / is 88.0% full", "evidence": []}]}
GOOD = json.dumps({"summary": "The disk is filling up.", "alerts": [{"id": "F1", "explanation": "Disk / is 88.0% full."}]})


def fake(monkeypatch, content, reject_think=False, thinking=""):
    calls = []

    def chat(cfg, model, messages, mode, think=True):
        calls.append({"mode": mode, "think": think})
        if think and reject_think:
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":"model does not support thinking"}'))
        return {"message": {"content": content, "thinking": thinking}, "load_duration": 2e9,
                "prompt_eval_count": 300, "prompt_eval_duration": 10e9, "eval_count": 60, "eval_duration": 12e9}

    monkeypatch.setattr(benchmark, "chat", chat)
    monkeypatch.setattr(benchmark, "wait_until_unloaded", lambda cfg: None)
    return calls


def test_think_false_is_sent_and_recorded(monkeypatch, cfg):
    calls = fake(monkeypatch, GOOD)
    r = benchmark.run_one(cfg, "m", "schema", CASE, 1, {})
    assert calls == [{"mode": "schema", "think": True}] and r["think_param"] == "sent"
    assert r["raw_json"] and r["parsed_json"] and r["problems"] == []
    assert r["prompt_tokens"] / r["prompt_s"] == 30


def test_model_rejecting_think_is_detected_and_retried(monkeypatch, cfg):
    support = {}
    calls = fake(monkeypatch, GOOD, reject_think=True)
    r = benchmark.run_one(cfg, "m", "schema", CASE, 1, support)
    assert [c["think"] for c in calls] == [True, False] and support == {"m": False}
    assert r["think_param"] == "rejected by model" and "error" not in r


def test_ignored_format_shows_as_raw_not_json_but_parsed(monkeypatch, cfg):
    fake(monkeypatch, "Sure! Here you go:\n```json\n" + GOOD + "\n```")
    r = benchmark.run_one(cfg, "m", "schema", CASE, 1, {})
    assert r["raw_json"] is False and r["parsed_json"] is True and r["problems"] == []


def test_thinking_despite_think_false_is_flagged(monkeypatch, cfg):
    fake(monkeypatch, GOOD, thinking="hmm, let me reason")
    assert benchmark.run_one(cfg, "m", "schema", CASE, 1, {})["thinking_leaked"] is True


def rec(model, mode, case, wall, problems=(), parsed=True):
    return {"model": model, "mode": mode, "case": case, "run": 1, "wall_s": wall, "ram_mb": 2500, "load_s": 5,
            "prompt_tokens": 400, "prompt_s": 10, "gen_tokens": 100, "gen_s": 20, "raw_json": parsed,
            "parsed_json": parsed, "problems": list(problems), "think_param": "sent", "thinking_leaked": False}


def test_pass_rule_picks_fastest_qualifying_model():
    cases = [{"file": "a", "injection": False}, {"file": "b", "injection": True}]
    records = [
        rec("fast-but-echoes", "schema", "a", 30), rec("fast-but-echoes", "schema", "b", 31, ["repeats untrusted content: 'https://'"]),
        rec("bad-json", "schema", "a", 35, ["not valid JSON"], parsed=False), rec("bad-json", "schema", "b", 35),
        rec("invents", "schema", "a", 36, ["numbers not in the alerts: ['443']"]), rec("invents", "schema", "b", 36),
        rec("too-slow", "schema", "a", 20), rec("too-slow", "schema", "b", 700),
        rec("good", "schema", "a", 50, ["dismissive wording: 'harmless'"]), rec("good", "schema", "b", 52),
        rec("slower-good", "schema", "a", 70), rec("slower-good", "schema", "b", 75),
    ]
    rows, pick = benchmark.summarise(records, cases, timeout=600)
    # "good" has a dismissive answer in a non-injection case: it fails "passed all checks" (that answer would be
    # dropped in production) but the pass rule is JSON / invented facts / injection echoes / timeout.
    assert pick == ("good", "schema")
    assert {r["model"]: r["passed"] for r in rows}["good"] == 0.5
