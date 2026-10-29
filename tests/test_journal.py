from pathlib import Path

from ops_summary.collectors import journal

SAMPLE = (Path(__file__).parent / "fixtures" / "journal" / "sample.jsonl").read_text()


def test_parse_skips_invalid_lines():
    assert len(journal.parse(SAMPLE)) == 6


def test_summarise_counts_errors_per_source():
    summary = journal.summarise(journal.parse(SAMPLE))
    assert summary["total"] == 6
    assert summary["by_source"] == {
        "CRON": {"total": 1, "errors": 0},
        "cloudflared.service": {"total": 2, "errors": 1},
        "kernel": {"total": 2, "errors": 1},
        "unknown": {"total": 1, "errors": 0},  # priority 4 (warning) is not an error
    }


def test_byte_array_message_is_decoded():
    entry = {"MESSAGE": [82, 84, 87, 58, 32, 255]}
    assert journal.message_of(entry) == "RTW: �"


def test_wifi_reauth_count():
    assert journal.count_matching(journal.parse(SAMPLE), "send eapol packet 4/4") == 1


def test_summary_contains_no_message_text():
    # Until redaction exists (phase 2), log text must never leave the collector.
    assert "failed to connect" not in str(journal.summarise(journal.parse(SAMPLE)))
