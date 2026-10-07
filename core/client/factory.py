"""Single place that turns Workspace + OpenAPI specs into a ready-to-use
API client namespace. Supports per-workspace base_url, secrets, openapi.json, and custom_apis.json.
Used by the runner CLI, the UI backend, the assistant, and MCP tools alike."""

from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from config.settings import SPEC_FILES_OVERRIDE, SPEC_TAGS, load_settings
from core import workspace_store
from core.client.base_client import BaseClient
from core.client.dynamic import load_client


def resolve_spec_files(workspace_id: Optional[str] = None) -> list[str]:
    """Resolves OpenAPI specs belonging strictly to the specified workspace."""
    if SPEC_FILES_OVERRIDE is not None:
        return SPEC_FILES_OVERRIDE
    # A clean checkout only tracks the workspace root.  Ensure the generated
    # default workspace/spec exists even when callers explicitly request it
    # (rather than reaching it via get_active_workspace_id()).
    workspace_store.ensure_default_workspace()
    files = []
    ws_id = workspace_id or workspace_store.get_active_workspace_id()

    # Workspace-scoped OpenAPI specification
    ws_spec = workspace_store.get_workspace_spec_path(ws_id)
    if ws_spec.exists() and ws_spec.stat().st_size > 0:
        files.append(str(ws_spec))

    # Workspace-scoped custom APIs (manually added endpoints)
    custom_spec = workspace_store.get_workspace_custom_apis_path(ws_id)
    if custom_spec.exists() and custom_spec.stat().st_size > 0:
        files.append(str(custom_spec))

    return files


def build_api_client(workspace_id: Optional[str] = None, base_url: Optional[str] = None) -> SimpleNamespace:
    settings = load_settings()
    ws_id = workspace_id or workspace_store.get_active_workspace_id()
    ws = {}
    try:
        ws = workspace_store.load_workspace(ws_id)
    except Exception:
        pass

    effective_base_url = (
        base_url
        or ws.get("base_url")
        or settings.base_url
    )
    secrets = ws.get("secrets") or {}
    effective_api_key = (
        secrets.get("testing_api_key")
        or secrets.get("api_key")
        or settings.testing_api_key
    )

    client = BaseClient(base_url=effective_base_url, testing_api_key=effective_api_key)

    spec_files = resolve_spec_files(ws_id)
    return load_client(spec_files, client, tags=SPEC_TAGS)
