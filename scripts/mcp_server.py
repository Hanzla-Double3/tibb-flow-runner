#!/usr/bin/env python3
"""Model Context Protocol (MCP) Server for tibb-tester.

Provides tools for AI coding agents (Claude Code, Gemini CLI, Cursor, etc.)
to manage workspaces, create, edit, compose, clean up, and execute QA test flows.
Can be run via stdio (for Claude Code, CLI agents) or SSE (for remote clients).

Usage:
  # Stdio mode (for Claude Code):
  ./.venv/bin/python scripts/mcp_server.py

  # Add to Claude Code:
  claude mcp add tibb-tester /path/to/tibb_tester/.venv/bin/python /path/to/tibb_tester/scripts/mcp_server.py

  # SSE mode:
  ./.venv/bin/python scripts/mcp_server.py --sse --port 8805
"""

import argparse
import asyncio
import inspect
import json
import os
import random
import string
import sys
import warnings
from dataclasses import asdict
from typing import Any, Optional

# Suppress protected namespace warning from pydantic v2
warnings.filterwarnings("ignore", message=".*protected namespace.*")

# ---------------------------------------------------------------------------
# Pydantic < 2.10 compatibility patch for MCP 2.x SDK
# MCP 2.x passes by_name=False to validate_json, validate_python, and
# model_validate. In Pydantic < 2.10 this raises a TypeError which causes the
# stdio reader to drop all JSON-RPC messages and hang during handshake.
# ---------------------------------------------------------------------------
from pydantic import BaseModel, TypeAdapter

if "by_name" not in inspect.signature(TypeAdapter.validate_json).parameters:
    _orig_validate_json = TypeAdapter.validate_json

    def _patched_validate_json(self, *args, **kwargs):
        kwargs.pop("by_name", None)
        return _orig_validate_json(self, *args, **kwargs)

    TypeAdapter.validate_json = _patched_validate_json

if "by_name" not in inspect.signature(TypeAdapter.validate_python).parameters:
    _orig_validate_python = TypeAdapter.validate_python

    def _patched_validate_python(self, *args, **kwargs):
        kwargs.pop("by_name", None)
        return _orig_validate_python(self, *args, **kwargs)

    TypeAdapter.validate_python = _patched_validate_python

if "by_name" not in inspect.signature(BaseModel.model_validate).parameters:
    _orig_model_validate = BaseModel.model_validate

    @classmethod
    def _patched_model_validate(cls, *args, **kwargs):
        kwargs.pop("by_name", None)
        return _orig_model_validate.__func__(cls, *args, **kwargs)

    BaseModel.model_validate = _patched_model_validate

# Add project root to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mcp.server.mcpserver import MCPServer
from core import flow_store as store
from core import manual_tools, workspace_store
from core.client.factory import build_api_client
from core.graph_builder import make_flow_fn, run_single_node
from core.flow_validation import validate_graph
from runner.executor import run_flow as execute_flow
from runner.workspace import run_workspace_flows
from ui_server.tools_info import list_tools as list_api_tools

app = MCPServer("tibb-tester")


class ToolResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None


def _new_id() -> str:
    return "n" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _find_node(graph: dict, node_id: str):
    def walk(items):
        for item in items:
            if item.get("id") == node_id:
                return item, items
            if item.get("kind") == "call":
                found = walk(item.get("validate", []))
                if found[0] is not None:
                    return found
            elif item.get("kind") == "condition":
                found = walk(item.get("then_steps", []))
                if found[0] is not None:
                    return found
                found = walk(item.get("else_steps", []))
                if found[0] is not None:
                    return found
            elif item.get("kind") == "split":
                for b in item.get("branches", []):
                    found = walk(b.get("steps", []))
                    if found[0] is not None:
                        return found
        return None, None

    found = walk(graph.get("steps", []))
    if found[0] is not None:
        return found
    return walk(graph.get("finally_steps", graph.get("teardown_steps", [])))


# ===========================================================================
# Workspace Management Tools
# ===========================================================================

