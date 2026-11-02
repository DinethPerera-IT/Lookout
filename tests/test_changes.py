"""Recent changes before a failure: apt history, container (re)creation, config file times."""
import time
from types import SimpleNamespace

from lookout import changes, crash

APT = """Start-Date: 2026-11-01  07:09:18
Commandline: apt-get --only-upgrade install -y netdata
Upgrade: netdata-plugin-apps:amd64 (2.12.0.360, 2.12.0.375), netdata-plugin-nfacct:amd64 (2.12.0.360, 2.12.0.375), netdata:amd64 (1, 2), netdata-plugin-go:amd64 (1, 2)
End-Date: 2026-11-01  07:09:31

Start-Date: 2026-10-20  10:00:00
Install: htop:amd64 (3.3.0-4)
End-Date: 2026-10-20  10:00:05

Start-Date: 2026-11-01  09:00:00
Install: docker-ce:amd64 (5:28.1.1-1~ubuntu.24.04~noble, automatic)
Remove: old-thing:amd64 (1.0)
End-Date: 2026-11-01  09:00:09
"""


def local(text):
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M:%S"))


def test_apt_history_in_the_window(tmp_path):
    log = tmp_path / "history.log"
    log.write_text(APT)
    found = changes.apt_changes(local("2026-10-31 12:00:00"), local("2026-11-01 12:00:00"), log)
    assert [c["what"] for c in found] == [
        "apt: upgraded 4 packages (netdata-plugin-apps, netdata-plugin-nfacct, netdata, +1 more)",
        "apt: installed 1 package (docker-ce); removed 1 package (old-thing)"]
    assert found[0]["at"] == local("2026-11-01 07:09:18")


def test_unreadable_apt_log_is_no_changes(tmp_path):
    assert changes.apt_changes(0, 2e9, tmp_path / "missing.log") == []


def test_recent_combines_sources_newest_first_with_code_written_summaries(tmp_path):
    log = tmp_path / "history.log"
    log.write_text(APT)
    failed = local("2026-11-01 09:30:00")
    mtimes = {"/etc/systemd/system/caddy.service.d/override.conf": failed - 120,
              "/etc/systemd/system/caddy.service.d/lookout-onfailure.conf": failed - 60,   # Lookout's own hook
              "/usr/lib/systemd/system/caddy.service": failed - 90 * 86400}
    ev = {"failed_at": failed, "target": {"kind": "unit", "name": "caddy.service",
                                          "fragment_path": "/usr/lib/systemd/system/caddy.service",
                                          "drop_ins": list(mtimes)[:2]}}
    found = changes.recent({}, ev, failed + 30, apt_log=log, stat=lambda p: SimpleNamespace(st_mtime=mtimes[p]))
    assert [c["summary"] for c in found] == [
        "/etc/systemd/system/caddy.service.d/override.conf was modified, 2 min before the failure",
        "apt: installed 1 package (docker-ce); removed 1 package (old-thing), 30 min before the failure",
        "apt: upgraded 4 packages (netdata-plugin-apps, netdata-plugin-nfacct, netdata, +1 more), 2 h before the failure"]


def test_hidden_or_missing_files_are_skipped():
    def stat(path):
        raise PermissionError(path)          # e.g. a compose file under /home, hidden by ProtectHome
    assert changes.file_changes(["/home/u/app/compose.yaml"], 0, 2e9, stat) == []


def test_recreated_container_is_a_change_and_feeds_the_classifier():
    failed = 1_800_000_000
    ev = {"failed_at": failed, "exit_code": 1, "log_lines": ["Traceback (most recent call last):", "KeyError: 'x'"],
          "kernel_oom": [], "host": {},
          "target": {"kind": "container", "name": "uptime-kuma", "image": "louislam/uptime-kuma:2",
                     "created": failed - 600, "compose_files": []}}
    ev["recent_changes"] = changes.recent({}, ev, failed + 5, apt_log="/nonexistent")
    assert ev["recent_changes"][0]["summary"] == \
        "container uptime-kuma was (re)created from image louislam/uptime-kuma:2, 10 min before the failure"
    cause, matches = crash.classify(ev)
    assert cause == "after_recent_change" and matches[0]["signal"] == "recent change"


def test_changes_after_the_failure_do_not_count():
    failed = 1_800_000_000
    ev = {"failed_at": failed, "target": {"kind": "container", "name": "x", "created": failed + 60}}
    assert changes.recent({}, ev, failed + 120, apt_log="/nonexistent") == []
