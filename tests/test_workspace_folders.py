"""Tests for Workspace Folder Architecture:
- Folder-based workspace persistence with openapi.json, flows/, base_url, secrets, and data
- Hierarchical reference resolution: flow-level vs workspace-level key-values and secrets
- Per-workspace client execution and MCP workspace tools
"""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from core import flow_store, workspace_store
from core.client.factory import build_api_client
from core.graph_builder import make_flow_fn
from flows.sdk import FlowContext
from scripts import mcp_server
from ui_server.app import app

client = TestClient(app)


def test_workspace_folder_lifecycle_and_structure(tmp_path):
    ws_id = "test_workspace_alpha"
    try:
        # 1. Create workspace
        ws = workspace_store.create_workspace(
            workspace_id=ws_id,
            name="Alpha Environment",
            base_url="https://api.alpha.test.internal",
            data={"tenant_id": 101, "env": "alpha"},
            secrets={"testing_api_key": "sec_alpha_123", "bearer_token": "tok_xyz"},
            openapi_spec=json.dumps({
                "openapi": "3.0.0",
                "info": {"title": "Alpha API", "version": "1.0"},
                "paths": {
                    "/alpha/ping": {
                        "get": {
                            "operationId": "get_alpha_ping",
                            "tags": ["alpha"],
                            "responses": {"200": {"description": "OK"}},
                        }
                    }
                },
            }),
        )

        assert ws["id"] == ws_id
        assert ws["name"] == "Alpha Environment"
        assert ws["base_url"] == "https://api.alpha.test.internal"
        assert ws["data"]["tenant_id"] == 101
        assert ws["secrets"]["testing_api_key"] == "sec_alpha_123"
        assert ws["has_spec"] is True

        ws_dir = workspace_store.get_workspace_dir(ws_id)
        assert ws_dir.exists()
        assert (ws_dir / "workspace.json").exists()
        assert (ws_dir / "openapi.json").exists()
        assert (ws_dir / "flows").exists()

        # 2. Save a flow inside this workspace
        flow_name = "alpha_sample_flow"
        graph = {
            "name": flow_name,
            "data": {"sample_id": 42},
            "steps": [],
        }
        flow_store.save_flow(flow_name, graph, workspace_id=ws_id)

        # Verify flow file exists in workspace's own flows directory
        assert (ws_dir / "flows" / f"{flow_name}.flow.json").exists()
        assert (ws_dir / "flows" / f"{flow_name}.py").exists()

        # List flows in workspace
        ws_flows = flow_store.list_flows(ws_id)
        assert flow_name in ws_flows

        # Load flow from workspace
        loaded = flow_store.load_flow(flow_name, ws_id)
        assert loaded["name"] == flow_name
        assert loaded["data"]["sample_id"] == 42

        # 3. Update workspace base_url and data
        updated = workspace_store.save_workspace(
            workspace_id=ws_id,
            base_url="https://api.alpha-v2.test.internal",
            data={"tenant_id": 202, "env": "alpha_v2"},
        )
        assert updated["base_url"] == "https://api.alpha-v2.test.internal"
        assert updated["data"]["tenant_id"] == 202

        # 4. Verify API client builder picks up workspace base_url and spec
        api = build_api_client(workspace_id=ws_id)
        # Client base_url should match workspace's configured base_url
        assert getattr(api, "_client", None) is not None
        assert api._client.base_url == "https://api.alpha-v2.test.internal"

    finally:
        workspace_store.delete_workspace(ws_id)
        assert not workspace_store.get_workspace_dir(ws_id).exists()


