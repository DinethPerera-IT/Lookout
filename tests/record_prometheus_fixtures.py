"""Re-record tests/fixtures/prometheus/*.json from the live Prometheus (read-only queries).

Run: .venv/bin/python -m tests.record_prometheus_fixtures
"""
import urllib.parse
from pathlib import Path

from ops_summary import config
from ops_summary.collectors import prometheus

OUT = Path(__file__).parent / "fixtures" / "prometheus"


def main():
    cfg = config.load()
    prom = cfg["prometheus"]
    for name, expr in prometheus.build_queries(cfg).items():
        url = f"{prom['url']}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        (OUT / f"{name}.json").write_bytes(prometheus.http_get(url, prom["timeout_seconds"]))
        print(f"recorded {name}")


if __name__ == "__main__":
    main()
