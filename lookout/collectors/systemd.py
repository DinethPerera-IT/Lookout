"""Failed systemd units, via `systemctl --failed` (works as any user)."""
import json

from . import run


def parse_failed(stdout):
    return [
        {"unit": u["unit"], "active": u["active"], "sub": u["sub"], "description": u["description"]}
        for u in json.loads(stdout or "[]")
    ]


def collect(cfg):
    stdout = run(["systemctl", "--failed", "--output=json", "--no-pager"], timeout=15)
    return {"ok": True, "failed_units": parse_failed(stdout)}
