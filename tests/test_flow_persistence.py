from fastapi.testclient import TestClient
import pytest
from core import flow_store as store, workspace_store
from core.codegen import generate_flow_source
from ui_server.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_persistence_workspace():
    ws_id = "test_persistence_isolated"
    if workspace_store.get_workspace_dir(ws_id).exists():
        workspace_store.delete_workspace(ws_id)
    workspace_store.create_workspace(ws_id, name="Persistence Test WS")
    workspace_store.set_active_workspace_id(ws_id)
    try:
        yield ws_id
    finally:
        workspace_store.set_active_workspace_id("default")
        workspace_store.delete_workspace(ws_id)


def test_flow_metadata_persistence_and_codegen():
    test_name = "test_persistence_flow"
    graph_data = {
        "name": test_name,
        "data": {"foo": "bar"},
        "steps": [
            {
                "id": "node_call_1",
                "kind": "call",
                "tool": "admin__admin_login_api_v1_admin_login_post",
                "bind_as": "login_res",
                "args": {"username": "admin"},
                "pos": {"x": 120, "y": 240},
                "hide_validators": True,
                "collapse_params": True,
                "collapse_json": False,
                "validate": [
                    {
                        "id": "node_check_1",
                        "kind": "check",
                        "check_type": "status",
                        "status": 200,
                        "pos": {"x": 480, "y": 240},
                    }
                ],
            }
        ],
    }

    try:
        # 1. Test store save and load
        store.save_flow(test_name, graph_data)
        loaded = store.load_flow(test_name)
        assert loaded["name"] == test_name
        assert loaded["steps"][0]["pos"] == {"x": 120, "y": 240}
        assert loaded["steps"][0]["hide_validators"] is True
        assert loaded["steps"][0]["collapse_params"] is True
        assert loaded["steps"][0]["collapse_json"] is False
        assert loaded["steps"][0]["validate"][0]["pos"] == {"x": 480, "y": 240}

        # 2. Test codegen produces valid python without error
        py_src = generate_flow_source(graph_data)
        assert "Call(" in py_src
        assert "admin_login_api_v1_admin_login_post" in py_src
        assert "expect_status(200)" in py_src

        # 3. Test API endpoint PUT and GET
        graph_data["steps"][0]["pos"] = {"x": 300, "y": 450}
        graph_data["steps"][0]["collapse_json"] = True
        put_res = client.put(f"/api/flows/{test_name}", json={"graph": graph_data})
        assert put_res.status_code == 200
        assert put_res.json()["ok"] is True
        assert put_res.json()["version"] == 2

        get_res = client.get(f"/api/flows/{test_name}")
        assert get_res.status_code == 200
        data = get_res.json()
        assert data["steps"][0]["pos"] == {"x": 300, "y": 450}
        assert data["steps"][0]["collapse_json"] is True
        assert data["steps"][0]["hide_validators"] is True
        assert data["version"] == 2
    finally:
        store.delete_flow(test_name)


def test_flow_optimistic_concurrency_conflict():
    test_name = "test_concurrency_flow"
    graph_v1 = {"name": test_name, "steps": []}
    try:
        # Initial save gives version 1
        res1 = client.put(f"/api/flows/{test_name}", json={"graph": graph_v1, "version": 0})
        assert res1.status_code == 200
        v1 = res1.json()["version"]
        assert v1 == 1

        # Simulate external write (e.g. MCP / Claude Code) advancing version to 2
        graph_v2 = {"name": test_name, "steps": [{"id": "step_1", "kind": "call", "tool": "test_tool"}]}
        res2 = client.put(f"/api/flows/{test_name}", json={"graph": graph_v2, "version": v1})
        assert res2.status_code == 200
        v2 = res2.json()["version"]
        assert v2 == 2

        # Stale browser tab attempts to save with outdated version 1
        stale_graph = {"name": test_name, "version": 1, "steps": []}
        conflict_res = client.put(f"/api/flows/{test_name}", json={"graph": stale_graph, "version": 1})
        assert conflict_res.status_code == 409
        assert "Flow 'test_concurrency_flow' has been updated on the server" in conflict_res.json()["detail"]

        # Re-fetching returns latest version and data from server
        fresh_res = client.get(f"/api/flows/{test_name}")
        assert fresh_res.status_code == 200
        fresh_data = fresh_res.json()
        assert fresh_data["version"] == 2
        assert len(fresh_data["steps"]) == 1

        # Tab saves with matching version 2 -> succeeds and increments to 3
        fresh_data["steps"].append({"id": "step_2", "kind": "call", "tool": "test_tool_2"})
        ok_res = client.put(f"/api/flows/{test_name}", json={"graph": fresh_data, "version": 2})
        assert ok_res.status_code == 200
        assert ok_res.json()["version"] == 3
    finally:
        store.delete_flow(test_name)

