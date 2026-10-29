import json
import urllib.parse
from pathlib import Path

import pytest

from lookout.__main__ import collect_all
from lookout.collectors import CollectorError, prometheus

FIXTURES = Path(__file__).parent / "fixtures" / "prometheus"


def recorded_fetch(cfg):
    """Serve recorded Prometheus responses, keyed by which query was asked for."""
    by_expr = {expr: name for name, expr in prometheus.build_queries(cfg).items()}

    def fetch(url, timeout):
        expr = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0]
        return (FIXTURES / f"{by_expr[expr]}.json").read_bytes()

    return fetch


def body(result, status="success"):
    return json.dumps({"status": status, "data": {"resultType": "vector", "result": result}})


def test_collect_parses_recorded_responses(cfg):
    facts = prometheus.collect(cfg, fetch=recorded_fetch(cfg))
    assert facts["ok"] is True
    assert facts["targets"] == [{"job": "node-exporter", "instance": "localhost:9100", "up": True}]
    assert facts["disk_used_pct"] == {"/": pytest.approx(31.806, abs=0.001)}
    assert facts["disk_avail_predicted_bytes"] == {"/": pytest.approx(1.682e11, rel=1e-3)}
    assert facts["mem_min_available_pct"] == pytest.approx(75.213, abs=0.001)
    assert facts["swap_max_used_bytes"] == 0.0
    assert facts["load15_max"] == 4.12


def test_down_target_reported():
    fetch = lambda url, timeout: body([{"metric": {"job": "node-exporter", "instance": "x"},
                                        "value": [0, "0"]}])
    results = prometheus.query("http://p", "up", 1, fetch)
    assert results == [({"job": "node-exporter", "instance": "x"}, 0.0)]


def test_nan_becomes_none():
    fetch = lambda url, timeout: body([{"metric": {}, "value": [0, "NaN"]}])
    assert prometheus.query("http://p", "x", 1, fetch) == [({}, None)]


def test_empty_result_gives_none():
    assert prometheus.first_value([]) is None


def test_error_status_raises():
    fetch = lambda url, timeout: json.dumps({"status": "error", "error": "parse error"})
    with pytest.raises(CollectorError, match="parse error"):
        prometheus.query("http://p", "bad(", 1, fetch)


def test_unreachable_prometheus_becomes_a_fact(cfg):
    cfg["prometheus"]["url"] = "http://127.0.0.1:1"  # nothing listens on port 1
    facts = collect_all(cfg, {"prometheus": prometheus.collect})
    assert facts["prometheus"]["ok"] is False
    assert "unreachable" in facts["prometheus"]["error"]
