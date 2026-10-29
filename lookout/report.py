"""Build the Discord message from a detection result. Pure code: no network, no LLM.

Everything in the facts embed is rendered here from structured flags. Log text
(evidence) is untrusted: it goes inside code blocks with backticks neutralised,
mentions are defused, and allowed_mentions forbids pings regardless.
"""
import re
from datetime import datetime, timezone

COLOR = {"crit": 0xE74C3C, "warn": 0xF1C40F, "resolved": 0x2ECC71}
ICON = {"crit": "🔴", "warn": "🟡"}
REASON = {"new": "new", "worse": "worse", "reappeared": "back again", "reminder": "still active (24 h reminder)"}
FACTS_LIMIT = 3800      # Discord embed description max is 4096
RESOLVED_LIMIT = 1500   # whole message (all embeds) max is 6000
EVIDENCE_LINES = 3
EVIDENCE_WIDTH = 200

_MARKDOWN = re.compile(r"([\\*_~`|>\[\]])")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def defuse_mentions(text):
    """@everyone -> @​everyone: belt and braces on top of allowed_mentions."""
    return text.replace("@", "@​")


def escape(text):
    """For code-generated text that may contain unit/container names."""
    return defuse_mentions(_MARKDOWN.sub(r"\\\1", text))


def evidence_block(lines):
    """Untrusted log lines -> one code block that they cannot break out of."""
    clean = []
    for line in lines[:EVIDENCE_LINES]:
        # One log entry = one line, however many newlines it contains
        line = _CONTROL.sub("", line.replace("\r", "").replace("\n", " ⏎ ")).replace("`", "ˋ")[:EVIDENCE_WIDTH]
        clean.append(defuse_mentions(line))
    return "```\n" + "\n".join(clean) + "\n```" if clean else ""


def fit(chunks, limit, more_label):
    """Join chunks until the limit; summarise what didn't fit instead of cutting mid-line."""
    out, used = [], 0
    for i, chunk in enumerate(chunks):
        if used + len(chunk) + 1 > limit - 80:
            out.append(more_label.format(n=len(chunks) - i))
            break
        out.append(chunk)
        used += len(chunk) + 1
    return "\n".join(out)


def title_for(report):
    counts = {s: sum(1 for r in report if r["severity"] == s) for s in ("crit", "warn")}
    parts = [f"{counts['crit']} critical"] * bool(counts["crit"]) + \
            [f"{counts['warn']} warning{'s' * (counts['warn'] != 1)}"] * bool(counts["warn"])
    worst = "crit" if counts["crit"] else "warn"
    return f"{ICON[worst]} Lookout: {', '.join(parts)}", COLOR[worst]


def build(result, title_prefix=""):
    """Return the webhook payload, or None when there is nothing to say (stay silent)."""
    report, resolved = result["report"], result["resolved"]
    if not report and not resolved:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    footer = "Lookout · facts from deterministic rules"
    b = result.get("baseline", {})
    if b.get("warming_up"):
        footer += f" · error baseline warming up ({b['hours']}/{b['required_hours']} h)"
    embeds = []

    if report:
        chunks = []
        for r in report:
            chunk = f"{ICON[r['severity']]} **{REASON.get(r['reason'], r['reason'])}** · {escape(r['summary'])}"
            block = evidence_block(r.get("evidence") or [])
            chunks.append(chunk + ("\n" + block if block else ""))
        still = [s["key"] for s in result.get("suppressed", [])]
        if still:
            chunks.append(f"-# Still active, already reported: {escape(', '.join(still))}")
        title, color = title_for(report)
        embeds.append({"title": title_prefix + title, "color": color,
                       "description": fit(chunks, FACTS_LIMIT, "…and {n} more (full list: journalctl -u lookout)"),
                       "footer": {"text": footer}, "timestamp": now})

    if resolved:
        lines = [f"✅ {escape(r['summary'])}" for r in resolved]
        embeds.append({"title": title_prefix + f"✅ Resolved ({len(resolved)})", "color": COLOR["resolved"],
                       "description": fit(lines, RESOLVED_LIMIT, "…and {n} more"),
                       "footer": {"text": footer}, "timestamp": now})

    return {"username": "Lookout", "allowed_mentions": {"parse": []}, "embeds": embeds}
