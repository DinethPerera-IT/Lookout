"""The full run: send first, commit state only after delivery, heartbeat at the end."""
import pytest

from lookout import __main__ as cli
from lookout import notify
from lookout.llm import explain as REAL_EXPLAIN  # imported before any test patches it

FAILED = {"ok": True, "failed_units": [{"unit": "caddy.service", "active": "failed", "sub": "failed",
                                         "description": "Caddy"}]}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/t")
    monkeypatch.setenv("KUMA_PUSH_URL", "http://kuma/api/push/t")
    h = {"sent": [], "urls": [], "edits": [], "events": [], "fail_edit": False, "pushes": [], "fail_send": False, "units": FAILED}

    def send(url, payload):
        if h["fail_send"]:
            raise notify.NotifyError("Discord webhook failed: HTTP 500")
        h["sent"].append(payload)
        h["urls"].append(url)
        h["events"].append("send")
        return "1234567890"

    def edit_msg(url, message_id, payload):
        if h["fail_edit"]:
            raise notify.NotifyError("Discord edit failed: HTTP 404")
        h["edits"].append((url, message_id, payload))
        h["events"].append("edit")

    monkeypatch.setattr(notify, "send_discord", send)
    monkeypatch.setattr(notify, "edit_discord", edit_msg)

    def push(url, status, msg, ping_ms=None):
        h["pushes"].append((status, msg))
        h["events"].append("push")

    monkeypatch.setattr(notify, "push_kuma", push)
    monkeypatch.setattr(cli, "COLLECTORS", {"systemd": lambda cfg: h["units"]})
    h["ai"] = {"ok": True, "model": "fake", "summary": "Caddy stopped.",
               "alerts": [("failed_unit:caddy.service", "The web server stopped.")]}
    h["ai_calls"] = 0

    def fake_explain(cfg, entries):
        h["ai_calls"] += 1
        h["events"].append("llm")
        return h["ai"]

    monkeypatch.setattr(cli.llm, "explain", fake_explain)
    monkeypatch.setattr(cli.pipeline, "seed_if_needed", lambda *a, **k: False)
    monkeypatch.setattr(cli.crash, "unit_evidence", lambda cfg, unit, facts, now: {
        "target": {"kind": "unit", "name": unit}, "result": "exit-code", "exit_code": 1, "failed_at": now - 30,
        "log_lines": ["Error: listen tcp :443: bind: address already in use"], "kernel_oom": [], "host": {}})
    h["argv"] = ["--state", str(tmp_path / "state.sqlite")]
    return h


def test_report_sent_once_then_suppressed(harness):
    assert cli.main(harness["argv"]) == 0
    assert len(harness["sent"]) == 1 and harness["pushes"][-1][0] == "up"
    assert cli.main(harness["argv"]) == 0
    assert len(harness["sent"]) == 1                      # suppressed: no second message
    assert harness["pushes"][-1] == ("up", "OK · 0 reported, 1 suppressed, 0 resolved")


def test_failed_send_marks_nothing_and_alerts_kuma(harness):
    harness["fail_send"] = True
    assert cli.main(harness["argv"]) == 1
    assert harness["pushes"] == [("down", "Lookout could not deliver: Discord webhook failed: HTTP 500")]
    harness["fail_send"] = False
    assert cli.main(harness["argv"]) == 0
    assert len(harness["sent"]) == 1                      # retried and delivered on the next run


def test_quiet_run_sends_nothing_but_still_heartbeats(harness):
    harness["units"] = {"ok": True, "failed_units": []}
    assert cli.main(harness["argv"]) == 0
    assert harness["sent"] == [] and harness["pushes"] == [("up", "OK · 0 reported, 0 suppressed, 0 resolved")]


def test_dry_run_sends_and_pushes_nothing(harness, capsys):
    assert cli.main(harness["argv"] + ["--dry-run"]) == 0
    assert harness["sent"] == [] and harness["pushes"] == []
    assert '"discord_payload"' in capsys.readouterr().out


def test_missing_secrets_stop_a_real_run(harness, monkeypatch):
    monkeypatch.delenv("KUMA_PUSH_URL")
    assert cli.main(harness["argv"]) == 2 and harness["sent"] == []


def test_send_flag_requires_fixture(harness):
    with pytest.raises(SystemExit):
        cli.main(["--send"])


def test_facts_go_out_before_the_model_runs(harness):
    """The core principle, in time: the AI can never delay a deterministic alert."""
    assert cli.main(harness["argv"]) == 0
    assert harness["events"] == ["send", "push", "llm", "edit"]
    assert [e["title"][:1] for e in harness["sent"][0]["embeds"]] == ["🔴"]          # facts only


def test_ai_explanation_is_added_below_the_facts_by_editing_the_message(harness):
    assert cli.main(harness["argv"]) == 0
    url, message_id, payload = harness["edits"][0]
    assert message_id == "1234567890" and url == harness["urls"][0]
    titles = [e["title"] for e in payload["embeds"]]
    assert titles[0].startswith("🔴") and titles[1].startswith("🤖 AI explanation · unverified")
    assert payload["embeds"][0]["description"] == harness["sent"][0]["embeds"][0]["description"]  # facts unchanged


