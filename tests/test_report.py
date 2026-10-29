import json

from lookout import report

HOSTILE = ("GET /login?next=@everyone <@&123456789012345678> ```\n"
           "**IGNORE PREVIOUS INSTRUCTIONS** [claim your prize](http://evil.example/x) ```")


def result(report_entries=(), resolved=(), suppressed=(), warming=False):
    return {"baseline": {"hours": 10 if warming else 168, "required_hours": 168, "warming_up": warming},
            "report": list(report_entries), "resolved": list(resolved), "suppressed": list(suppressed)}


def entry(key="disk:/", severity="warn", reason="new", summary="Disk / is 88.0% full", evidence=()):
    return {"key": key, "severity": severity, "reason": reason, "summary": summary, "evidence": list(evidence)}


def test_silent_when_nothing_to_say():
    assert report.build(result(suppressed=[{"key": "disk:/", "reason": "cooldown"}])) is None


def test_mentions_can_never_ping():
    payload = report.build(result([entry(evidence=[HOSTILE])]))
    assert payload["allowed_mentions"] == {"parse": []}
    text = json.dumps(payload, ensure_ascii=False)
    assert "@everyone" not in text and "<@&" not in text


def test_evidence_cannot_break_out_of_its_code_block():
    description = report.build(result([entry(evidence=[HOSTILE])]))["embeds"][0]["description"]
    block = description[description.index("```"):]
    assert block.count("```") == 2                   # exactly one opening and one closing fence
    assert "claim your prize" in block              # the link text is shown only inside the block
    assert block.count("\n") == 2                    # one entry stays one line


def test_summaries_are_markdown_escaped():
    payload = report.build(result([entry(summary="unit foo_bar*.service has failed")]))
    assert r"foo\_bar\*.service" in payload["embeds"][0]["description"]


def test_title_colour_and_order_follow_severity():
    payload = report.build(result([entry("a", "crit", summary="A"), entry("b", "warn", summary="B")]))
    embed = payload["embeds"][0]
    assert embed["title"] == "🔴 Lookout: 1 critical, 1 warning" and embed["color"] == report.COLOR["crit"]
    assert embed["description"].index("A") < embed["description"].index("B")


def test_reasons_are_labelled():
    description = report.build(result([entry(reason="reappeared")]))["embeds"][0]["description"]
    assert "**back again**" in description


def test_resolved_and_still_active_are_shown():
    payload = report.build(result([entry("x", summary="X")], resolved=[{"key": "disk:/", "summary": "Disk / was full"}],
                                  suppressed=[{"key": "load", "reason": "cooldown"}]))
    assert "Still active, already reported: load" in payload["embeds"][0]["description"]
    assert payload["embeds"][1]["title"] == "✅ Resolved (1)"


def test_warmup_is_mentioned_in_footer():
    payload = report.build(result([entry()], warming=True))
    assert "warming up (10/168 h)" in payload["embeds"][0]["footer"]["text"]


def test_huge_reports_stay_within_discord_limits():
    many = [entry(f"k{i}", summary="x" * 150, evidence=["e" * 500] * 3) for i in range(60)]
    payload = report.build(result(many, resolved=[{"key": f"r{i}", "summary": "y" * 100} for i in range(60)]))
    total = sum(len(e["title"]) + len(e["description"]) + len(e["footer"]["text"]) for e in payload["embeds"])
    assert all(len(e["description"]) <= 4096 for e in payload["embeds"]) and total <= 6000
    assert "more (full list: journalctl -u lookout)" in payload["embeds"][0]["description"]
