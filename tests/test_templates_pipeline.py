from ops_summary import pipeline
from ops_summary.collectors import journal
from ops_summary.state import HOUR, State
from ops_summary.templates import normalize_source, template_of


def test_variants_share_a_template():
    a = template_of("Failed to connect to 10.0.0.5:7844 after 3021ms")
    b = template_of("Failed to connect to 10.0.0.9:7844 after 15ms")
    assert a == b == "Failed to connect to <ip> after <n>ms"


def test_template_placeholders():
    assert template_of("container 3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b exited 137") == "container <uuid> exited <n>"
    assert template_of("addr fe80::1ff:fe23:4567:890a dropped") == "addr <ip> dropped"
    assert template_of("at 12:30:45 token=<redacted>") == "at <n>:<n>:<n> token=<redacted>"


def test_per_session_units_collapse():
    assert normalize_source("session-648.scope") == normalize_source("session-12.scope") == "session-<n>.scope"
    assert normalize_source("cron.service") == "cron.service"


def test_seed_runs_only_once(cfg):
    calls = []

    def fake_history(cfg, now):
        calls.append(now)
        return {"start": now - 200 * HOUR, "samples": {}, "templates": {}}

    s = State(":memory:")
    assert pipeline.seed_if_needed(s, cfg, 10_000 * HOUR, fake_history) is True
    assert pipeline.seed_if_needed(s, cfg, 10_001 * HOUR, fake_history) is False
    assert len(calls) == 1
    s.close()


def test_baseline_is_zero_filled_and_excludes_current_window(cfg):
    s = State(":memory:")
    now = 10_000 * HOUR + 30 * 60  # window starts at hour 9_999 + 30 min
    s.set_baseline_start(0)
    s.add_samples({("a", 9_998): 4, ("a", 9_999): 99})  # 9_999 is the current window's bucket
    b = pipeline.load_baseline(s, cfg, now)
    assert b.hours == 168 and len(b.error_counts["a"]) == 168
    assert b.error_counts["a"][-1] == 4 and sum(b.error_counts["a"]) == 4
    s.close()


def test_history_buckets_hourly_and_skips_noise(cfg, monkeypatch):
    now = 10_000 * HOUR
    entries = [
        {"_SYSTEMD_UNIT": "app.service", "PRIORITY": "3", "MESSAGE": "boom 1",
         "__REALTIME_TIMESTAMP": str(int((now - 2 * HOUR) * 1e6))},
        {"_SYSTEMD_UNIT": "app.service", "PRIORITY": "3", "MESSAGE": "boom 2",
         "__REALTIME_TIMESTAMP": str(int((now - 2 * HOUR + 60) * 1e6))},
        {"_SYSTEMD_UNIT": "apt-daily.service", "PRIORITY": "3",
         "MESSAGE": "Timeout occurred while waiting for network connectivity.",
         "__REALTIME_TIMESTAMP": str(int((now - 3 * HOUR) * 1e6))},
    ]
    monkeypatch.setattr(journal, "read", lambda *a, **k: entries)
    monkeypatch.setattr(journal, "oldest_timestamp", lambda: 0)
    h = journal.history(cfg, now)
    assert h["samples"] == {("app.service", 9_998): 2}
    assert list(h["templates"]) == [("app.service", "boom <n>")]
    assert h["start"] == (9_999 - 168) * HOUR  # aligned to a full hour
