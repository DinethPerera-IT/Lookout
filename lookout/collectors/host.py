"""Local snapshot of disk, memory and load. Works even when Prometheus is down."""
import os
import shutil
from pathlib import Path


def parse_meminfo(text):
    """Parse /proc/meminfo into {field: bytes}."""
    values = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        number = int(parts[0])
        values[name] = number * 1024 if len(parts) > 1 and parts[1] == "kB" else number
    return values


def disk(mountpoint):
    try:
        usage = shutil.disk_usage(mountpoint)
    except OSError as e:
        return {"mountpoint": mountpoint, "error": str(e)}
    # Same formula as df's Use%: blocks reserved for root count as neither used nor available.
    used_pct = 100 * usage.used / (usage.used + usage.free) if usage.used + usage.free else 0.0
    return {"mountpoint": mountpoint, "used_pct": round(used_pct, 1), "avail_bytes": usage.free}


def boot_time(stat_text):
    """Boot time (epoch seconds) from /proc/stat's btime line."""
    for line in stat_text.splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return None


def collect(cfg, meminfo_path="/proc/meminfo", stat_path="/proc/stat"):
    mem = parse_meminfo(Path(meminfo_path).read_text())
    load1, load5, load15 = os.getloadavg()
    return {
        "ok": True,
        "disks": [disk(m) for m in cfg["host"]["mounts"]],
        "memory": {
            "total_bytes": mem["MemTotal"],
            "available_bytes": mem["MemAvailable"],
            "available_pct": round(100 * mem["MemAvailable"] / mem["MemTotal"], 1),
            "swap_used_bytes": mem["SwapTotal"] - mem["SwapFree"],
        },
        "load": {"load1": load1, "load5": load5, "load15": load15, "cpus": os.cpu_count()},
        "boot_time": boot_time(Path(stat_path).read_text()),
    }
