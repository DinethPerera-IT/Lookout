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


def flags_with(summary):
    return [{"severity": "warn", "summary": summary, "evidence": []}]


def one(text):
    return json.dumps({"summary": text, "alerts": [{"id": "F1", "explanation": text}]})


@pytest.mark.parametrize("summary, text", [
    ("tailscaled.service: 9 errors in the last 60 min", "Tailscale reported errors."),
    ("Container uptime-kuma restarted since the last check", "Kuma restarted."),
    ("cloudflared.service: 30 errors in the last 60 min (normal: median 1/h, threshold 5)",
     "30 errors in one hour, above the threshold of 5 errors."),
    ("caddy.service: an error never seen before (3x in the last 60 min)", "It happened three times in 60 minutes."),
    ("15-min load average 8.50 (highest in the window; warn at 6, crit at 8)", "The 15-minute load average is 8.50."),
    ("Disk / is 96.2% full (warn at 85%, crit at 95%)", "Disks like this can fill up when logs grow quickly."),
    ("Disk / is 96.2% full (warn at 85%, crit at 95%)", "This can happen when the disk is full of old backups."),
])
def test_correct_paraphrases_pass(summary, text):
    assert llm.validate(one(text), flags_with(summary))[1] == []


@pytest.mark.parametrize("summary, text, problem", [
    ("Prometheus target node-exporter (localhost:9100) is down", "node-exporter might also be down.", "hedges a fact"),
    ("systemd unit caddy.service has failed (failed/failed)", "caddy.service may have failed.", "hedges a fact"),
    ("Container commoncents is stuck restarting (Restarting (1) 4 seconds ago)",
     "It has restarted five times.", "quantities not in the alerts"),
    ("caddy.service: an error never seen before (3x in the last 60 min)", "It happened 3 times in 4 hours.",
     "quantities not in the alerts"),
    ("Disk / is 96.2% full (warn at 85%, crit at 95%)", "The disk is 90% full.", "quantities not in the alerts"),
])
def test_softened_or_misquoted_facts_fail(summary, text, problem):
    parsed, problems = llm.validate(one(text), flags_with(summary))
    assert parsed is None and any(problem in p for p in problems)


# --- explain(): the live call, with a fake Ollama ---------------------------
import io
import urllib.error

ENTRIES = [{"key": "failed_unit:caddy.service", "severity": "crit",
            "summary": "systemd unit caddy.service has failed (failed/failed)", "evidence": []}]


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ollama(content=None, raise_=None, seen=None):
    def opener(req, timeout):
        if seen is not None:
            seen.append((json.loads(req.data), timeout))
        if raise_:
            raise raise_
        return FakeResponse(json.dumps({"message": {"content": content}}).encode())
    return opener


def test_explain_maps_answers_back_to_flag_keys(cfg):
    seen = []
    content = json.dumps({"summary": "Caddy stopped.", "alerts": [{"id": "F1", "explanation": "caddy.service stopped."}]})
    ai = llm.explain(cfg, ENTRIES, opener=ollama(content, seen=seen))
    assert ai == {"ok": True, "model": "llama3.2:3b", "summary": "Caddy stopped.",
                  "alerts": [("failed_unit:caddy.service", "caddy.service stopped.")]}
    body, timeout = seen[0]
    assert body["think"] is False and body["format"] == llm.SCHEMA and body["model"] == "llama3.2:3b"
    assert body["options"]["temperature"] == 0 and timeout == 600


@pytest.mark.parametrize("content, raise_, reason", [
    ("garbage", None, "answer failed checks (not valid JSON)"),
    (json.dumps({"summary": "This is harmless, @everyone.", "alerts": [{"id": "F1", "explanation": "x"}]}), None,
     "answer failed checks (dismissive wording, repeats untrusted content)"),
    (None, TimeoutError(), "no answer within 600 s"),
    (None, urllib.error.URLError(TimeoutError()), "no answer within 600 s"),
    (None, urllib.error.URLError(ConnectionRefusedError()), "Ollama unreachable"),
    (None, urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO()), "Ollama returned HTTP 500"),
    (None, RuntimeError("weird"), "unexpected error (RuntimeError)"),
])
def test_explain_failures_become_code_written_reasons(cfg, content, raise_, reason):
    ai = llm.explain(cfg, ENTRIES, opener=ollama(content, raise_))
    assert ai == {"ok": False, "reason": reason}
    assert "@" not in ai["reason"] and "harmless" not in ai["reason"]   # nothing model-written leaks out


def test_explain_skips_too_many_alerts(cfg):
    assert llm.explain(cfg, ENTRIES * 9, opener=ollama("unused"))["reason"] == "more than 8 alerts"


def test_sanitize_strips_links_and_control_characters():
    assert llm.sanitize("See [the docs](http://x.example) or www.y.example now") == "See the docs or [link removed] now"
    assert llm.sanitize("bell\x07here") == "bell here"
