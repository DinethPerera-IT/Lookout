from ops_summary.__main__ import collect_all


def test_one_broken_collector_does_not_stop_the_others(cfg):
    def broken(cfg):
        raise RuntimeError("boom")

    facts = collect_all(cfg, {"broken": broken, "fine": lambda cfg: {"ok": True}})
    assert facts["broken"] == {"ok": False, "error": "RuntimeError: boom"}
    assert facts["fine"] == {"ok": True}
