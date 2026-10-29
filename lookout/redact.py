"""Strip secrets and identifying data from log text before it goes anywhere.

Applied once, at the collector boundary, so every later stage (state, stdout,
the local LLM, Discord) only ever sees redacted text. Rules run in order from
most to least specific, and replacements keep the key name so lines stay
readable: "password=hunter2" -> "password=<redacted>".

False positives are cheap (a hex ID becomes <token>); false negatives leak.
The rules therefore err on the side of redacting.

Demo:  echo 'curl -H "Authorization: Bearer abc123def456"' | python3 -m lookout.redact
"""
import re
import sys
from collections import Counter

def _keep_quotes(m):
    """key_value replacement that preserves quoting, so JSON lines stay valid JSON."""
    value = m.group(3)
    quote = value[0] if value[:1] and value[0] in "\"'" else ""
    return f"{m.group(1)}{m.group(2)}{quote}<redacted>{quote}"


_SECRET_WORDS = (r"passw(?:or)?d|passphrase|secret|token|api[_-]?key|apikey|access[_-]?key"
                 r"|private[_-]?key|credential|session[_-]?id|auth")

RULES = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
                               re.S), "<private-key>"),
    ("discord_webhook", re.compile(r"https?://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/\S+"),
     "<discord-webhook>"),
    ("kuma_push", re.compile(r"(/api/push/)[A-Za-z0-9]+"), r"\1<redacted>"),
    ("url_credentials", re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted>@"),
    ("auth_header", re.compile(r"(?i)\b((?:proxy-)?authorization|cookie|set-cookie|x-api-key|x-auth-token)"
                               r"(\"?\s*[:=]\s*\"?)[^\r\n\"]+"), r"\1\2<redacted>"),
    ("bearer", re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 <redacted>"),
    ("jwt", re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*"), "<jwt>"),
    ("known_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[\w-]{10,}"
                               r"|AKIA[0-9A-Z]{16}|tskey-[\w-]{10,}|sk-[A-Za-z0-9_-]{20,}|glpat-[\w-]{20,})"),
     "<token>"),
    # Three dot-separated base64url parts, e.g. Discord bot tokens
    ("dotted_token", re.compile(r"\b[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}"), "<token>"),
    ("key_value", re.compile(r"(?i)\b([\w.-]*(?:" + _SECRET_WORDS + r")[\w.-]*)(\"?'?\s*[:=]\s*)"
                             r"(\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"), _keep_quotes),
    ("query_secret", re.compile(r"([?&](?:key|sig|signature|code|state)=)[^&\s]+"), r"\1<redacted>"),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), "<email>"),
    # WiFi network names: wpa_supplicant and systemd-networkd formats
    ("ssid", re.compile(r"(?i)(\bssid=)('[^']*'|\"[^\"]*\")"), r"\1<ssid>"),
    ("ssid_networkd", re.compile(r"(Connected WiFi access point: ).+?( \(|$)"), r"\1<ssid>\2"),
    ("mac", re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b"), "<mac>"),
    # Catch-alls for unknown token formats: long runs mixing letters and digits
    ("long_token", re.compile(r"\b(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{32,}\b"),
     "<token>"),
    ("base64_blob", re.compile(r"(?<![\w/+])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])"
                               r"(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{40,}={0,2}"), "<blob>"),
]

# A second, simpler net used only to *check* redaction: a secret-sounding word
# followed by a value that isn't a placeholder, or raw token shapes.
_LEAK_PATTERNS = [
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\"?'?\s*[:=]\s*\"?'?(?!<)[^\s\"'<]{3,}"),
    re.compile(r"(?i)\bbearer\s+(?!<)\S{8,}"),
    re.compile(r"\beyJ[\w-]{10,}"),
    re.compile(r"://[^/\s:@<]+:[^/\s@<]+@"),
    re.compile(r"discord(?:app)?\.com/api/webhooks/\d"),
]


def redact_counts(text):
    """Return (redacted_text, Counter of rule name -> replacements). Never returns secret values."""
    counts = Counter()
    for name, pattern, replacement in RULES:
        text, n = pattern.subn(replacement, text)
        if n:
            counts[name] += n
    return text, counts


def redact(text):
    return redact_counts(text)[0]


def find_leaks(text):
    """Count lines that still look like they contain a secret (for checks, never printed)."""
    return sum(1 for line in text.splitlines() if any(p.search(line) for p in _LEAK_PATTERNS))


if __name__ == "__main__":
    for line in sys.stdin:
        sys.stdout.write(redact(line))
