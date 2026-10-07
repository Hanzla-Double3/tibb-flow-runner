"""Workspace folder management for tibb-tester.

Each workspace is a directory in data/workspaces/<workspace_id>/ containing:
- workspace.json: { id, name, base_url, secrets, data }
- openapi.json: The workspace's own OpenAPI specification
- flows/: Directory of flow JSON blueprints and generated Python scripts
"""

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACES_ROOT = PROJECT_ROOT / "data" / "workspaces"
ACTIVE_WORKSPACE_FILE = WORKSPACES_ROOT / ".active_workspace"


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", text.strip().lower())
    return re.sub(r"^_+|__+|_\$", "", cleaned) or "workspace"


def get_workspace_dir(workspace_id: str) -> Path:
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACES_ROOT / workspace_id


def get_workspace_spec_path(workspace_id: str) -> Path:
    return get_workspace_dir(workspace_id) / "openapi.json"


def get_workspace_custom_apis_path(workspace_id: str) -> Path:
    return get_workspace_dir(workspace_id) / "custom_apis.json"


def get_workspace_flows_dir(workspace_id: str) -> Path:
    return get_workspace_dir(workspace_id) / "flows"


def ensure_default_workspace() -> Path:
    WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)
    default_dir = WORKSPACES_ROOT / "default"
    default_dir.mkdir(parents=True, exist_ok=True)

    config_path = default_dir / "workspace.json"
    if not config_path.exists():
        default_config = {
            "id": "default",
            "name": "Default Workspace",
            "base_url": os.environ.get("TIBB_BASE_URL", "http://localhost:8000"),
            "secrets": {},
            "data": {},
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        config_path.write_text(json.dumps(default_config, indent=2))

    spec_path = default_dir / "openapi.json"
    if not spec_path.exists() or spec_path.stat().st_size == 0:
        main_spec = PROJECT_ROOT / "config" / "specs" / "main_api.json"
        if main_spec.exists():
            spec_path.write_text(main_spec.read_text())

    custom_path = default_dir / "custom_apis.json"
    if not custom_path.exists():
        legacy_manual = PROJECT_ROOT / "data" / "specs" / "manual.spec"
        if legacy_manual.exists():
            try:
                custom_path.write_text(legacy_manual.read_text())
            except Exception:
                pass

    flows_dir = default_dir / "flows"
    if not flows_dir.exists():
        dashboard_flows = PROJECT_ROOT / "flows" / "dashboard"
        if dashboard_flows.exists():
            try:
                os.symlink(dashboard_flows, flows_dir, target_is_directory=True)
            except Exception:
                flows_dir.mkdir(parents=True, exist_ok=True)
                for f in dashboard_flows.glob("*.*"):
                    if f.is_file():
                        shutil.copy2(f, flows_dir / f.name)
        else:
            flows_dir.mkdir(parents=True, exist_ok=True)
            (flows_dir / "__init__.py").touch(exist_ok=True)

    return default_dir


def get_active_workspace_id() -> str:
    ensure_default_workspace()
    if ACTIVE_WORKSPACE_FILE.exists():
        try:
            ws_id = ACTIVE_WORKSPACE_FILE.read_text().strip()
            if ws_id and (WORKSPACES_ROOT / ws_id).is_dir():
                return ws_id
        except Exception:
            pass
    return "default"


def set_active_workspace_id(workspace_id: str) -> None:
    ensure_default_workspace()
    ws_dir = WORKSPACES_ROOT / workspace_id
    if not ws_dir.is_dir():
        raise FileNotFoundError(f"Workspace '{workspace_id}' does not exist")
    ACTIVE_WORKSPACE_FILE.write_text(workspace_id.strip())


def list_workspaces() -> list[dict]:
    ensure_default_workspace()
    results = []
    active_id = get_active_workspace_id()

    for item in sorted(WORKSPACES_ROOT.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        ws_id = item.name
        config_path = item / "workspace.json"
        config = {}
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
            except Exception:
                config = {}

        flows_dir = item / "flows"
        flow_count = 0
        if flows_dir.exists():
            flow_count = len(list(flows_dir.glob("*.flow.json")))

        spec_path = item / "openapi.json"
        has_spec = spec_path.exists() and spec_path.stat().st_size > 0

        results.append({
            "id": ws_id,
            "name": config.get("name", ws_id),
            "base_url": config.get("base_url", "http://localhost:8000"),
            "flow_count": flow_count,
            "has_spec": has_spec,
            "data_keys": list(config.get("data", {}).keys()),
            "secret_keys": list(config.get("secrets", {}).keys()),
            "is_active": ws_id == active_id,
            "updated_at": config.get("updated_at", 0),
        })
    return results


def load_workspace(workspace_id: str) -> dict:
    ensure_default_workspace()
    ws_dir = get_workspace_dir(workspace_id)
    if not ws_dir.exists():
        raise FileNotFoundError(f"Workspace '{workspace_id}' not found")

    config_path = ws_dir / "workspace.json"
    if not config_path.exists():
        config = {
            "id": workspace_id,
            "name": workspace_id,
            "base_url": "http://localhost:8000",
            "secrets": {},
            "data": {},
            "updated_at": time.time(),
        }
        config_path.write_text(json.dumps(config, indent=2))
    else:
        config = json.loads(config_path.read_text())

    flows_dir = ws_dir / "flows"
    flows = []
    if flows_dir.exists():
        flows = sorted(p.stem.removesuffix(".flow") for p in flows_dir.glob("*.flow.json"))

    spec_path = ws_dir / "openapi.json"
    has_spec = spec_path.exists() and spec_path.stat().st_size > 0

    return {
        "id": workspace_id,
        "name": config.get("name", workspace_id),
        "base_url": config.get("base_url", "http://localhost:8000"),
        "secrets": config.get("secrets", {}),
        "data": config.get("data", {}),
        "flow_names": flows,
        "flow_count": len(flows),
        "has_spec": has_spec,
        "is_active": workspace_id == get_active_workspace_id(),
        "created_at": config.get("created_at"),
        "updated_at": config.get("updated_at"),
    }


def create_workspace(
    workspace_id: str,
    name: Optional[str] = None,
    base_url: str = "http://localhost:8000",
    data: Optional[dict] = None,
    secrets: Optional[dict] = None,
    openapi_spec: Optional[Any] = None,
) -> dict:
    ensure_default_workspace()
    ws_id = _slugify(workspace_id)
    ws_dir = get_workspace_dir(ws_id)
    if ws_dir.exists() and (ws_dir / "workspace.json").exists():
        raise ValueError(f"Workspace '{ws_id}' already exists")

    ws_dir.mkdir(parents=True, exist_ok=True)
    flows_dir = ws_dir / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    (flows_dir / "__init__.py").touch(exist_ok=True)

    config = {
        "id": ws_id,
        "name": name or workspace_id,
        "base_url": base_url or "http://localhost:8000",
        "secrets": secrets or {},
        "data": data or {},
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    (ws_dir / "workspace.json").write_text(json.dumps(config, indent=2))

    spec_path = ws_dir / "openapi.json"
    if openapi_spec:
        if isinstance(openapi_spec, dict):
            spec_path.write_text(json.dumps(openapi_spec, indent=2))
        else:
            spec_path.write_text(str(openapi_spec))

    return load_workspace(ws_id)


def save_workspace(
    workspace_id: str,
    name: Optional[str] = None,
    base_url: Optional[str] = None,
    data: Optional[dict] = None,
    secrets: Optional[dict] = None,
    openapi_spec: Optional[Any] = None,
    flow_names: Optional[list[str]] = None,
) -> dict:
    ensure_default_workspace()
    ws_dir = get_workspace_dir(workspace_id)
    if not ws_dir.exists():
        return create_workspace(
            workspace_id=workspace_id,
            name=name,
            base_url=base_url or "http://localhost:8000",
            data=data,
            secrets=secrets,
            openapi_spec=openapi_spec,
        )

    config_path = ws_dir / "workspace.json"
    current = {}
    if config_path.exists():
        try:
            current = json.loads(config_path.read_text())
        except Exception:
            current = {}

    if name is not None:
        current["name"] = name
    if base_url is not None:
        current["base_url"] = base_url
    if data is not None:
        current["data"] = data
    if secrets is not None:
        current["secrets"] = secrets
    current["updated_at"] = time.time()

    config_path.write_text(json.dumps(current, indent=2))

    if openapi_spec is not None:
        spec_path = ws_dir / "openapi.json"
        if isinstance(openapi_spec, dict):
            spec_path.write_text(json.dumps(openapi_spec, indent=2))
        else:
            spec_path.write_text(str(openapi_spec))

    return load_workspace(workspace_id)


def delete_workspace(workspace_id: str) -> None:
    if workspace_id == "default":
        raise ValueError("Cannot delete the default workspace")
    ws_dir = get_workspace_dir(workspace_id)
    if ws_dir.exists():
        shutil.rmtree(ws_dir, ignore_errors=True)
    if get_active_workspace_id() == workspace_id:
        set_active_workspace_id("default")
