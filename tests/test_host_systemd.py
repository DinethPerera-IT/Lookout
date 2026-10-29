from ops_summary.collectors import host, systemd

MEMINFO = """MemTotal:       16000000 kB
MemFree:         3000000 kB
MemAvailable:    1200000 kB
SwapTotal:       4000000 kB
SwapFree:        3000000 kB
HugePages_Total:       0
"""


def test_parse_meminfo_converts_kb_to_bytes():
    mem = host.parse_meminfo(MEMINFO)
    assert mem["MemTotal"] == 16_000_000 * 1024
    assert mem["HugePages_Total"] == 0  # no unit, left as-is


def test_host_collect_reports_memory_pressure_values(cfg, tmp_path):
    path = tmp_path / "meminfo"
    path.write_text(MEMINFO)
    facts = host.collect(cfg, meminfo_path=path)
    assert facts["memory"]["available_pct"] == 7.5
    assert facts["memory"]["swap_used_bytes"] == 1_000_000 * 1024


def test_missing_mount_is_reported_not_raised():
    result = host.disk("/does/not/exist")
    assert result["mountpoint"] == "/does/not/exist" and "error" in result


def test_parse_failed_units():
    stdout = ('[{"unit":"caddy.service","load":"loaded","active":"failed","sub":"failed",'
              '"description":"Caddy"}]')
    assert systemd.parse_failed(stdout) == [
        {"unit": "caddy.service", "active": "failed", "sub": "failed", "description": "Caddy"}]
    assert systemd.parse_failed("[]") == []