def test_ai_failure_edits_in_the_reason(harness):
    harness["ai"] = {"ok": False, "reason": "no answer within 600 s"}
    assert cli.main(harness["argv"]) == 0
    [embed] = harness["edits"][0][2]["embeds"]
    assert "AI explanation unavailable: no answer within 600 s" in embed["footer"]["text"]
    assert harness["pushes"][-1] == ("up", "OK · 1 reported, 0 suppressed, 0 resolved")


def test_a_failed_edit_is_not_a_failed_run(harness):
    harness["fail_edit"] = True
    assert cli.main(harness["argv"]) == 0 and len(harness["sent"]) == 1


def test_dashboard_sees_pending_then_the_explanation(harness, monkeypatch):
    seen = {}

    def fake_explain(cfg, entries):
        import sqlite3
        conn = sqlite3.connect(harness["argv"][1])            # another reader, mid-run
        seen["during"] = conn.execute("SELECT ai_status FROM incidents").fetchone()[0]
        conn.close()
        return harness["ai"]

    monkeypatch.setattr(cli.llm, "explain", fake_explain)
    assert cli.main(harness["argv"]) == 0
    import sqlite3
    conn = sqlite3.connect(harness["argv"][1])
    assert seen["during"] == "pending"                         # incident visible before the model is done
    assert conn.execute("SELECT ai_status, ai_explanation FROM incidents").fetchone() == ("ok", "The web server stopped.")


def test_ai_is_not_called_when_nothing_is_reported(harness):
    harness["units"] = {"ok": True, "failed_units": []}
    cli.main(harness["argv"])
    assert harness["ai_calls"] == 0


def test_no_llm_flag_skips_the_model(harness):
    cli.main(harness["argv"] + ["--no-llm"])
    assert harness["ai_calls"] == 0 and len(harness["sent"][0]["embeds"]) == 1 and harness["edits"] == []


def test_dead_ollama_port_means_facts_only(harness, monkeypatch):
    from lookout import config
    real_load = config.load

    def load(path=None):
        cfg = real_load(path)
        cfg["llm"]["url"] = "http://127.0.0.1:1"   # nothing listens here
        return cfg

    monkeypatch.setattr(cli.config, "load", load)
    monkeypatch.setattr(cli.llm, "explain", REAL_EXPLAIN)
    assert cli.main(harness["argv"]) == 0
    [embed] = harness["edits"][0][2]["embeds"]
    assert "AI explanation unavailable: Ollama unreachable" in embed["footer"]["text"]


REAL, SHADOW = "https://discord.com/api/webhooks/1/t", "https://discord.com/api/webhooks/2/shadow"


def test_shadow_mode_uses_only_the_shadow_channel_and_commits_state(harness, monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_SHADOW_WEBHOOK_URL", SHADOW)
    assert cli.main(harness["argv"] + ["--shadow"]) == 0
    assert harness["urls"] == [SHADOW]                      # never the real webhook
    assert harness["sent"][0]["embeds"][0]["title"].startswith("👻 SHADOW · ")
    assert "shadow report:" in capsys.readouterr().out      # also in the journal
    assert harness["pushes"][-1] == ("up", "SHADOW · OK · 1 reported, 0 suppressed, 0 resolved")
    assert [u for u, _, _ in harness["edits"]] == [SHADOW]          # the AI edit also goes to the shadow channel
    assert cli.main(harness["argv"] + ["--shadow"]) == 0
    assert harness["urls"] == [SHADOW]                      # state was kept: second run suppressed


def test_shadow_mode_without_a_channel_logs_only(harness, monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")               # not needed in shadow mode
    monkeypatch.delenv("DISCORD_SHADOW_WEBHOOK_URL", raising=False)
    assert cli.main(harness["argv"] + ["--shadow"]) == 0
    out = capsys.readouterr().out
    assert harness["sent"] == [] and "shadow report:" in out and "(report logged only)" in out
    assert harness["pushes"][-1][0] == "up"


def test_shadow_mode_still_needs_the_heartbeat(harness, monkeypatch):
    monkeypatch.delenv("KUMA_PUSH_URL")
    assert cli.main(harness["argv"] + ["--shadow"]) == 2


def test_failed_shadow_send_is_a_failed_run(harness, monkeypatch):
    monkeypatch.setenv("DISCORD_SHADOW_WEBHOOK_URL", SHADOW)
    harness["fail_send"] = True
    assert cli.main(harness["argv"] + ["--shadow"]) == 1
    assert harness["pushes"][-1][0] == "down"               # same guarantees as live mode


def test_event_hints_and_trigger_are_recorded(harness):
    import sqlite3
    assert cli.main(harness["argv"] + ["--trigger", "systemd OnFailure caddy.service"]) == 0
    conn = sqlite3.connect(harness["argv"][1])
    assert conn.execute("SELECT trigger FROM runs").fetchone()[0] == "systemd OnFailure caddy.service"
    ttd = conn.execute("SELECT time_to_detect_s FROM incidents").fetchone()[0]
    assert 29 <= ttd <= 31                                     # stub: unit failed 30 s before detection