def test_hierarchical_data_and_secrets_resolution():
    """Verify that flow-level data overrides workspace-level data, while workspace secrets and non-clashing data resolve seamlessly."""
    ws_data = {"shared_key": "ws_val", "override_me": "ws_orig"}
    flow_data = {"override_me": "flow_override", "flow_only": "flow_val"}
    secrets = {"api_token": "secret_token_999"}

    ctx = FlowContext(
        api=None,
        data=flow_data,
        workspace_data=ws_data,
        secrets=secrets,
    )

    # 1. Flow data overrides workspace data when keys overlap
    assert ctx.data["override_me"] == "flow_override"

    # 2. Non-overlapping workspace data is directly accessible via ctx.data
    assert ctx.data["shared_key"] == "ws_val"

    # 3. Flow-only data is accessible
    assert ctx.data["flow_only"] == "flow_val"

    # 4. Workspace secrets are accessible via ctx.secrets
    assert ctx.secrets["api_token"] == "secret_token_999"

    # 5. Test reference resolvers from core.refs
    from core.refs import resolve_value

    # $data.<key> resolves flow-level override
    res_override = resolve_value("$data.override_me")(ctx)
    assert res_override == "flow_override"

    # $data.<key> falls back to workspace data
    res_shared = resolve_value("$data.shared_key")(ctx)
    assert res_shared == "ws_val"

    # $workspace.<key> explicitly resolves workspace data
    res_ws = resolve_value("$workspace.shared_key")(ctx)
    assert res_ws == "ws_val"

    # $secret.<key> resolves secrets
    res_sec = resolve_value("$secret.api_token")(ctx)
    assert res_sec == "secret_token_999"


def test_mcp_workspace_tools():
    """Verify MCP workspace management tools: list_workspaces, create_workspace, get_workspace, update_workspace, switch_active_workspace."""
    test_ws_id = "test_mcp_ws"
    try:
        # 1. Create workspace via MCP tool
        create_res = mcp_server.create_workspace(
            workspace_id=test_ws_id,
            name="MCP Workspace Test",
            base_url="https://api.mcp.test",
            data={"org": "tibb"},
            secrets={"testing_api_key": "mcp_sec_key"},
        )
        assert create_res.success is True
        assert create_res.data["id"] == test_ws_id
        assert create_res.data["base_url"] == "https://api.mcp.test"

        # 2. List workspaces via MCP tool
        list_res = mcp_server.list_workspaces()
        assert list_res.success is True
        ws_ids = [w["id"] for w in list_res.data]
        assert test_ws_id in ws_ids

        # 3. Get workspace details (secrets are safely masked in tool response)
        get_res = mcp_server.get_workspace(test_ws_id)
        assert get_res.success is True
        assert get_res.data["data"]["org"] == "tibb"
        assert get_res.data["secrets_masked"]["testing_api_key"] == "***"

        # 4. Switch active workspace
        switch_res = mcp_server.switch_active_workspace(test_ws_id)
        assert switch_res.success is True
        assert workspace_store.get_active_workspace_id() == test_ws_id

        # 5. Create a flow in this active workspace
        flow_res = mcp_server.create_new_flow("mcp_ws_flow")
        assert flow_res.success is True
        assert (workspace_store.get_workspace_dir(test_ws_id) / "flows" / "mcp_ws_flow.flow.json").exists()

        # 6. Update workspace
        up_res = mcp_server.update_workspace(
            workspace_id=test_ws_id,
            base_url="https://api.mcp-updated.test",
        )
        assert up_res.success is True
        assert up_res.data["base_url"] == "https://api.mcp-updated.test"

    finally:
        mcp_server.switch_active_workspace("default")
        mcp_server.delete_workspace(test_ws_id)


def test_rest_api_workspace_endpoints():
    """Verify FastAPI endpoints for workspace management."""
    ws_id = "test_rest_ws"
    try:
        # POST /api/workspaces
        res = client.post("/api/workspaces", json={
            "id": ws_id,
            "name": "REST API Test WS",
            "base_url": "https://rest.test.api",
            "data": {"k1": "v1"},
            "secrets": {"token": "secret123"},
        })
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == ws_id
        assert data["base_url"] == "https://rest.test.api"

        # GET /api/workspaces
        list_res = client.get("/api/workspaces")
        assert list_res.status_code == 200
        assert any(w["id"] == ws_id for w in list_res.json())

        # GET /api/workspaces/{id}
        get_res = client.get(f"/api/workspaces/{ws_id}")
        assert get_res.status_code == 200
        assert get_res.json()["name"] == "REST API Test WS"

        # PUT /api/workspaces/{id}
        put_res = client.put(f"/api/workspaces/{ws_id}", json={
            "name": "REST API Updated",
            "base_url": "https://rest2.test.api",
        })
        assert put_res.status_code == 200
        assert put_res.json()["name"] == "REST API Updated"
        assert put_res.json()["base_url"] == "https://rest2.test.api"

        # PUT flow in workspace: /api/workspaces/{ws_id}/flows/{name}
        flow_res = client.put(f"/api/workspaces/{ws_id}/flows/rest_flow", json={
            "graph": {"name": "rest_flow", "steps": []},
        })
        assert flow_res.status_code == 200
        assert flow_res.json()["ok"] is True

        # GET flow in workspace
        load_res = client.get(f"/api/workspaces/{ws_id}/flows/rest_flow")
        assert load_res.status_code == 200
        assert load_res.json()["name"] == "rest_flow"

    finally:
        client.delete(f"/api/workspaces/{ws_id}")


