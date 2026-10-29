"""Configuration loading. Thresholds live in config.toml; secrets never do."""
import tomllib
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


def load(path=None):
    with open(path or DEFAULT_CONFIG, "rb") as f:
        return tomllib.load(f)
