from pathlib import Path

from lookout.collectors import journal

SAMPLE = (Path(__file__).parent / "fixtures" / "journal" / "sample.jsonl").read_text()


def test_parse_skips_invalid_lines():
    assert len(journal.parse(SAMPLE)) == 6


def test_summarise_counts_errors_per_source():
    summary = journal.summarise(journal.parse(SAMPLE))
    assert summary["total"] == 6
    assert summary["by_source"] == {
        "CRON": {"total": 1, "errors": 0, "noise": 0},
        "cloudflared.service": {"total": 2, "errors": 1, "noise": 0},
        "kernel": {"total": 2, "errors": 1, "noise": 0},
        "unknown": {"total": 1, "errors": 0, "noise": 0},  # priority 4 (warning) is not an error
    }
    assert summary["error_templates"]["cloudflared.service"] == {
        "failed to connect to edge": {"count": 1, "examples": ["failed to connect to edge"]}}


def test_known_noise_is_counted_separately(cfg):
    line = '{"_SYSTEMD_UNIT":"apt-daily.service","PRIORITY":"3","MESSAGE":"Timeout occurred while waiting for network connectivity."}'
    summary = journal.summarise(journal.parse(line), journal.noise_patterns(cfg))
    assert summary["by_source"]["apt-daily.service"] == {"total": 1, "errors": 0, "noise": 1}
    assert summary["error_templates"] == {}


def test_byte_array_message_is_decoded():
    entry = {"MESSAGE": [82, 84, 87, 58, 32, 255]}
    assert journal.message_of(entry) == "RTW: �"


def test_wifi_reauth_count():
    assert journal.count_matching(journal.parse(SAMPLE), "send eapol packet 4/4") == 1


def test_examples_are_redacted():
    line = '{"_SYSTEMD_UNIT":"app.service","PRIORITY":"3","MESSAGE":"login failed password=hunter2"}'
    summary = journal.summarise(journal.parse(line))
    assert "hunter2" not in str(summary)


VETH_LINES = [  # exactly as logged on this server when a demo container stopped
    'Interface "veth4b94e60" not found.',
    'Interface "veth00421e5" not found.',
    'Interface "vethdab1476" not found.',
    'Interface "veth9d3c036" not found.',
]


def test_docker_veth_interfaces_vanishing_is_known_noise(cfg):
    from lookout.logstats import LogStats
    stats = LogStats(journal.noise_patterns(cfg))
    for source in ("networkd-dispatcher.service", "networkctl"):
        for line in VETH_LINES:
            stats.add(source, line, is_error=True)
    r = stats.result()
    for source in ("networkd-dispatcher.service", "networkctl"):
        assert r["by_source"][source] == {"total": 4, "errors": 0, "noise": 4}
    assert r["error_templates"] == {}                           # so no spike and no "never seen before"


def test_real_interfaces_going_missing_are_still_errors(cfg):
    from lookout.logstats import LogStats
    stats = LogStats(journal.noise_patterns(cfg))
    for line in ('Interface "wlan0" not found.', 'Interface "eth0" not found.', 'Interface "veth-x" not found.',
                 'Interface "vethab12" not found. Also something else broke'):
        stats.add("networkd-dispatcher.service", line, is_error=True)
    assert stats.result()["by_source"]["networkd-dispatcher.service"]["errors"] == 4


def test_every_known_noise_entry_has_a_reason(cfg):
    for entry in cfg["journal"]["known_noise"] + cfg["docker"]["known_noise"]:
        assert entry.get("reason"), entry
