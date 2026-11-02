import io
import json
import urllib.error

import pytest

from lookout import config, notify

WEBHOOK = "https://discord.com/api/webhooks/1/secret-token"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b""):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


def test_discord_request_shape():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return FakeResponse(b"{}")

    notify.send_discord(WEBHOOK, {"content": "hi"}, opener=opener)
    req = seen[0]
    assert req.get_method() == "POST" and req.full_url == WEBHOOK + "?wait=true"
    assert req.get_header("User-agent").startswith("Lookout")
    assert json.loads(req.data) == {"content": "hi"}


def test_errors_never_contain_the_secret_url():
    def opener(req, timeout):
        raise http_error(404)

    with pytest.raises(notify.NotifyError) as err:
        notify.send_discord(WEBHOOK, {}, opener=opener)
    assert str(err.value) == "Discord webhook failed: HTTP 404"
    assert err.value.__cause__ is None and err.value.__suppress_context__  # no chained traceback with the URL


def test_rate_limit_is_retried_once():
    calls, waits = [], []

    def opener(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise http_error(429, b'{"retry_after": 0.5}')
        return FakeResponse(b"{}")

    notify.send_discord(WEBHOOK, {}, opener=opener, sleep=waits.append)
    assert len(calls) == 2 and waits == [0.5]


def test_unreachable_discord():
    def opener(req, timeout):
        raise urllib.error.URLError(ConnectionRefusedError())

    with pytest.raises(notify.NotifyError, match="unreachable: ConnectionRefusedError"):
        notify.send_discord(WEBHOOK, {}, opener=opener)


def test_kuma_url_replaces_the_default_query():
    url = notify.kuma_url("http://localhost:3001/api/push/abc?status=up&msg=OK&ping=", "down", "x y", 1234.5)
    assert url == "http://localhost:3001/api/push/abc?status=down&msg=x+y&ping=1234"


def test_env_file_parsing(tmp_path, monkeypatch):
    for name in ("A", "B", "C", "D"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("D", "from-systemd")
    env = tmp_path / ".env"
    env.write_text('# comment\nA="http://x/push/t?status=up&msg=OK"\nexport B=plain\nC=\'single\'\nD=from-file\n')
    config.load_env_file(env)
    import os
    assert os.environ["A"] == "http://x/push/t?status=up&msg=OK"
    assert os.environ["B"] == "plain" and os.environ["C"] == "single"
    assert os.environ["D"] == "from-systemd"  # the real environment wins


def test_placeholder_secrets_count_as_missing(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/CHANGE_ME/CHANGE_ME")
    monkeypatch.delenv("KUMA_PUSH_URL", raising=False)
    assert config.missing_secrets() == ["DISCORD_WEBHOOK_URL", "KUMA_PUSH_URL"]


def test_send_returns_the_message_id_and_edit_patches_that_message():
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return FakeResponse(b'{"id": "1199887766554433", "channel_id": "1"}')

    assert notify.send_discord(WEBHOOK + "?thread_id=42", {"content": "facts"}, opener=opener) == "1199887766554433"
    assert seen[0].full_url == WEBHOOK + "?thread_id=42&wait=true"
    notify.edit_discord(WEBHOOK + "?thread_id=42", "1199887766554433", {"content": "facts + AI"}, opener=opener)
    assert seen[1].get_method() == "PATCH"
    assert seen[1].full_url == WEBHOOK + "/messages/1199887766554433?thread_id=42"


def test_odd_responses_give_no_id_and_edit_refuses_a_bad_id():
    for body in (b"", b"not json", b'{"id": "../../x"}', b"[]"):
        assert notify.send_discord(WEBHOOK, {}, opener=lambda req, timeout: FakeResponse(body)) is None
    with pytest.raises(notify.NotifyError):
        notify.edit_discord(WEBHOOK, "../../evil", {}, opener=lambda req, timeout: FakeResponse(b"{}"))


def test_edit_errors_never_contain_the_secret_url():
    def opener(req, timeout):
        raise http_error(404)

    with pytest.raises(notify.NotifyError) as e:
        notify.edit_discord(WEBHOOK, "123", {}, opener=opener)
    assert "secret-token" not in str(e.value) and "404" in str(e.value)
