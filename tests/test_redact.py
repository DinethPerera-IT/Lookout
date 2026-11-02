import pytest

from lookout.collectors import journal
from lookout.redact import find_leaks, redact, redact_counts

# Fake secrets are assembled at runtime so no realistic-looking token appears
# literally in the repo (GitHub secret scanning would flag or block the push).
J = "".join
FAKE_JWT = J(["eyJhbGciOiJIUzI1NiJ9", ".", "eyJzdWIiOiJ0ZXN0In0", ".", "c2lnbmF0dXJlX2Zha2U"])
FAKE_WEBHOOK = J(["https://discord.com/api/webhooks/", "123456789012345678", "/", "fake-Token_value"])
FAKE_GH = J(["ghp_", "A1b2" * 9])
FAKE_BOT = J(["MTIzNDU2Nzg5MDEyMzQ1Njc4", ".", "GaBcDe", ".", "x" * 30 + "1234"])
FAKE_KEY = J(["-----BEGIN OPENSSH PRIVATE KEY-----\n", "b3BlbnNzaC1rZXk=\n", "-----END OPENSSH PRIVATE KEY-----"])


@pytest.mark.parametrize("raw, expected", [
    ("login password=hunter2 ok", "login password=<redacted> ok"),
    ('{"db_password": "s3cr3t!"}', '{"db_password": "<redacted>"}'),
    ("API_KEY=abc123 next", "API_KEY=<redacted> next"),
    ("client_secret: 'xyz'", "client_secret: '<redacted>'"),
    ("GET /?token=abc&page=2", "GET /?token=<redacted>&page=2"),
    ("GET /cb?code=abc123&x=1", "GET /cb?code=<redacted>&x=1"),
    ("Authorization: Bearer abcdefgh12345678", "Authorization: <redacted>"),
    ("sent bearer abcdefgh12345678 upstream", "sent bearer <redacted> upstream"),
    ("cookie: session=abc; theme=dark", "cookie: <redacted>"),
    ("dsn postgres://app:pa55word@db:5432/hr", "dsn postgres://<redacted>@db:5432/hr"),
    (f"jwt {FAKE_JWT} end", "jwt <jwt> end"),
    (f"posting to {FAKE_WEBHOOK}", "posting to <discord-webhook>"),
    ("push http://localhost:3001/api/push/AbC123xyz?status=up",
     "push http://localhost:3001/api/push/<redacted>?status=up"),
    (f"token leaked {FAKE_GH}", "token leaked <token>"),
    (f"bot {FAKE_BOT} ready", "bot <token> ready"),
    (f"key:\n{FAKE_KEY}\nafter", "key:\n<private-key>\nafter"),
    ("mail from someone@example.com failed", "mail from <email> failed"),
    ("wlan0: Trying to associate with aa:bb:cc:dd:ee:ff (SSID='My Home 12 Street' freq=5220 MHz)",
     "wlan0: Trying to associate with <mac> (SSID=<ssid> freq=5220 MHz)"),
    ("wlan0: Connected WiFi access point: My Home 12 Street (aa:bb:cc:dd:ee:ff)",
     "wlan0: Connected WiFi access point: <ssid> (<mac>)"),
    ("wlan0: associated with SSID 'Flat 12 Smith St'", "wlan0: associated with SSID <ssid>"),
    ('scan result: ssid: "Flat 12 Smith St" signal -61', 'scan result: ssid: <ssid> signal -61'),
    ("CTRL-EVENT-DISCONNECTED bssid=AA:BB:CC:DD:EE:FF reason=3",
     "CTRL-EVENT-DISCONNECTED bssid=<mac> reason=3"),
    ("unknown format k9f8s7d6f5g4h3j2k1l0q9w8e7r6t5y4u3 here", "unknown format <token> here"),
    ("tunnel " + J(["eyJhIjoiZmFrZSIsInQi", "/", "OiJmYWtlIiwicyI6Ilpo", "+", "YWJ4"]), "tunnel <blob>"),
])
def test_redacts(raw, expected):
    assert redact(raw) == expected


@pytest.mark.parametrize("line", [
    "Started Daily apt download activities.",
    "RTW: send eapol packet 4/4",
    "Timeout occurred while waiting for network connectivity.",
    "sudo: alice : TTY=pts/1 ; PWD=/home/<user>/ops-summary ; USER=root",
    "Failed password for invalid user admin from 203.0.113.7 port 51234 ssh2",
    "ExecStart=/home/<user>/discord-bot-main/venv/bin/python bot.py",
    "container 3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b restarted",
    "level=info msg=\"Request completed\" status=200 duration=12ms",
])
def test_leaves_normal_lines_alone(line):
    assert redact(line) == line


def test_counts_name_rules_but_never_values():
    _, counts = redact_counts("password=hunter2 user@example.com")
    assert counts == {"key_value": 1, "email": 1}
    assert "hunter2" not in str(counts)


def test_redaction_is_idempotent():
    text = f"Authorization: Bearer abcdefgh12345678 {FAKE_JWT} password=x {FAKE_WEBHOOK}"
    once = redact(text)
    assert redact(once) == once


def test_leak_check_flags_raw_and_passes_redacted():
    raw = "\n".join(["password=hunter2", f"Bearer {FAKE_GH}", FAKE_JWT, FAKE_WEBHOOK,
                     "postgres://u:p@h/db", "nothing to see"])
    assert find_leaks(raw) == 5
    assert find_leaks(redact(raw)) == 0


def test_journal_messages_are_redacted_at_parse_time():
    line = '{"_SYSTEMD_UNIT":"app.service","PRIORITY":"3","MESSAGE":"auth failed token=abc123"}'
    assert journal.parse(line)[0]["MESSAGE"] == "auth failed token=<redacted>"
