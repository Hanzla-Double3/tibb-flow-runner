"""Persists flows: the graph JSON (the editable blueprint, whether built by
hand, by the dashboard, or by the assistant) plus the generated .py file
(what the CLI runner actually executes). Scoped per-workspace while maintaining
full backward-compatibility with flows/dashboard."""

import json
import time
from pathlib import Path
from typing import Optional

from core.codegen import generate_flow_source
from core import workspace_store

DEFAULT_FLOWS_DIR = Path(__file__).resolve().parent.parent / "flows" / "dashboard"
FLOWS_DIR = DEFAULT_FLOWS_DIR


def _get_flows_dir(workspace_id: Optional[str] = None) -> Path:
    # If FLOWS_DIR has been monkeypatched in tests, honor it directly
    if FLOWS_DIR != DEFAULT_FLOWS_DIR:
        FLOWS_DIR.mkdir(parents=True, exist_ok=True)
        (FLOWS_DIR / "__init__.py").touch(exist_ok=True)
        return FLOWS_DIR

    if workspace_id:
        fdir = workspace_store.get_workspace_flows_dir(workspace_id)
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "__init__.py").touch(exist_ok=True)
        return fdir

    active_id = workspace_store.get_active_workspace_id()
    fdir = workspace_store.get_workspace_flows_dir(active_id)
    fdir.mkdir(parents=True, exist_ok=True)
    (fdir / "__init__.py").touch(exist_ok=True)
    return fdir


def _paths(name: str, workspace_id: Optional[str] = None) -> tuple[Path, Path]:
    fdir = _get_flows_dir(workspace_id)
    return fdir / f"{name}.flow.json", fdir / f"{name}.py"


def list_flows(workspace_id: Optional[str] = None) -> list[str]:
    fdir = _get_flows_dir(workspace_id)
    flows = {p.stem.removesuffix(".flow") for p in fdir.glob("*.flow.json")}
    effective_ws = workspace_id or workspace_store.get_active_workspace_id()
    if effective_ws == "default" and DEFAULT_FLOWS_DIR.exists():
        flows.update(p.stem.removesuffix(".flow") for p in DEFAULT_FLOWS_DIR.glob("*.flow.json"))
    return sorted(flows)


class FlowConflictError(Exception):
    """Raised when save_flow detects a concurrent write conflict."""
    pass


def save_flow(name: str, graph: dict, expected_version: int | None = None, workspace_id: Optional[str] = None) -> dict:
    json_path, py_path = _paths(name, workspace_id)
    current_version = 0
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text())
            current_version = existing.get("version", 0)
        except Exception:
            current_version = 0

    if expected_version is not None and current_version > expected_version:
        raise FlowConflictError(
            f"Flow '{name}' has been updated on the server (current version: {current_version}, expected: {expected_version})"
        )

    new_version = max(current_version, graph.get("version", 0)) + 1
    updated_graph = {
        **graph,
        "name": name,
        "version": new_version,
        "updated_at": time.time(),
    }
    json_path.write_text(json.dumps(updated_graph, indent=2))
    py_path.write_text(generate_flow_source(updated_graph))

    # Also keep DEFAULT_FLOWS_DIR in sync only if explicitly writing to default workspace
    effective_ws = workspace_id or workspace_store.get_active_workspace_id()
    if effective_ws == "default" and DEFAULT_FLOWS_DIR.exists() and json_path.parent != DEFAULT_FLOWS_DIR:
        try:
            (DEFAULT_FLOWS_DIR / f"{name}.flow.json").write_text(json.dumps(updated_graph, indent=2))
            (DEFAULT_FLOWS_DIR / f"{name}.py").write_text(generate_flow_source(updated_graph))
        except Exception:
            pass

    return updated_graph


def load_flow(name: str, workspace_id: Optional[str] = None) -> dict:
    json_path, _ = _paths(name, workspace_id)
    effective_ws = workspace_id or workspace_store.get_active_workspace_id()
    if not json_path.exists():
        if effective_ws == "default":
            fallback_json = DEFAULT_FLOWS_DIR / f"{name}.flow.json"
            if fallback_json.exists():
                json_path = fallback_json
            else:
                raise FileNotFoundError(name)
        else:
            raise FileNotFoundError(name)
    data = json.loads(json_path.read_text())
    if "version" not in data:
        data["version"] = 1
    return data


def delete_flow(name: str, workspace_id: Optional[str] = None) -> None:
    json_path, py_path = _paths(name, workspace_id)
    json_path.unlink(missing_ok=True)
    py_path.unlink(missing_ok=True)
    effective_ws = workspace_id or workspace_store.get_active_workspace_id()
    if effective_ws == "default" and DEFAULT_FLOWS_DIR.exists():
        (DEFAULT_FLOWS_DIR / f"{name}.flow.json").unlink(missing_ok=True)
        (DEFAULT_FLOWS_DIR / f"{name}.py").unlink(missing_ok=True)
