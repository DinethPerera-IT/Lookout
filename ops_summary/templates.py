"""Turn log messages into templates so variants of one message count as one.

"Failed to connect to 10.0.0.5:7844 after 3021ms" and
"Failed to connect to 10.0.0.9:7844 after 15ms" both become
"Failed to connect to <ip> after <n>ms". Counting templates instead of raw lines
makes error spikes measurable and "never seen before" detectable.

Input is already redacted, so placeholders like <token> pass through unchanged.
"""
import hashlib
import re

MAX_LENGTH = 200

_RULES = [
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    # IPv6: must contain "::" or a hex letter, so times like 12:30:45 are left alone
    (re.compile(r"(?<![\w:])(?=[0-9a-fA-F:]*(?:::|[a-fA-F]))[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}(?![\w:])"),
     "<ip>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\s+"), " "),
]

_SOURCE_RULES = [
    (re.compile(r"^session-\d+\.scope$"), "session-<n>.scope"),
    (re.compile(r"[0-9a-f]{12,}"), "<id>"),
    (re.compile(r"^run-[a-z0-9]+\.(scope|service)$"), r"run-<id>.\1"),
]


def template_of(message):
    text = message.strip()
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text[:MAX_LENGTH]


def normalize_source(source):
    """Collapse per-session unit names (session-648.scope) into one source."""
    for pattern, replacement in _SOURCE_RULES:
        source = pattern.sub(replacement, source)
    return source


def fingerprint(source, template):
    """Short stable ID for a (source, template) pair, used in flag keys."""
    return hashlib.sha1(f"{source}|{template}".encode()).hexdigest()[:10]
