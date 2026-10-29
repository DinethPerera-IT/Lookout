"""Persistent state in SQLite: error baselines, known templates and reported issues.

The connection never autocommits. A real run calls commit() at the end; a dry
run just closes, which rolls back every change, so dry runs never affect what
later real runs report.
"""
import sqlite3
from dataclasses import dataclass, field

HOUR = 3600
RANK = {"warn": 1, "crit": 2}

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
    # Errors per source per hour bucket (bucket = window start // 3600)
    """CREATE TABLE IF NOT EXISTS error_samples (
        source TEXT, hour INTEGER, count INTEGER, PRIMARY KEY (source, hour))""",
    """CREATE TABLE IF NOT EXISTS templates (
        source TEXT, template TEXT, first_seen REAL, last_seen REAL, count INTEGER,
        PRIMARY KEY (source, template))""",
    # One row per issue we have reported (or are suppressing).
    # resolved_at: when it last cleared (NULL while active)
    # resolved_notified: 1 if a "resolved" notice was sent for the current clearance
    # last_resolved_notice: when the most recent "resolved" notice was sent
    """CREATE TABLE IF NOT EXISTS issues (
        key TEXT PRIMARY KEY, kind TEXT, severity TEXT, summary TEXT,
        first_seen REAL, last_reported REAL, resolved_at REAL, resolved_notified INTEGER DEFAULT 0,
        last_resolved_notice REAL)""",
    # Containers as seen at the last run, to detect restarts and disappearances.
    # was_running is sticky: once seen running, a stopped container stays "expected".
    """CREATE TABLE IF NOT EXISTS containers (
        name TEXT PRIMARY KEY, id TEXT, state TEXT, up_min REAL, was_running INTEGER, seen_at REAL)""",
]


@dataclass
class Decision:
    report: list = field(default_factory=list)       # (flag, reason): new | reappeared | worse | reminder
    suppressed: list = field(default_factory=list)   # (flag, reason)
    resolved: list = field(default_factory=list)     # issue rows to announce as resolved
    resolved_silently: list = field(default_factory=list)  # cleared, notice held back (flapping)
    forget: list = field(default_factory=list)       # keys to delete


def decide(flags, issues, now, cooldown):
    """Compare this run's flags with stored issues. Pure function: no I/O.

    Invariant: the last message about an issue never says "resolved" while it is
    broken. Flap protection therefore only ever holds back *resolved* notices
    (at most one per flap window), never a failure.
    """
    reminder = cooldown["reminder_hours"] * HOUR
    event_cooldown = cooldown["event_cooldown_hours"] * HOUR
    flap_window = cooldown["flap_window_hours"] * HOUR
    d = Decision()
    current = {f.key for f in flags}

    def notice_allowed(row):
        last = row["last_resolved_notice"]
        return last is None or now - last >= flap_window

    for flag in flags:
        row = issues.get(flag.key)
        if row is None:
            d.report.append((flag, "new"))
        elif row["resolved_at"] is not None and row["resolved_notified"]:
            # The last thing said was "resolved", so a recurrence must always be heard.
            d.report.append((flag, "reappeared"))
        elif RANK[flag.severity] > RANK[row["severity"]]:
            d.report.append((flag, "worse"))
        elif now - row["last_reported"] >= (event_cooldown if flag.kind == "event" else reminder):
            d.report.append((flag, "reminder"))
        elif row["resolved_at"] is not None:
            # Cleared briefly but the resolved notice was held back: from the reader's
            # point of view it never stopped being broken.
            d.suppressed.append((flag, "flapping"))
        else:
            d.suppressed.append((flag, "cooldown"))

    for key, row in issues.items():
        if key in current:
            continue
        if row["kind"] == "event":
            # Events (spikes, novel errors) never "resolve"; they just expire.
            if now - row["last_reported"] >= event_cooldown:
                d.forget.append(key)
        elif row["resolved_at"] is None or not row["resolved_notified"]:
            # Just cleared, or cleared earlier with the notice held back: announce it
            # unless a resolved notice already went out within the flap window.
            if notice_allowed(row):
                d.resolved.append(row)
            elif row["resolved_at"] is None:
                d.resolved_silently.append(row)
        elif notice_allowed(row):
            d.forget.append(key)  # announced and stable for a full flap window
    return d


