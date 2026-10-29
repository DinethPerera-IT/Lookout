"""Configuration loading. Thresholds live in config.toml; secrets never do."""
import os
import tomllib
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"
SECRETS = ("DISCORD_WEBHOOK_URL", "KUMA_PUSH_URL")


def load(path=None):
    with open(path or DEFAULT_CONFIG, "rb") as f:
        return tomllib.load(f)


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


def missing_secrets(names=SECRETS):
    return [n for n in names if secret(n) is None]
