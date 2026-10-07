"""Persistent registry of OpenAPI specs the framework builds tools from.

Replaces the old env-var-configured spec file list: specs are registered at
runtime (imported via the dashboard, or written here directly) and every
consumer -- the dashboard's tool list, the CLI runner, the assistant -- reads
the same registry, so there is exactly one source of truth for "what tools
exist" regardless of who's asking.
"""

import json
from pathlib import Path

STORE_DIR = Path(__file__).resolve().parent.parent / "data" / "specs"


def _registry_path() -> Path:
    return STORE_DIR / "_registry.json"


def _load_registry() -> dict:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = _registry_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _save_registry(registry: dict) -> None:
    _registry_path().write_text(json.dumps(registry, indent=2))


def _content_path(spec_id: str) -> Path:
    return STORE_DIR / f"{spec_id}.spec"


def list_specs() -> list[dict]:
    registry = _load_registry()
    return [{"id": sid, **meta} for sid, meta in sorted(registry.items())]


def get_spec_content(spec_id: str) -> str | None:
    path = _content_path(spec_id)
    return path.read_text() if path.exists() else None


def add_spec(spec_id: str, title: str, content: str) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    _content_path(spec_id).write_text(content)
    registry = _load_registry()
    registry[spec_id] = {"title": title}
    _save_registry(registry)


def remove_spec(spec_id: str) -> None:
    registry = _load_registry()
    registry.pop(spec_id, None)
    _save_registry(registry)
    _content_path(spec_id).unlink(missing_ok=True)


def spec_file_paths() -> list[str]:
    """All registered spec files, as paths -- what core.client.dynamic consumes."""
    registry = _load_registry()
    return [str(_content_path(sid)) for sid in registry.keys()]
