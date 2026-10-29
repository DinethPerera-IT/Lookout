"""Metric history from the Prometheus HTTP API (node-exporter).

Window functions (min/max_over_time) catch spikes between runs that a
point-in-time `free` or `df` would miss.
"""
import json
import math
import urllib.error
import urllib.parse
import urllib.request

from . import CollectorError


def build_queries(cfg):
    window = f"{cfg['window_minutes']}m"
    days = cfg["thresholds"]["disk_full_within_days"]
    sel = '{fstype!~"%s"}' % "|".join(cfg["prometheus"]["exclude_fstypes"])
    size, free, avail = (f"node_filesystem_{m}_bytes{sel}" for m in ("size", "free", "avail"))
    return {
        "targets": "up",
        # Same formula as df's Use%
        "disk_used_pct": f"100 * ({size} - {free}) / ({size} - {free} + {avail})",
        "disk_avail_predicted_bytes": f"predict_linear({avail}[6h], {days} * 86400)",
        "mem_min_available_pct": f"100 * min_over_time(node_memory_MemAvailable_bytes[{window}])"
                                 " / node_memory_MemTotal_bytes",
        "swap_max_used_bytes": "max_over_time((node_memory_SwapTotal_bytes"
                               f" - node_memory_SwapFree_bytes)[{window}:1m])",
        "load15_max": f"max_over_time(node_load15[{window}])",
    }


def http_get(url, timeout):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise CollectorError(f"Prometheus unreachable: {e}")


def query(base_url, expr, timeout, fetch=http_get):
    """Run an instant query; return [(labels, value)] with NaN/Inf mapped to None."""
    url = f"{base_url}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    body = json.loads(fetch(url, timeout))
    if body.get("status") != "success":
        raise CollectorError(f"Prometheus query failed: {body.get('error', 'unknown error')}")
    results = []
    for r in body["data"]["result"]:
        value = float(r["value"][1])
        results.append((r["metric"], value if math.isfinite(value) else None))
    return results


def by_mountpoint(results):
    return {labels.get("mountpoint", "?"): value for labels, value in results}


def first_value(results):
    return results[0][1] if results else None


def collect(cfg, fetch=http_get):
    prom = cfg["prometheus"]
    queries = build_queries(cfg)

    def q(name):
        return query(prom["url"], queries[name], prom["timeout_seconds"], fetch)

    return {
        "ok": True,
        "targets": [{"job": labels.get("job"), "instance": labels.get("instance"), "up": value == 1}
                    for labels, value in q("targets")],
        "disk_used_pct": by_mountpoint(q("disk_used_pct")),
        "disk_avail_predicted_bytes": by_mountpoint(q("disk_avail_predicted_bytes")),
        "mem_min_available_pct": first_value(q("mem_min_available_pct")),
        "swap_max_used_bytes": first_value(q("swap_max_used_bytes")),
        "load15_max": first_value(q("load15_max")),
    }
