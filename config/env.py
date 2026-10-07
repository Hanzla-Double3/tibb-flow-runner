"""Loads a local .env file into os.environ, if present. Real environment
variables always take precedence over .env values (setdefault, not
overwrite), so `FOO=bar python x.py` still wins over a .env entry."""

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or (_ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
