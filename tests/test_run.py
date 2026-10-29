"""The full run: send first, commit state only after delivery, heartbeat at the end."""
import pytest

from lookout import __main__ as cli
from lookout import notify

FAILED = {"ok": True, "failed_units": [{"unit": "caddy.service", "active": "failed", "sub": "failed",
                                         "description": "Caddy"}]}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/t")
    monkeypatch.setenv("KUMA_PUSH_URL", "http://kuma/api/push/t")
    h = {"sent": [], "pushes": [], "fail_send": False, "units": FAILED}

    def send(url, payload):
        if h["fail_send"]:
            raise notify.NotifyError("Discord webhook failed: HTTP 500")
        h["sent"].append(payload)

    monkeypatch.setattr(notify, "send_discord", send)
    monkeypatch.setattr(notify, "push_kuma", lambda url, status, msg, ping_ms=None: h["pushes"].append((status, msg)))
    monkeypatch.setattr(cli, "COLLECTORS", {"systemd": lambda cfg: h["units"]})
    monkeypatch.setattr(cli.pipeline, "seed_if_needed", lambda *a, **k: False)
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
