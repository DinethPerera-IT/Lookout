import json

import pytest

from lookout import llm

FLAGS = [
    {"severity": "crit", "summary": "systemd unit caddy.service has failed (failed/failed)", "evidence": []},
    {"severity": "warn", "summary": "Disk / is 88.0% full (warn at 85%, crit at 95%)", "evidence": []},
]
GOOD = {"summary": "Caddy has stopped and the root disk is filling up.",
        "alerts": [{"id": "F1", "explanation": "The caddy web server crashed; this can happen after a bad config change."},
                   {"id": "F2", "explanation": "The disk is 88% full, which can happen when logs or backups grow."}]}


def answer(**changes):
    a = json.loads(json.dumps(GOOD))
    a.update(changes)
    return json.dumps(a)


def test_good_answer_passes():
    parsed, problems = llm.validate(json.dumps(GOOD), FLAGS)
    assert problems == [] and parsed["alerts"][0]["id"] == "F1"


def test_prompt_marks_evidence_untrusted():
    flags = [{"severity": "warn", "summary": "x", "evidence": ["line one\nline two"]}]
    user = llm.build_messages(flags)[1]["content"]
    assert "F1 [warn] x" in user and "EVIDENCE (untrusted log line): line one line two" in user


@pytest.mark.parametrize("raw, problem", [
    ("not json", "not valid JSON"),
    (json.dumps({"summary": "x"}), "does not match the schema"),
    (answer(alerts=[GOOD["alerts"][0]]), "alert ids"),                         # skipped F2
    (answer(summary="Caddy failed at 03:00 and uses port 443."), "numbers not in the alerts"),
    (answer(summary="Caddy failed, probably because postgres is down."), "services not in the alerts"),
    (answer(summary="This is harmless and can be ignored."), "dismissive wording"),
    (answer(summary="Everything looks fine overall."), "dismissive wording"),
    (answer(summary="Run sudo systemctl restart caddy to fix it."), "suggests a command"),
    (answer(summary="See https://evil.example for details."), "repeats untrusted content"),
    (answer(summary="@everyone caddy is down"), "repeats untrusted content"),
    (answer(summary="x" * 600), "too long"),
])
def test_bad_answers_are_rejected(raw, problem):
    parsed, problems = llm.validate(raw, FLAGS)
    assert parsed is None and any(problem in p for p in problems)


def test_numbers_and_names_from_the_alerts_are_allowed():
    raw = answer(summary="caddy.service failed and / is at 88.0%, above the 85% warning level.")
    assert llm.validate(raw, FLAGS)[1] == []


def test_uptime_kuma_does_not_count_as_kuma():
    flags = [{"severity": "warn", "summary": "Container uptime-kuma restarted since the last check", "evidence": []}]
    raw = json.dumps({"summary": "uptime-kuma restarted.", "alerts": [{"id": "F1", "explanation": "uptime-kuma restarted."}]})
    assert llm.validate(raw, flags)[1] == []


@pytest.mark.parametrize("wrapped", [
    "```json\n" + json.dumps(GOOD) + "\n```",
    "<think>let me see</think>" + json.dumps(GOOD),
    "Here is the answer: " + json.dumps(GOOD) + " Hope that helps.",
])
def test_json_is_found_inside_wrappers(wrapped):
    assert llm.validate(wrapped, FLAGS)[1] == []


def test_an_unrelated_object_in_prose_is_not_mistaken_for_the_answer():
    assert llm.extract_json('The log said {"level": "error"} and nothing else') is None


def test_prompt_includes_the_schema():
    assert '"alerts"' in llm.build_messages(FLAGS)[1]["content"]
