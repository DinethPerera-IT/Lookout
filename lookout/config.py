"""Configuration loading. Thresholds live in config.toml; secrets never do."""
import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
LOCAL_NAME = "config.local.toml"   # next to config.toml, gitignored: settings specific to one machine
SECRETS = ("DISCORD_WEBHOOK_URL", "KUMA_PUSH_URL")


def load(path=None):
    """config.toml, with config.local.toml (same folder, if present) layered on top."""
    path = Path(path or DEFAULT_CONFIG)
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    local = path.with_name(LOCAL_NAME)
    if local.exists():
        with open(local, "rb") as f:
            merge(cfg, tomllib.load(f))
    return cfg


def merge(base, override):
    """Tables merge key by key; any other value (including lists) replaces the base value."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def load_env_file(path):
    """Read KEY=VALUE lines into os.environ for manual runs (systemd uses EnvironmentFile=).

    Values already in the environment win. Nothing is printed.
    """
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def secret(name):
    """A secret from the environment, or None. Never log the returned value."""
    value = os.environ.get(name, "").strip()
    return value if value and "CHANGE_ME" not in value else None


def discord_enabled(cfg):
    """[notify] discord = false turns Discord off entirely (dashboard only). Default: on."""
    return bool(cfg.get("notify", {}).get("discord", True))


def missing_secrets(names=SECRETS):
    return [n for n in names if secret(n) is None]