def test_workspace_isolated_apis_and_manual_tools():
    """Verify that workspaces have their own isolated APIs/tools and manual additions do not leak."""
    ws1 = "test_ws_isolated_1"
    ws2 = "test_ws_isolated_2"
    try:
        workspace_store.create_workspace(workspace_id=ws1, name="WS One")
        workspace_store.create_workspace(workspace_id=ws2, name="WS Two")

        # 1. Add manual API to ws1
        res = client.post(f"/api/workspaces/{ws1}/tools/manual", json={
            "operation_id": "create_special_record",
            "tag": "special",
            "method": "POST",
            "path": "/api/v1/special/records",
            "query_params": ["limit"],
            "body_fields": ["title", "notes"],
        })
        assert res.status_code == 200
        tool_name = res.json()["tool_name"]
        assert tool_name == "special__create_special_record"

        # Check custom_apis.json was written to ws1 directory
        ws1_custom = workspace_store.get_workspace_custom_apis_path(ws1)
        assert ws1_custom.exists()

        # 2. Check ws1 tools includes this tool
        tools_ws1 = client.get(f"/api/workspaces/{ws1}/tools").json()
        matching_ws1 = [t for t in tools_ws1 if t["name"] == tool_name]
        assert len(matching_ws1) == 1
        assert matching_ws1[0]["is_manual"] is True
        assert matching_ws1[0]["method"] == "POST"
        assert matching_ws1[0]["path"] == "/api/v1/special/records"

        # 3. Check ws2 tools does NOT include this tool (isolation!)
        tools_ws2 = client.get(f"/api/workspaces/{ws2}/tools").json()
        assert len(tools_ws2) == 0  # No global fallback; empty workspace has strictly 0 tools

        # 4. Test MCP tool with workspace_id
        mcp_res = mcp_server.create_manual_api_tool(
            workspace_id=ws2,
            operation_id="custom_ws2_endpoint",
            method="GET",
            path="/api/ws2/custom",
            tag="ws2",
        )
        assert mcp_res.success is True
        assert mcp_res.data["tool_name"] == "ws2__custom_ws2_endpoint"

        # Verify ws2 has it, ws1 does not
        tools_ws2_updated = client.get(f"/api/workspaces/{ws2}/tools").json()
        assert any(t["name"] == "ws2__custom_ws2_endpoint" for t in tools_ws2_updated)
        assert not any(t["name"] == "ws2__custom_ws2_endpoint" for t in client.get(f"/api/workspaces/{ws1}/tools").json())

        # 5. Remove manual tool via MCP
        rem_res = mcp_server.remove_manual_api_tool("custom_ws2_endpoint", workspace_id=ws2)
        assert rem_res.success is True
        assert not any(t["name"] == "ws2__custom_ws2_endpoint" for t in client.get(f"/api/workspaces/{ws2}/tools").json())

        # 6. Remove manual tool via REST API
        del_res = client.delete(f"/api/workspaces/{ws1}/tools/manual/create_special_record")
        assert del_res.status_code == 200
        assert del_res.json()["ok"] is True
        assert not any(t["name"] == tool_name for t in client.get(f"/api/workspaces/{ws1}/tools").json())

    finally:
        workspace_store.delete_workspace(ws1)
        workspace_store.delete_workspace(ws2)