class State:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, autocommit=False)
        self.conn.row_factory = sqlite3.Row
        for statement in SCHEMA:
            self.conn.execute(statement)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()  # uncommitted changes are rolled back

    # --- baseline ---------------------------------------------------------
    def meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return float(row["value"]) if row else None

    def set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))

    def baseline_start(self):
        return self.meta("baseline_start")

    def set_baseline_start(self, ts):
        self.set_meta("baseline_start", ts)

    def add_samples(self, samples):
        """samples: {(source, hour): count}. Re-running an hour replaces its count."""
        self.conn.executemany(
            "INSERT INTO error_samples VALUES (?, ?, ?) "
            "ON CONFLICT (source, hour) DO UPDATE SET count = excluded.count",
            [(s, h, c) for (s, h), c in samples.items()])

    def error_samples(self, start_hour, end_hour):
        """{source: {hour: count}} for start_hour <= hour < end_hour."""
        result = {}
        for row in self.conn.execute(
                "SELECT source, hour, count FROM error_samples WHERE hour >= ? AND hour < ?",
                (start_hour, end_hour)):
            result.setdefault(row["source"], {})[row["hour"]] = row["count"]
        return result

    def known_templates(self):
        return {(r["source"], r["template"]) for r in self.conn.execute("SELECT source, template FROM templates")}

    def add_templates(self, templates):
        """templates: {(source, template): (first_seen, last_seen, count)}"""
        self.conn.executemany(
            "INSERT INTO templates VALUES (?, ?, ?, ?, ?) ON CONFLICT (source, template) DO UPDATE SET "
            "first_seen = min(first_seen, excluded.first_seen), "
            "last_seen = max(last_seen, excluded.last_seen), count = count + excluded.count",
            [(s, t, first, last, n) for (s, t), (first, last, n) in templates.items()])

    # --- containers -------------------------------------------------------
    def containers(self):
        return {r["name"]: dict(r) for r in self.conn.execute("SELECT * FROM containers")}

    def save_containers(self, rows):
        """rows: [{name, id, state, up_min, was_running, seen_at}]; replaces the snapshot."""
        self.conn.execute("DELETE FROM containers")
        self.conn.executemany(
            "INSERT INTO containers VALUES (:name, :id, :state, :up_min, :was_running, :seen_at)", rows)

    # --- issues -----------------------------------------------------------
    def issues(self):
        return {r["key"]: dict(r) for r in self.conn.execute("SELECT * FROM issues")}

    def apply(self, decision, now):
        for flag, _ in decision.report:
            self.conn.execute(
                "INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL) ON CONFLICT (key) DO UPDATE SET "
                "kind = excluded.kind, severity = excluded.severity, summary = excluded.summary, "
                "first_seen = CASE WHEN resolved_at IS NULL THEN first_seen ELSE excluded.first_seen END, "
                "last_reported = excluded.last_reported, resolved_at = NULL, resolved_notified = 0",
                (flag.key, flag.kind, flag.severity, flag.summary, now, now))
        for flag, _ in decision.suppressed:
            self.conn.execute(
                "UPDATE issues SET severity = ?, summary = ?, resolved_at = NULL WHERE key = ?",
                (flag.severity, flag.summary, flag.key))
        for row in decision.resolved:
            self.conn.execute(
                "UPDATE issues SET resolved_at = coalesce(resolved_at, ?), resolved_notified = 1, "
                "last_resolved_notice = ? WHERE key = ?", (now, now, row["key"]))
        for row in decision.resolved_silently:
            self.conn.execute("UPDATE issues SET resolved_at = ? WHERE key = ?", (now, row["key"]))
        self.conn.executemany("DELETE FROM issues WHERE key = ?", [(k,) for k in decision.forget])

    def prune(self, now, sample_days, template_days):
        self.conn.execute("DELETE FROM error_samples WHERE hour < ?", (int((now - sample_days * 86400) // HOUR),))
        self.conn.execute("DELETE FROM templates WHERE last_seen < ?", (now - template_days * 86400,))
