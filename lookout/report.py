"""Build the Discord message from a detection result. Pure code: no network, no LLM.

Everything in the facts embed is rendered here from structured flags. Log text
(evidence) is untrusted: it goes inside code blocks with backticks neutralised,
mentions are defused, and allowed_mentions forbids pings regardless.
"""
import re
from datetime import datetime, timezone

COLOR = {"crit": 0xE74C3C, "warn": 0xF1C40F, "resolved": 0x2ECC71, "ai": 0x95A5A6}
ICON = {"crit": "🔴", "warn": "🟡"}
REASON = {"new": "new", "worse": "worse", "reappeared": "back again", "reminder": "still active (24 h reminder)"}
FACTS_LIMIT = 3000      # Discord embed description max is 4096
AI_LIMIT = 1600
RESOLVED_LIMIT = 800    # whole message (all embeds) max is 6000
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


def ai_embed(ai, title_prefix, footer, now):
    """The model's explanation: its own embed, below the facts, clearly labelled."""
    chunks = [escape(ai["summary"])] + [f"• `{escape(key).replace('`', '')}`: {escape(text)}" for key, text in ai["alerts"]]
    return {"title": title_prefix + f"🤖 AI explanation · unverified ({ai['model']})", "color": COLOR["ai"],
            "description": fit(chunks, AI_LIMIT, "…and {n} more"),
            "footer": {"text": "Written by a local model from the facts above; it cannot change them"},
            "timestamp": now}


def build(result, title_prefix="", ai=None):
    """Return the webhook payload, or None when there is nothing to say (stay silent).

    `ai` is the result of llm.explain(): rendered below the facts when ok, otherwise
    the facts embed notes (in code-written words) that no explanation is available."""
    report, resolved = result["report"], result["resolved"]
    if not report and not resolved:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    footer = "Lookout · facts from deterministic rules"
    b = result.get("baseline", {})
    if b.get("warming_up"):
        footer += f" · error baseline warming up ({b['hours']}/{b['required_hours']} h)"
    facts_footer = footer
    if report and ai is not None and not ai["ok"]:
        facts_footer += f" · AI explanation unavailable: {ai['reason']}"
    embeds = []

    if report:
        chunks = []
        for r in report:
            chunk = f"{ICON[r['severity']]} **{REASON.get(r['reason'], r['reason'])}** · {escape(r['summary'])}"
            lines = r.get("evidence") or []
            if r.get("cause"):  # a classified crash: say what code concluded, quote what matched
                chunk += f"\nCause (classified by code): **{escape(r['cause'])}**"
                matched = [m["detail"] for m in r.get("cause_matches", []) if m["signal"] in ("log line", "kernel OOM killer")]
                lines = matched or lines[-EVIDENCE_LINES:]
            block = evidence_block(lines)
            chunks.append(chunk + ("\n" + block if block else ""))
        still = [s["key"] for s in result.get("suppressed", [])]
        if still:
            chunks.append(f"-# Still active, already reported: {escape(', '.join(still))}")
        title, color = title_for(report)
        embeds.append({"title": title_prefix + title, "color": color,
                       "description": fit(chunks, FACTS_LIMIT, "…and {n} more (full list: journalctl -u lookout)"),
                       "footer": {"text": facts_footer}, "timestamp": now})
        if ai is not None and ai["ok"]:
            embeds.append(ai_embed(ai, title_prefix, footer, now))

    if resolved:
        lines = [f"✅ {escape(r['summary'])}" for r in resolved]
        embeds.append({"title": title_prefix + f"✅ Resolved ({len(resolved)})", "color": COLOR["resolved"],
                       "description": fit(lines, RESOLVED_LIMIT, "…and {n} more"),
                       "footer": {"text": footer}, "timestamp": now})

    return {"username": "Lookout", "allowed_mentions": {"parse": []}, "embeds": embeds}
