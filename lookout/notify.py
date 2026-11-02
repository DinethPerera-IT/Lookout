"""Deliver reports: Discord webhook and the Uptime Kuma push heartbeat.

Both URLs are secrets (they contain tokens). They are never printed, logged or
put in exception messages; errors say "HTTP 404", not where.
"""
import json
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

USER_AGENT = "Lookout (https://github.com/DinethPerera-IT, 1.0)"  # Discord rejects urllib's default
MAX_RETRY_AFTER = 10  # seconds we're willing to wait on a Discord rate limit


class NotifyError(Exception):
    pass


def send_discord(webhook_url, payload, timeout=15, opener=urllib.request.urlopen, sleep=time.sleep):
    """POST the payload; ?wait=true makes Discord confirm the message was actually created.
    Returns the new message's id (used to add the AI section later), or None if not given."""
    parts = urlsplit(webhook_url)
    query = dict(parse_qsl(parts.query), wait="true")
    return _discord(urlunsplit(parts._replace(query=urlencode(query))), "POST", payload, timeout, opener, sleep)


def edit_discord(webhook_url, message_id, payload, timeout=15, opener=urllib.request.urlopen, sleep=time.sleep):
    """Replace the content of a message this webhook sent (PATCH .../messages/<id>)."""
    if not str(message_id).isdigit():
        raise NotifyError("Discord edit failed: no message id")
    parts = urlsplit(webhook_url)
    url = urlunsplit(parts._replace(path=parts.path.rstrip("/") + f"/messages/{message_id}"))
    _discord(url, "PATCH", payload, timeout, opener, sleep)


def _discord(url, method, payload, timeout, opener, sleep):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    for attempt in (1, 2):
        try:
            with opener(req, timeout=timeout) as resp:
                body = resp.read()
            try:
                message_id = json.loads(body or b"{}").get("id")
            except (ValueError, AttributeError):
                message_id = None
            return message_id if isinstance(message_id, str) and message_id.isdigit() else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:
                try:
                    retry_after = float(json.loads(e.read() or b"{}").get("retry_after", 1))
                except ValueError:
                    retry_after = 1
                sleep(min(retry_after, MAX_RETRY_AFTER))
                continue
            raise NotifyError(f"Discord webhook failed: HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            raise NotifyError(f"Discord webhook unreachable: {type(reason).__name__}") from None
    raise NotifyError("Discord webhook failed: still rate limited")


def kuma_url(push_url, status, msg, ping_ms=None):
    """Kuma shows push URLs with ?status=up&msg=OK&ping= already attached; replace those."""
    parts = urlsplit(push_url)
    query = dict(parse_qsl(parts.query))
    query.update(status=status, msg=msg)
    if ping_ms is not None:
        query["ping"] = str(int(ping_ms))
    return urlunsplit(parts._replace(query=urlencode(query)))


def push_kuma(push_url, status, msg, ping_ms=None, timeout=10, opener=urllib.request.urlopen):
    req = urllib.request.Request(kuma_url(push_url, status, msg, ping_ms), headers={"User-Agent": USER_AGENT})
    try:
        with opener(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        raise NotifyError(f"Kuma push failed: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        reason = getattr(e, "reason", e)
        raise NotifyError(f"Kuma push unreachable: {type(reason).__name__}") from None