@app.tool()
def list_workspaces() -> ToolResponse:
    """List all workspace folders with their base_url, flows count, spec status, and key-value keys."""
    try:
        workspaces = workspace_store.list_workspaces()
        return ToolResponse(success=True, data=workspaces)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def get_workspace(workspace_id: Optional[str] = None) -> ToolResponse:
    """Get full details of a workspace (or active workspace if omitted): base_url, secrets, data dict, and flows."""
    try:
        ws_id = workspace_id or workspace_store.get_active_workspace_id()
        ws = workspace_store.load_workspace(ws_id)
        # Mask secrets in returned data for safety
        safe_secrets = {k: ("***" if v else "") for k, v in ws.get("secrets", {}).items()}
        data = {**ws, "secrets_masked": safe_secrets}
        return ToolResponse(success=True, data=data)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def create_workspace(
    workspace_id: str,
    name: Optional[str] = None,
    base_url: str = "http://localhost:8000",
    data: Optional[dict] = None,
    secrets: Optional[dict] = None,
    openapi_spec: Optional[str] = None,
) -> ToolResponse:
    """Create a new workspace folder with its own openapi.json, flows/ folder, base_url, secrets, and key-value data.

    Args:
        workspace_id: Unique slug identifier for the workspace folder (e.g. 'staging', 'auth_service').
        name: Human-friendly display name (e.g. 'Staging Environment').
        base_url: Base API URL for all requests made within this workspace (e.g. 'https://api.staging.example.com').
        data: Workspace-level key-value dictionary shared across all flows ($data.key or $workspace.key).
        secrets: Sensitive credentials/tokens dictionary ($secret.key).
        openapi_spec: Optional OpenAPI JSON or YAML string specific to this workspace.
    """
    try:
        created = workspace_store.create_workspace(
            workspace_id=workspace_id,
            name=name,
            base_url=base_url,
            data=data,
            secrets=secrets,
            openapi_spec=openapi_spec,
        )
        return ToolResponse(success=True, data=created)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def update_workspace(
    workspace_id: Optional[str] = None,
    name: Optional[str] = None,
    base_url: Optional[str] = None,
    data: Optional[dict] = None,
    secrets: Optional[dict] = None,
    openapi_spec: Optional[str] = None,
) -> ToolResponse:
    """Update an existing workspace's name, base_url, data dictionary, secrets, or OpenAPI specification.

    Args:
        workspace_id: Target workspace ID (defaults to active workspace).
        name: New display name.
        base_url: New base URL.
        data: Updated workspace-level key-value dictionary.
        secrets: Updated workspace-level secrets dictionary.
        openapi_spec: Updated OpenAPI specification string.
    """
    try:
        ws_id = workspace_id or workspace_store.get_active_workspace_id()
        updated = workspace_store.save_workspace(
            workspace_id=ws_id,
            name=name,
            base_url=base_url,
            data=data,
            secrets=secrets,
            openapi_spec=openapi_spec,
        )
        return ToolResponse(success=True, data=updated)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def switch_active_workspace(workspace_id: str) -> ToolResponse:
    """Set the active workspace for subsequent flow and endpoint operations."""
    try:
        workspace_store.set_active_workspace_id(workspace_id)
        ws = workspace_store.load_workspace(workspace_id)
        return ToolResponse(success=True, data={"active_workspace": workspace_id, "name": ws.get("name")})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def delete_workspace(workspace_id: str) -> ToolResponse:
    """Delete a workspace folder and all its contents (cannot delete default workspace)."""
    try:
        workspace_store.delete_workspace(workspace_id)
        return ToolResponse(success=True, data={"deleted": workspace_id})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


# ===========================================================================
# Endpoint Discovery Tools
# ===========================================================================

