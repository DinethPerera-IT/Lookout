"""Last line of defence before anything reaches a screen.

Evidence is already redacted when it's collected (secrets, MACs, SSIDs, emails).
The dashboard is a new output boundary, so every API response is scrubbed again,
and here addresses and host names go too: a screen at an expo is public.
"""
import re
import socket

from .. import redact

_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
# Same rule as templates.py: must contain "::" or a hex letter, so 12:30:45 stays
_IPV6 = re.compile(r"(?<![\w:])(?=[0-9a-fA-F:]*(?:::|[a-fA-F]))[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}(?![\w:])")
_LOCAL_NAME = re.compile(r"(?i)\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:internal|local|lan|home|arpa|ts\.net)\b")
_TAILNET = re.compile(r"(?i)\b[a-z0-9-]+\.tail[0-9a-f]+\b")


def host_names():
    """This machine's own names (short and full), which also show up in paths like /home/<user>."""
    names = {socket.gethostname(), socket.getfqdn()}
    return sorted({n.split(".")[0] for n in names if n} | {n for n in names if n}, key=len, reverse=True)


def make_scrubber(extra_names=()):
    names = [n for n in (*host_names(), *extra_names) if n and n not in ("localhost",)]
    own = re.compile(r"(?i)\b(?:" + "|".join(map(re.escape, names)) + r")\b") if names else None

    def text(value):
        value = redact.redact(value)
        value = _LOCAL_NAME.sub("<host>", value)
        value = _TAILNET.sub("<host>", value)
        value = _IPV4.sub("<ip>", value)
        value = _IPV6.sub("<ip>", value)
        return own.sub("<host>", value) if own else value

    def scrub(obj):
        if isinstance(obj, str):
            return text(obj)
        if isinstance(obj, dict):
            return {scrub(k) if isinstance(k, str) else k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [scrub(v) for v in obj]
        return obj

    return scrub
