from lookout.__main__ import collect_all


def test_one_broken_collector_does_not_stop_the_others(cfg):
    def broken(cfg):
        raise RuntimeError("boom")

    facts = collect_all(cfg, {"broken": broken, "fine": lambda cfg: {"ok": True}})
    assert facts["broken"] == {"ok": False, "error": "RuntimeError: boom"}
    assert facts["fine"] == {"ok": True}


def test_config_local_toml_is_layered_on_top(tmp_path):
    from lookout import config
    (tmp_path / "config.toml").write_text('[dashboard]\nport = 8088\nhide_containers = []\n[llm]\nmodel = "m"\n')
    (tmp_path / "config.local.toml").write_text('[dashboard]\nhide_containers = ["x"]\n')
    cfg = config.load(tmp_path / "config.toml")
    assert cfg["dashboard"] == {"port": 8088, "hide_containers": ["x"]} and cfg["llm"] == {"model": "m"}
    (tmp_path / "config.local.toml").unlink()
    assert config.load(tmp_path / "config.toml")["dashboard"]["hide_containers"] == []