@app.tool()
def list_available_api_endpoints(
    workspace_id: Optional[str] = None,
    cursor: int = 0,
    limit: int = 25,
    query: Optional[str] = None,
) -> ToolResponse:
    """Return one page of API endpoints, avoiding oversized SSE/MCP events.

    Use ``next_cursor`` from the response for the next page. ``query`` filters
    by endpoint name, path, tag, or summary before pagination.
    """
    try:
        api_tools = list_api_tools(workspace_id)
        if query:
            needle = query.lower().strip()
            api_tools = [tool for tool in api_tools if needle in " ".join(str(tool.get(k, "")) for k in ("name", "path", "tag", "summary")).lower()]
        start = max(0, int(cursor))
        size = min(100, max(1, int(limit)))
        items = api_tools[start:start + size]
        next_cursor = start + size if start + size < len(api_tools) else None
        return ToolResponse(success=True, data={"items": items, "next_cursor": next_cursor, "total": len(api_tools)})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def describe_api_endpoint(tool_name: str, workspace_id: Optional[str] = None) -> ToolResponse:
    """Get the full resolved OpenAPI JSON schema, parameters, nested request body shapes, enums, and responses for an endpoint.

    Args:
        tool_name: Exact tool name (e.g. 'lab-orders__place_order_api_v1_lab_orders_post') or partial path/operation identifier.
        workspace_id: Optional workspace ID to resolve spec from.
    """
    try:
        tools = list_api_tools(workspace_id)
        target = None
        # 1. Exact match
        for t in tools:
            if t["name"] == tool_name:
                target = t
                break
        # 2. Case-insensitive or substring match
        if not target:
            clean = tool_name.lower().strip()
            for t in tools:
                if (
                    t["name"].lower() == clean or
                    t["name"].lower().endswith(clean) or
                    clean in t["name"].lower() or
                    clean == t.get("path", "").lower()
                ):
                    target = t
                    break

        if not target:
            return ToolResponse(
                success=False,
                error=f"Endpoint '{tool_name}' not found. Use list_available_api_endpoints() to see available endpoint names.",
            )

        return ToolResponse(success=True, data=target)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def create_manual_api_tool(
    operation_id: str,
    method: str,
    path: str,
    tag: str = "manual",
    query_params: Optional[list[str]] = None,
    body_fields: Optional[list[str]] = None,
    requires_auth: bool = False,
    content_type: str = "application/json",
    summary: str = "",
    description: str = "",
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Create a manual API tool/endpoint in a workspace not present in imported OpenAPI specs.

    Args:
        operation_id: Unique operation ID for the tool (e.g. 'upload_profile_photo').
        method: HTTP method: GET, POST, PUT, PATCH, DELETE.
        path: API path template, e.g. '/api/v1/users/{id}/avatar'.
        tag: Category tag (default 'manual').
        query_params: List of query parameter names.
        body_fields: List of body field names. For multipart, field names become form-data fields.
        requires_auth: Whether testing_api_key / auth header is required.
        content_type: 'application/json', 'multipart/form-data', or 'application/x-www-form-urlencoded'.
        summary: Short summary of what this endpoint does.
        description: Detailed description of the endpoint.
        workspace_id: Optional workspace ID to add this tool to (defaults to active workspace).
    """
    try:
        tool_name = manual_tools.add_manual_operation(
            workspace_id=workspace_id,
            operation_id=operation_id,
            method=method,
            path=path,
            tag=tag,
            query_params=query_params or [],
            body_fields=body_fields or [],
            requires_auth=requires_auth,
            content_type=content_type,
            summary=summary,
            description=description,
        )
        return ToolResponse(success=True, data={"tool_name": tool_name, "content_type": content_type})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_api_endpoint(
    operation_id: str,
    method: str,
    path: str,
    tag: str = "manual",
    query_params: Optional[list[str]] = None,
    body_fields: Optional[list[str]] = None,
    requires_auth: bool = False,
    content_type: str = "application/json",
    summary: str = "",
    description: str = "",
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add a custom API endpoint/tool directly to a workspace.

    Each API in an OpenAPI spec is an individual tool; this tool allows you to manually
    add new endpoints to the workspace's collection of available APIs.
    """
    return create_manual_api_tool(
        operation_id=operation_id,
        method=method,
        path=path,
        tag=tag,
        query_params=query_params,
        body_fields=body_fields,
        requires_auth=requires_auth,
        content_type=content_type,
        summary=summary,
        description=description,
        workspace_id=workspace_id,
    )


@app.tool()
def remove_manual_api_tool(
    operation_id: str,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Remove a manually added API tool/endpoint from a workspace.

    Args:
        operation_id: The operation ID or tool name (e.g. 'upload_profile_photo' or 'manual__upload_profile_photo').
        workspace_id: Optional workspace ID (defaults to active workspace).
    """
    try:
        removed = manual_tools.remove_manual_operation(operation_id, workspace_id=workspace_id)
        if not removed:
            return ToolResponse(success=False, error=f"Manual operation '{operation_id}' not found in workspace.")
        return ToolResponse(success=True, data={"removed": operation_id})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


# ===========================================================================
# Flow Management Tools
# ===========================================================================

@app.tool()
def list_saved_flows(workspace_id: Optional[str] = None) -> ToolResponse:
    """List names of all saved QA test flows in the workspace."""
    try:
        flows = store.list_flows(workspace_id)
        return ToolResponse(success=True, data=flows)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def get_flow(flow_name: str, workspace_id: Optional[str] = None) -> ToolResponse:
    """Get the full JSON definition and steps of a specific flow in a workspace."""
    try:
        flow = store.load_flow(flow_name, workspace_id)
        if not flow:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        return ToolResponse(success=True, data=flow)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def create_new_flow(
    flow_name: str,
    seed_data: Optional[dict] = None,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Create a new blank flow with an optional initial seed data dict in a workspace."""
    try:
        name = flow_name.strip()
        graph = {"name": name, "data": seed_data or {}, "steps": []}
        store.save_flow(name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"name": name, "message": f"Flow '{name}' created successfully."})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_step_to_flow(
    flow_name: str,
    tool: str,
    bind_as: str = "",
    args: Optional[dict] = None,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add an API call step to a flow.

    Args:
        flow_name: Name of the target flow.
        tool: Tool name, e.g. 'auth__login'.
        bind_as: Variable name to bind response under ($ctx.<bind_as>.body.<path>).
        args: Tool arguments (can use $data.key, $workspace.key, $secret.key, or $ctx.var.body.path).
        workspace_id: Optional workspace ID.
    """
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        node_id = _new_id()
        step = {
            "id": node_id,
            "kind": "call",
            "tool": tool,
            "bind_as": bind_as or "",
            "args": args or {},
            "validate": [],
        }
        graph.setdefault("steps", []).append(step)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": node_id, "step": step})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_subflow_step(
    parent_flow_name: str,
    subflow_name: str,
    bind_as: str = "",
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Embed an existing flow as a modular subflow step inside another flow."""
    try:
        graph = store.load_flow(parent_flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{parent_flow_name}' not found.")
        if not store.load_flow(subflow_name, workspace_id):
            return ToolResponse(success=False, error=f"Subflow '{subflow_name}' does not exist.")

        node_id = _new_id()
        node = {
            "id": node_id,
            "kind": "flow",
            "flow_name": subflow_name,
            "bind_as": bind_as or "",
        }
        graph.setdefault("steps", []).append(node)
        store.save_flow(parent_flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": node_id, "node": node})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_split_step(
    flow_name: str,
    name: str = "Split Paths",
    mode: str = "parallel",
    branch_names: Optional[list[str]] = None,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add a split step that runs branches either in parallel (concurrently) or sequentially."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        if mode not in ("parallel", "sequential"):
            return ToolResponse(success=False, error="mode must be 'parallel' or 'sequential'")

        node_id = _new_id()
        b_names = branch_names or ["Branch 1", "Branch 2"]
        branches = [{"id": _new_id(), "name": bn, "steps": []} for bn in b_names]
        node = {
            "id": node_id,
            "kind": "split",
            "name": name,
            "mode": mode,
            "branches": branches,
        }
        graph.setdefault("steps", []).append(node)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"split_id": node_id, "node": node})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_step_to_branch(
    flow_name: str,
    split_id: str,
    branch_name: str,
    tool: str,
    bind_as: str = "",
    args: Optional[dict] = None,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add an API call step into a specific branch of a split step."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        split_node, _ = _find_node(graph, split_id)
        if not split_node or split_node.get("kind") != "split":
            return ToolResponse(success=False, error=f"Split node '{split_id}' not found.")

        target_branch = None
        for b in split_node.get("branches", []):
            if b.get("name", "").lower() == branch_name.lower() or b.get("id") == branch_name:
                target_branch = b
                break
        if not target_branch:
            available = [b.get("name") for b in split_node.get("branches", [])]
            return ToolResponse(success=False, error=f"Branch '{branch_name}' not found in split. Available: {available}")

        node_id = _new_id()
        step = {
            "id": node_id,
            "kind": "call",
            "tool": tool,
            "bind_as": bind_as or "",
            "args": args or {},
            "validate": [],
        }
        target_branch.setdefault("steps", []).append(step)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": node_id, "step": step, "branch": target_branch.get("name")})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_flow_to_branch(
    flow_name: str,
    split_id: str,
    branch_name: str,
    subflow_name: str,
    bind_as: str = "",
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Embed an existing flow as a subflow inside a specific branch of a split step."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        if not store.load_flow(subflow_name, workspace_id):
            return ToolResponse(success=False, error=f"Subflow '{subflow_name}' does not exist.")
        split_node, _ = _find_node(graph, split_id)
        if not split_node or split_node.get("kind") != "split":
            return ToolResponse(success=False, error=f"Split node '{split_id}' not found.")

        target_branch = None
        for b in split_node.get("branches", []):
            if b.get("name", "").lower() == branch_name.lower() or b.get("id") == branch_name:
                target_branch = b
                break
        if not target_branch:
            available = [b.get("name") for b in split_node.get("branches", [])]
            return ToolResponse(success=False, error=f"Branch '{branch_name}' not found in split. Available: {available}")

        node_id = _new_id()
        node = {
            "id": node_id,
            "kind": "flow",
            "flow_name": subflow_name,
            "bind_as": bind_as or "",
        }
        target_branch.setdefault("steps", []).append(node)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": node_id, "node": node, "branch": target_branch.get("name")})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def set_split_mode(
    flow_name: str,
    split_id: str,
    mode: str,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Set the execution mode of a split step: 'parallel' (concurrent) or 'sequential'."""
    try:
        if mode not in ("parallel", "sequential"):
            return ToolResponse(success=False, error="mode must be 'parallel' or 'sequential'")
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        split_node, _ = _find_node(graph, split_id)
        if not split_node or split_node.get("kind") != "split":
            return ToolResponse(success=False, error=f"Split node '{split_id}' not found.")

        split_node["mode"] = mode
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"split_id": split_id, "mode": mode})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_validation_check(
    flow_name: str,
    step_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    check_type: str = "status",
    status: Optional[int] = None,
    path: Optional[str] = None,
    equals: Optional[Any] = None,
    equals_null: Optional[bool] = None,
    contains: Optional[str] = None,
    op: str = "==",
    expr: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add a validation check/assertion to an existing step in a flow."""
    try:
        target_id = step_id or parent_id
        if not target_id:
            return ToolResponse(success=False, error="step_id (or parent_id) is required.")

        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        node, _ = _find_node(graph, target_id)
        if not node:
            return ToolResponse(success=False, error=f"Step '{target_id}' not found in flow.")

        check_id = _new_id()
        c_type = "json_path" if check_type in ("json", "json_path") else check_type
        check = {"id": check_id, "kind": "check", "check_type": c_type}
        if c_type == "status":
            check["status"] = status or 200
        elif c_type == "json_path":
            check["path"] = path or ""
            if equals_null is True:
                check["equals"] = None
            elif equals is not None:
                check["equals"] = equals
            elif contains is not None:
                check["contains"] = contains
            else:
                check["equals"] = True
            if op != "==":
                check["op"] = op
        elif c_type == "python_expr":
            check["expr"] = expr or "True"

        node.setdefault("validate", []).append(check)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"check_id": check_id, "check": check})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_persistence_validation(
    flow_name: str,
    parent_id: str,
    tool: str,
    args: Optional[dict] = None,
    path: str = "",
    equals: Optional[Any] = None,
    not_none: bool = False,
    status: int = 200,
    op: str = "==",
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Attach a first-class QA persistence/state assertion to a call step.

    ``tool`` is normally a testing endpoint such as
    ``testing__patient_state``. Its arguments and expected value may use
    ``$ctx`` references, so a mutation can be checked against durable state.
    """
    try:
        graph = store.load_flow(flow_name, workspace_id)
        node, _ = _find_node(graph, parent_id)
        if not node or node.get("kind") != "call":
            return ToolResponse(success=False, error=f"Call step '{parent_id}' not found.")
        check = {"id": _new_id(), "kind": "check", "check_type": "persistence", "tool": tool,
                 "args": args or {}, "path": path, "status": status, "not_none": not_none, "op": op}
        if equals is not None:
            check["equals"] = equals
        node.setdefault("validate", []).append(check)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"check_id": check["id"], "check": check})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_assertion_condition(
    flow_name: str, left: Any, op: str = "==", right: Any = None,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add an assert-true condition. A false result fails the flow immediately."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        node = {"id": _new_id(), "kind": "condition", "left": left, "op": op, "right": right,
                "assert": True, "then_steps": [], "else_steps": []}
        graph.setdefault("steps", []).append(node)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": node["id"], "node": node})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_finally_step(
    flow_name: str, tool: str, args: Optional[dict] = None, bind_as: str = "",
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add a teardown call that runs even when an earlier normal step fails."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        step = {"id": _new_id(), "kind": "call", "tool": tool, "args": args or {},
                "bind_as": bind_as, "validate": []}
        graph.setdefault("finally_steps", []).append(step)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": step["id"], "step": step})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_wait_step(
    flow_name: str, seconds: float, name: str = "wait", workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Add an explicit delay step; use retry policies for transient API failures."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        step = {"id": _new_id(), "kind": "wait", "seconds": max(0.0, seconds), "name": name}
        graph.setdefault("steps", []).append(step)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": step["id"], "step": step})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def add_wait_until_step(
    flow_name: str, left: Any, op: str = "is_truthy", right: Any = None,
    steps: Optional[list[dict]] = None, timeout_s: float = 30.0, interval_s: float = 1.0,
    backoff: float = 1.0, max_interval_s: float = 5.0,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Poll optional child steps until a condition is true, or fail on timeout.

    Supply a polling call in ``steps`` (for example a queue/status GET) when
    the condition relies on eventually consistent external state.
    """
    try:
        graph = store.load_flow(flow_name, workspace_id)
        step = {"id": _new_id(), "kind": "wait_until", "left": left, "op": op, "right": right,
                "steps": steps or [], "timeout_s": max(0.0, timeout_s),
                "interval_s": max(0.01, interval_s), "backoff": max(1.0, backoff),
                "max_interval_s": max(0.01, max_interval_s)}
        graph.setdefault("steps", []).append(step)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": step["id"], "step": step})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def set_retry_policy(
    flow_name: str, node_id: str, attempts: int = 3, initial_delay_s: float = 0.25,
    backoff: float = 2.0, max_delay_s: float = 5.0, workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Configure bounded exponential retry for a call (OTP/eventual-consistency safe)."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        node, _ = _find_node(graph, node_id)
        if not node or node.get("kind") != "call":
            return ToolResponse(success=False, error=f"Call step '{node_id}' not found.")
        node["retry"] = {"attempts": max(1, attempts), "initial_delay_s": max(0.0, initial_delay_s),
                         "backoff": max(1.0, backoff), "max_delay_s": max(0.0, max_delay_s)}
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": node_id, "retry": node["retry"]})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def validate_flow(flow_name: str, workspace_id: Optional[str] = None) -> ToolResponse:
    """Preflight OpenAPI-required arguments without making any API calls."""
    try:
        ws_id = workspace_id or workspace_store.get_active_workspace_id()
        graph = store.load_flow(flow_name, ws_id)
        issues = validate_graph(graph, ws_id)
        return ToolResponse(success=not issues, data={"valid": not issues, "issues": issues},
                            error="Flow has incomplete required arguments." if issues else None)
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def remove_validation_check(
    flow_name: str,
    check_id: str,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Remove a validation check from a step in a flow."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        node, parent_list = _find_node(graph, check_id)
        if not node:
            return ToolResponse(success=False, error=f"Validation check '{check_id}' not found.")
        if node.get("kind") != "check":
            return ToolResponse(success=False, error=f"Node '{check_id}' is not a check node (kind={node.get('kind')}).")
        parent_list.remove(node)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"removed_check_id": check_id})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def update_step_args(
    flow_name: str,
    node_id: str,
    args: dict,
    merge: bool = False,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Update or patch the arguments of an existing call step in a flow."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        node, _ = _find_node(graph, node_id)
        if not node:
            return ToolResponse(success=False, error=f"Node '{node_id}' not found in flow.")
        if node.get("kind") != "call":
            return ToolResponse(success=False, error=f"Node '{node_id}' is not a call node (kind={node.get('kind')}).")
        if merge:
            node.setdefault("args", {}).update(args)
        else:
            node["args"] = args
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"node_id": node_id, "args": node["args"]})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def remove_step_from_flow(
    flow_name: str,
    node_id: str,
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Remove a step/node (and its nested checks/sub-steps) from a flow."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        node, parent_list = _find_node(graph, node_id)
        if not node:
            return ToolResponse(success=False, error=f"Node '{node_id}' not found in flow.")
        parent_list.remove(node)
        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"removed_node_id": node_id})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def clean_up_flow(
    flow_name: str,
    orientation: str = "vertical",
    workspace_id: Optional[str] = None,
) -> ToolResponse:
    """Auto-collapse parameters & body JSON, hide validators, and align nodes neatly with zero overlaps."""
    try:
        graph = store.load_flow(flow_name, workspace_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")

        def tidy(node):
            if not node:
                return
            kind = node.get("kind")
            if kind == "call":
                node["hide_validators"] = True
                node["collapse_params"] = True
                node["collapse_json"] = True
                for v in node.get("validate", []):
                    tidy(v)
            elif kind == "condition":
                for s in node.get("then_steps", []):
                    tidy(s)
                for s in node.get("else_steps", []):
                    tidy(s)
            elif kind == "split":
                for b in node.get("branches", []):
                    for s in b.get("steps", []):
                        tidy(s)

        for step in graph.get("steps", []):
            tidy(step)

        mode = orientation.lower()
        if mode == "horizontal":
            cur_x = 80
            base_y = 240
            for step in graph.get("steps", []):
                step["pos"] = {"x": cur_x, "y": base_y}
                if step.get("kind") == "condition":
                    ty = base_y - 180
                    tx = cur_x + 380
                    for s in step.get("then_steps", []):
                        s["pos"] = {"x": tx, "y": ty}
                        tx += 360
                    ey = base_y + 200
                    ex = cur_x + 380
                    for s in step.get("else_steps", []):
                        s["pos"] = {"x": ex, "y": ey}
                        ex += 360
                    cur_x = max(cur_x + 380, max(tx, ex) + 20)
                elif step.get("kind") == "split":
                    branches = step.get("branches", [])
                    total = len(branches)
                    half_span = max(0, (total - 1) * 110)
                    max_x = cur_x + 380
                    for bi, b in enumerate(branches):
                        by = base_y - half_span + (bi * 240)
                        bx = cur_x + 380
                        for s in b.get("steps", []):
                            s["pos"] = {"x": bx, "y": by}
                            bx += 360
                        if bx > max_x:
                            max_x = bx
                    cur_x = max(cur_x + 380, max_x + 20)
                elif step.get("kind") == "call":
                    vy = base_y + 260
                    for v in step.get("validate", []):
                        v["pos"] = {"x": cur_x, "y": vy}
                        vy += 140
                    cur_x += 380
                else:
                    cur_x += 380
        else:
            y = 80
            for step in graph.get("steps", []):
                step["pos"] = {"x": 80, "y": y}
                if step.get("kind") == "condition":
                    ty = y - 40
                    tx = 80 + 380
                    for s in step.get("then_steps", []):
                        s["pos"] = {"x": tx, "y": ty}
                        tx += 360
                    ey = y + 200
                    ex = 80 + 380
                    for s in step.get("else_steps", []):
                        s["pos"] = {"x": ex, "y": ey}
                        ex += 360
                    y += 460
                elif step.get("kind") == "split":
                    branches = step.get("branches", [])
                    by = y - 20
                    for b in branches:
                        bx = 80 + 380
                        for s in b.get("steps", []):
                            s["pos"] = {"x": bx, "y": by}
                            bx += 360
                        by += 240
                    y = max(y + 280, by + 40)
                elif step.get("kind") == "call":
                    vy = y
                    for v in step.get("validate", []):
                        v["pos"] = {"x": 80 + 380, "y": vy}
                        vy += 140
                    v_count = len(step.get("validate", []))
                    y += max(280, v_count * 140 + 40)
                elif step.get("kind") == "flow":
                    y += 240
                else:
                    y += 280

        store.save_flow(flow_name, graph, workspace_id=workspace_id)
        return ToolResponse(success=True, data={"message": f"Flow '{flow_name}' cleaned up and aligned ({mode} layout, zero overlaps)."})
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def run_flow(
    flow_name: str,
    workspace_id: Optional[str] = None,
    params: Optional[dict] = None,
    base_url: Optional[str] = None,
) -> ToolResponse:
    """Execute a flow against the workspace's target base URL, optionally overriding parameters or base URL.

    Args:
        flow_name: Name of the saved flow blueprint to run.
        workspace_id: Optional workspace ID.
        params: Optional runtime dictionary to override seed data ($data.<key>).
        base_url: Optional base URL override for this run.
    """
    try:
        ws_id = workspace_id or workspace_store.get_active_workspace_id()
        graph = store.load_flow(flow_name, ws_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        if params:
            graph.setdefault("data", {}).update(params)
        issues = validate_graph(graph, ws_id)
        if issues:
            return ToolResponse(success=False, data={"status": "invalid", "issues": issues},
                                error="Flow has incomplete required arguments; no API calls were made.")
        ws = workspace_store.load_workspace(ws_id)
        api = build_api_client(workspace_id=ws_id, base_url=base_url)
        flow_fn = make_flow_fn(graph, workspace=ws)
        report = execute_flow(flow_fn, api)
        return ToolResponse(
            success=(report.status == "pass"),
            data=asdict(report),
            error=report.error if report.status != "pass" else None,
        )
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def run_workspace_flows_sequentially(
    workspace_id: Optional[str] = None,
    base_url: Optional[str] = None,
) -> ToolResponse:
    """Run every saved flow in a workspace sequentially and return each result.

    The suite continues after failures so the response contains a complete
    pass/fail report. For live progress in the dashboard, use its “Run All
    Flows” control, which streams the same runner events over WebSocket.
    """
    try:
        ws_id = workspace_id or workspace_store.get_active_workspace_id()
        reports = run_workspace_flows(ws_id, base_url=base_url)
        data = {"workspace_id": ws_id, "flows": [asdict(report) for report in reports]}
        data["status"] = "pass" if all(report.status == "pass" for report in reports) else "fail"
        return ToolResponse(success=data["status"] == "pass", data=data,
                            error=None if data["status"] == "pass" else "One or more flows failed.")
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


@app.tool()
def run_flow_node(
    flow_name: str,
    node_id: str,
    workspace_id: Optional[str] = None,
    args_override: Optional[dict] = None,
    data_override: Optional[dict] = None,
    base_url: Optional[str] = None,
) -> ToolResponse:
    """Execute a single node/step in a flow, optionally overriding arguments or parameters without altering the saved flow.

    Args:
        flow_name: Flow name containing the node.
        node_id: Target node ID to execute.
        workspace_id: Optional workspace ID.
        args_override: Optional dictionary of arguments to override on this step for this execution.
        data_override: Optional dictionary of seed data to override.
        base_url: Optional base URL override.
    """
    try:
        ws_id = workspace_id or workspace_store.get_active_workspace_id()
        graph = store.load_flow(flow_name, ws_id)
        if not graph:
            return ToolResponse(success=False, error=f"Flow '{flow_name}' not found.")
        if data_override:
            graph.setdefault("data", {}).update(data_override)
        if args_override:
            def walk(steps):
                for s in steps:
                    if s.get("id") == node_id:
                        s.setdefault("args", {}).update(args_override)
                        return True
                    if s.get("then") and walk(s["then"]):
                        return True
                    if s.get("else") and walk(s["else"]):
                        return True
                    for b in s.get("branches", []):
                        if walk(b.get("steps", [])):
                            return True
                return False
            walk(graph.get("steps", []))
        ws = workspace_store.load_workspace(ws_id)
        api = build_api_client(workspace_id=ws_id, base_url=base_url)
        result = run_single_node(graph, node_id, api, workspace=ws)
        return ToolResponse(
            success=result.get("ok", False),
            data=result,
            error=result.get("error") if not result.get("ok", False) else None,
        )
    except Exception as e:
        return ToolResponse(success=False, error=str(e))


def main():
    parser = argparse.ArgumentParser(description="tibb-tester MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run in SSE mode (HTTP) instead of stdio")
    parser.add_argument("--port", type=int, default=8805, help="Port for SSE mode (default: 8805)")
    args = parser.parse_args()

    if args.sse:
        app.run(transport="sse", port=args.port)
    else:
        _run_compat_stdio()


def _jsonrpc_response(request_id: Any, result: dict | None = None, error: dict | None = None) -> None:
    """Write exactly one newline-delimited JSON-RPC response to stdout."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result or {}
    sys.stdout.write(json.dumps(message, default=str, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _compat_tool_definitions() -> list[dict]:
    """Expose MCP tool metadata without relying on the SDK's broken stdio adapter.

    Recent MCP SDK builds replace process file descriptors while serving stdio.
    That transport can leave a client waiting forever in some subprocess hosts.
    The tools themselves are regular synchronous functions, so this thin
    JSON-RPC loop keeps the standard MCP wire contract but avoids that adapter.
    """
    definitions = []
    for tool in app._tool_manager.list_tools():
        item = {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters,
        }
        if tool.title:
            item["title"] = tool.title
        if tool.output_schema:
            item["outputSchema"] = tool.output_schema
        if tool.annotations:
            item["annotations"] = tool.annotations.model_dump(by_alias=True, mode="json")
        definitions.append(item)
    return definitions


def _run_compat_stdio() -> None:
    """A reliable MCP stdio transport for newline-delimited JSON-RPC clients."""
    initialized = False
    for raw_line in sys.stdin:
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            _jsonrpc_response(None, error={"code": -32700, "message": f"Parse error: {exc.msg}"})
            continue
        if not isinstance(message, dict):
            _jsonrpc_response(None, error={"code": -32600, "message": "Invalid request"})
            continue

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        # Notifications deliberately have no id and therefore no response.
        if method == "notifications/initialized":
            initialized = True
            continue
        if method == "initialize":
            initialized = True
            requested_version = params.get("protocolVersion", "2025-03-26") if isinstance(params, dict) else "2025-03-26"
            _jsonrpc_response(request_id, {
                "protocolVersion": requested_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "tibb-tester", "version": "0.1.0"},
            })
            continue
        if method == "tools/list":
            _jsonrpc_response(request_id, {"tools": _compat_tool_definitions()})
            continue
        if method == "tools/call":
            if not initialized:
                _jsonrpc_response(request_id, error={"code": -32002, "message": "Server not initialized"})
                continue
            if not isinstance(params, dict):
                _jsonrpc_response(request_id, error={"code": -32602, "message": "Invalid tool parameters"})
                continue
            name = params.get("name")
            arguments = params.get("arguments") or {}
            tool = app._tool_manager.get_tool(name) if isinstance(name, str) else None
            if tool is None:
                _jsonrpc_response(request_id, error={"code": -32601, "message": f"Unknown tool: {name}"})
                continue
            if not isinstance(arguments, dict):
                _jsonrpc_response(request_id, error={"code": -32602, "message": "Tool arguments must be an object"})
                continue
            try:
                value = tool.fn(**arguments)
                if inspect.isawaitable(value):
                    value = asyncio.run(value)
                payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
                text_value = json.dumps(payload, default=str)
                is_error = isinstance(payload, dict) and payload.get("success") is False
                _jsonrpc_response(request_id, {
                    "content": [{"type": "text", "text": text_value}],
                    "structuredContent": payload,
                    "isError": is_error,
                })
            except Exception as exc:
                _jsonrpc_response(request_id, {
                    "content": [{"type": "text", "text": json.dumps({"success": False, "error": str(exc)})}],
                    "isError": True,
                })
            continue
        if request_id is not None:
            _jsonrpc_response(request_id, error={"code": -32601, "message": f"Method not found: {method}"})


if __name__ == "__main__":
    main()