def test_run_with_parameter_and_argument_overrides(monkeypatch):
    """Verify Edit & Run capability: running single nodes or entire flows with args_override and params overrides."""
    from core.client.base_client import StepResult

    ws_id = "test_edit_run_ws"
    try:
        workspace_store.create_workspace(
            workspace_id=ws_id,
            name="Edit Run Workspace",
            base_url="http://mock",
            data={"user": "wrong_user", "pwd": "wrong_pwd"},
        )

        class MockAuthAPI:
            def login(self, username, password):
                if username == "admin" and password == "secret":
                    return StepResult(status_code=200, headers={"content-type": "application/json"},
                                      body={"token": "valid_token"}, url="http://mock/login", method="POST")
                return StepResult(status_code=401, headers={"content-type": "application/json"},
                                  body={"detail": "invalid"}, url="http://mock/login", method="POST")

        class MockAPI:
            def __init__(self):
                self.auth = MockAuthAPI()

        mock_client = MockAPI()
        monkeypatch.setattr("ui_server.app._safe_build_client", lambda *a, **k: mock_client)
        monkeypatch.setattr("scripts.mcp_server.build_api_client", lambda *a, **k: mock_client)

        flow_graph = {
            "name": "edit_run_flow",
            "data": {"user": "wrong_user", "pwd": "wrong_pwd"},
            "steps": [
                {
                    "id": "step_auth",
                    "kind": "call",
                    "tool": "auth__login",
                    "bind_as": "auth",
                    "args": {"username": "$data.user", "password": "$data.pwd"},
                    "validate": [{"id": "check_auth", "kind": "check", "check_type": "status", "status": 200}],
                }
            ],
        }
        from core import flow_store
        flow_store.save_flow("edit_run_flow", flow_graph, workspace_id=ws_id)

        # 1. Run node with original data -> fails with 401
        res_orig = client.post(
            f"/api/workspaces/{ws_id}/flows/edit_run_flow/nodes/step_auth/run",
            json={"graph": flow_graph},
        )
        assert res_orig.status_code == 200
        assert res_orig.json()["status"] == "fail"
        assert res_orig.json()["response"]["status_code"] == 401

        # 2. Run node with args_override -> succeeds with 200
        res_override = client.post(
            f"/api/workspaces/{ws_id}/flows/edit_run_flow/nodes/step_auth/run",
            json={
                "graph": flow_graph,
                "args_override": {"username": "admin", "password": "secret"},
            },
        )
        assert res_override.status_code == 200
        assert res_override.json()["status"] == "pass"
        assert res_override.json()["response"]["status_code"] == 200
        assert res_override.json()["response"]["body"]["token"] == "valid_token"

        # 3. Run node with data_override -> succeeds with 200
        res_data_override = client.post(
            f"/api/workspaces/{ws_id}/flows/edit_run_flow/nodes/step_auth/run",
            json={
                "graph": flow_graph,
                "data_override": {"user": "admin", "pwd": "secret"},
            },
        )
        assert res_data_override.status_code == 200
        assert res_data_override.json()["status"] == "pass"

        # 4. Run full flow via REST with params override -> passes
        res_flow_override = client.post(
            f"/api/workspaces/{ws_id}/flows/edit_run_flow/run",
            json={
                "graph": flow_graph,
                "params": {"user": "admin", "pwd": "secret"},
            },
        )
        assert res_flow_override.status_code == 200
        assert res_flow_override.json()["status"] == "pass"

        # 5. Run via MCP server run_flow_node with args_override
        mcp_node_res = mcp_server.run_flow_node(
            flow_name="edit_run_flow",
            node_id="step_auth",
            workspace_id=ws_id,
            args_override={"username": "admin", "password": "secret"},
        )
        assert mcp_node_res.success is True
        assert mcp_node_res.data["status"] == "pass"

        # 6. Run via MCP server run_flow with params override
        mcp_flow_res = mcp_server.run_flow(
            flow_name="edit_run_flow",
            workspace_id=ws_id,
            params={"user": "admin", "pwd": "secret"},
        )
        assert mcp_flow_res.success is True
        assert mcp_flow_res.data["status"] == "pass"

    finally:
        workspace_store.delete_workspace(ws_id)


