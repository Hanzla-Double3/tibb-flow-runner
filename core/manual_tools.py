"""Workspace-aware custom API operation registry.

Allows users and AI agents to hand-define API operations without a full OpenAPI spec.
Stored per-workspace in `data/workspaces/<workspace_id>/custom_apis.json` so that
every workspace has its own isolated tools and APIs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from core import workspace_store

MANUAL_SPEC_ID = "manual"
MANUAL_SPEC_TITLE = "Manual tools"

_PATH_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _empty_spec(title: str = MANUAL_SPEC_TITLE) -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": title, "version": "1.0"},
        "paths": {},
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
    }


def _get_target_file(workspace_id: Optional[str] = None) -> Path:
    ws_id = workspace_id or workspace_store.get_active_workspace_id()
    return workspace_store.get_workspace_custom_apis_path(ws_id)


def load_custom_spec(workspace_id: Optional[str] = None) -> dict:
    target = _get_target_file(workspace_id)
    if target.exists():
        try:
            return json.loads(target.read_text())
        except Exception:
            pass
    return _empty_spec()


# Backward-compatibility alias for test suites
_load = load_custom_spec


def save_custom_spec(spec: dict, workspace_id: Optional[str] = None) -> None:
    target = _get_target_file(workspace_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(spec, indent=2))


def add_manual_operation(
    *,
    operation_id: str,
    tag: str,
    method: str,
    path: str,
    query_params: list[str] | None = None,
    body_fields: list[str] | None = None,
    requires_auth: bool = False,
    content_type: str = "application/json",
    summary: str = "",
    description: str = "",
    workspace_id: Optional[str] = None,
) -> str:
    """Adds an API operation to the specified (or active) workspace."""
    spec = load_custom_spec(workspace_id)

    op: dict = {
        "operationId": operation_id,
        "tags": [tag or "manual"],
    }
    if summary:
        op["summary"] = summary
    if description:
        op["description"] = description

    parameters = []
    for name in _PATH_PARAM_RE.findall(path):
        parameters.append({"name": name, "in": "path", "required": True, "schema": {"type": "string"}})
    for name in (query_params or []):
        name = name.strip()
        if name:
            parameters.append({"name": name, "in": "query", "required": False, "schema": {"type": "string"}})
    if parameters:
        op["parameters"] = parameters

    fields = [f.strip() for f in (body_fields or []) if f.strip()]
    if fields:
        ct = content_type or "application/json"
        if ct == "multipart/form-data":
            properties = {}
            for f in fields:
                if f in ("file", "files", "attachment", "document", "upload", "image") or f.endswith("_file"):
                    properties[f] = {"type": "string", "format": "binary"}
                else:
                    properties[f] = {"type": "string"}
            op["requestBody"] = {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "properties": properties,
                        }
                    }
                },
            }
        else:
            op["requestBody"] = {
                "required": True,
                "content": {
                    ct: {
                        "schema": {
                            "type": "object",
                            "properties": {f: {"type": "string"} for f in fields},
                        }
                    }
                },
            }

    if requires_auth:
        op["security"] = [{"bearerAuth": []}]

    spec.setdefault("paths", {}).setdefault(path, {})[method.lower()] = op
    save_custom_spec(spec, workspace_id)
    tool_tag = tag or "manual"
    return f"{tool_tag}__{operation_id}"


def remove_manual_operation(operation_id: str, workspace_id: Optional[str] = None) -> bool:
    """Removes a manually defined operation by operation_id or tool name."""
    spec = load_custom_spec(workspace_id)
    paths = spec.get("paths", {})
    removed = False

    clean_op_id = operation_id.split("__")[-1]

    for path, methods in list(paths.items()):
        if not isinstance(methods, dict):
            continue
        for m, op in list(methods.items()):
            if isinstance(op, dict):
                cur_id = op.get("operationId", "")
                if cur_id == operation_id or cur_id == clean_op_id:
                    del methods[m]
                    removed = True
        if not methods:
            del paths[path]

    if removed:
        save_custom_spec(spec, workspace_id)
    return removed


def list_manual_operations(workspace_id: Optional[str] = None) -> list[dict]:
    """Lists summary of manually added operations in this workspace."""
    spec = load_custom_spec(workspace_id)
    results = []
    for path, methods in spec.get("paths", {}).items():
        if not isinstance(methods, dict):
            continue
        for m, op in methods.items():
            if isinstance(op, dict):
                op_id = op.get("operationId", "")
                tag = (op.get("tags") or ["manual"])[0]
                results.append({
                    "name": f"{tag}__{op_id}",
                    "operation_id": op_id,
                    "tag": tag,
                    "method": m.upper(),
                    "path": path,
                    "summary": op.get("summary", ""),
                    "description": op.get("description", ""),
                })
    return results
