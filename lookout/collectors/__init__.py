"""Read-only data collectors. Each exposes collect(cfg) -> dict with an "ok" key."""
import subprocess


class CollectorError(Exception):
    pass


def run(cmd, timeout):
    """Run a read-only command and return stdout, raising CollectorError on any failure."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise CollectorError(f"{cmd[0]} not found")
    except subprocess.TimeoutExpired:
        raise CollectorError(f"{cmd[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        raise CollectorError(f"{cmd[0]} exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout
