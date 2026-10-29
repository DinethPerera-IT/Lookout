from lookout.detect import Flag
from lookout.state import HOUR, State, decide

COOLDOWN = {"reminder_hours": 24, "event_cooldown_hours": 6, "flap_window_hours": 6}


def row(key, kind="state", severity="warn", last_reported=0, resolved_at=None, notified=0, last_notice=None):
    return {"key": key, "kind": kind, "severity": severity, "summary": key,
            "last_reported": last_reported, "resolved_at": resolved_at, "resolved_notified": notified,
            "last_resolved_notice": last_notice}


def test_events_expire_silently_instead_of_resolving():
    d = decide([], {"e": row("e", kind="event")}, 7 * HOUR, COOLDOWN)
    assert d.resolved == [] and d.forget == ["e"]


def test_event_kept_during_cooldown_so_repeats_stay_quiet():
    d = decide([], {"e": row("e", kind="event")}, 2 * HOUR, COOLDOWN)
    assert d.forget == []


def test_recurrence_after_resolved_notice_is_always_reported():
    stored = {"s": row("s", last_reported=0, resolved_at=HOUR, notified=1, last_notice=HOUR)}
    d = decide([Flag("s", "x", "warn", "s")], stored, HOUR + 60, COOLDOWN)  # 1 min after "resolved"
    assert [(f.key, r) for f, r in d.report] == [("s", "reappeared")]


def test_second_resolved_notice_within_flap_window_is_held_back():
    stored = {"s": row("s", last_reported=2 * HOUR, last_notice=HOUR)}  # active again, notice sent 1 h ago
    d = decide([], stored, 3 * HOUR, COOLDOWN)
    assert d.resolved == [] and [r["key"] for r in d.resolved_silently] == ["s"]


def test_held_back_resolved_notice_sent_after_flap_window():
    stored = {"s": row("s", last_reported=2 * HOUR, resolved_at=3 * HOUR, notified=0, last_notice=HOUR)}
    assert decide([], stored, 5 * HOUR, COOLDOWN).resolved == []
    assert [r["key"] for r in decide([], stored, 7 * HOUR, COOLDOWN).resolved] == ["s"]


def test_severity_increase_reported_even_while_flapping():
    stored = {"s": row("s", last_reported=2 * HOUR, resolved_at=3 * HOUR, notified=0, last_notice=HOUR)}
    d = decide([Flag("s", "x", "crit", "s")], stored, 3 * HOUR + 60, COOLDOWN)
    assert [(f.key, r) for f, r in d.report] == [("s", "worse")]


def test_flapping_back_before_held_notice_stays_quiet():
    stored = {"s": row("s", last_reported=2 * HOUR, resolved_at=3 * HOUR, notified=0, last_notice=HOUR)}
    d = decide([Flag("s", "x", "warn", "s")], stored, 3 * HOUR + 60, COOLDOWN)
    assert d.report == [] and d.suppressed[0][1] == "flapping"


def test_crit_dropping_to_warn_is_not_reported():
    d = decide([Flag("s", "x", "warn", "s")], {"s": row("s", severity="crit")}, HOUR, COOLDOWN)
    assert d.report == [] and d.suppressed[0][1] == "cooldown"


def test_dry_run_changes_are_rolled_back(tmp_path):
    path = tmp_path / "state.sqlite"
    s = State(path)
    s.set_baseline_start(123.0)
    s.commit()
    s.add_samples({("app.service", 1): 5})
    s.close()  # no commit: like a dry run
    s = State(path)
    assert s.baseline_start() == 123.0
    assert s.error_samples(0, 10) == {}
    s.close()


def test_apply_round_trip(tmp_path):
    s = State(tmp_path / "state.sqlite")
    flag = Flag("disk:/", "disk", "warn", "Disk / is 88% full")
    s.apply(decide([flag], s.issues(), 100.0, COOLDOWN), 100.0)
    stored = s.issues()["disk:/"]
    assert stored["severity"] == "warn" and stored["last_reported"] == 100.0
    s.apply(decide([], s.issues(), 200.0, COOLDOWN), 200.0)
    assert s.issues()["disk:/"]["resolved_at"] == 200.0
    s.close()


def test_prune_drops_old_samples_and_templates():
    s = State(":memory:")
    now = 100 * 86400
    s.add_samples({("a", int((now - 40 * 86400) // HOUR)): 1, ("a", int(now // HOUR)): 2})
    s.add_templates({("a", "old"): (0, now - 100 * 86400, 1), ("a", "new"): (now, now, 1)})
    s.prune(now, 30, 90)
    assert sum(s.error_samples(0, 10**9)["a"].values()) == 2
    assert s.known_templates() == {("a", "new")}
    s.close()
