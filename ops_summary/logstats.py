"""Shared error counting for log sources (journal, Docker).

Every log source produces the same shape, so the rules don't care where a line
came from: {"total", "by_source": {source: {total, errors, noise}},
"error_templates": {source: {template: {count, examples}}}}.
Messages must already be redacted.
"""
from .templates import template_of

MAX_EXAMPLES = 3
MAX_EXAMPLE_LENGTH = 300


def is_noise(message, noise):
    return any(p.search(message) for p in noise)


class LogStats:
    """Counts for the current window."""

    def __init__(self, noise=()):
        self.noise = noise
        self.total = 0
        self.by_source = {}
        self.error_templates = {}

    def add(self, source, message, is_error):
        self.total += 1
        counts = self.by_source.setdefault(source, {"total": 0, "errors": 0, "noise": 0})
        counts["total"] += 1
        if not is_error:
            return
        if is_noise(message, self.noise):
            counts["noise"] += 1
            return
        counts["errors"] += 1
        slot = self.error_templates.setdefault(source, {}).setdefault(
            template_of(message), {"count": 0, "examples": []})
        slot["count"] += 1
        if len(slot["examples"]) < MAX_EXAMPLES:
            slot["examples"].append(message[:MAX_EXAMPLE_LENGTH])

    def result(self):
        return {"total": self.total, "by_source": dict(sorted(self.by_source.items())),
                "error_templates": self.error_templates}


class History:
    """Hourly error counts and templates from past logs, to seed the baseline."""

    def __init__(self, noise=()):
        self.noise = noise
        self.samples = {}
        self.templates = {}

    def add_error(self, source, ts, message):
        if is_noise(message, self.noise):
            return
        key = (source, int(ts // 3600))
        self.samples[key] = self.samples.get(key, 0) + 1
        tkey = (source, template_of(message))
        first, last, count = self.templates.get(tkey, (ts, ts, 0))
        self.templates[tkey] = (min(first, ts), max(last, ts), count + 1)
