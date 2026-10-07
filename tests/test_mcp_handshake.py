import json
import subprocess
import sys
import pytest


def test_mcp_stdio_initialize_handshake():
    p = subprocess.Popen(
        [sys.executable, "scripts/mcp_server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    init_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "Claude Code", "version": "1.0"},
        },
    }) + "\n"

    p.stdin.write(init_msg)
    p.stdin.flush()

    line = p.stdout.readline()
    assert line, "Expected JSON-RPC initialize response but stdout was empty"
    resp = json.loads(line)
    assert resp.get("jsonrpc") == "2.0"
    assert resp.get("id") == 1
    assert "result" in resp
    assert resp["result"]["serverInfo"]["name"] == "tibb-tester"

    # Send tools/list
    tools_msg = json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }) + "\n"
    p.stdin.write(tools_msg)
    p.stdin.flush()

    tools_line = p.stdout.readline()
    assert tools_line, "Expected tools/list response"
    tools_resp = json.loads(tools_line)
    tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
    assert "list_saved_flows" in tool_names
    assert "add_split_step" in tool_names

    p.terminate()


@pytest.fixture
def isolated_mcp_workspace():
    from core import workspace_store
    ws_id = "test_mcp_handshake_ws"
    if workspace_store.get_workspace_dir(ws_id).exists():
        workspace_store.delete_workspace(ws_id)
    workspace_store.create_workspace(ws_id, name="MCP Handshake Test WS")
    workspace_store.set_active_workspace_id(ws_id)
    try:
        yield ws_id
    finally:
        workspace_store.set_active_workspace_id("default")
        workspace_store.delete_workspace(ws_id)


def test_mcp_run_flow_and_run_node(isolated_mcp_workspace):
    from core import flow_store as store
    from scripts.mcp_server import run_flow, run_flow_node

    flow_name = "test_mcp_execution_flow"
    graph = {
        "name": flow_name,
        "data": {"num": 10},
        "steps": [
            {
                "id": "step_cond",
                "kind": "condition",
                "left": "$data.num",
                "op": "==",
                "right": 10,
            }
        ],
    }

    try:
        store.save_flow(flow_name, graph)

        # Test run_flow executes without TypeError and passes
        res = run_flow(flow_name)
        assert res.success is True, f"run_flow failed: {res.error}"
        assert res.data["status"] == "pass"
        assert len(res.data["steps"]) == 1

        # Test run_flow_node executes node and returns success
        node_res = run_flow_node(flow_name, "step_cond")
        assert node_res.success is True, f"run_flow_node failed: {node_res.error}"
        assert node_res.data["status"] == "pass"
    finally:
        store.delete_flow(flow_name)


def test_mcp_describe_api_endpoint_resolved_schema():
    from scripts.mcp_server import describe_api_endpoint

    # Test describe_api_endpoint resolves nested objects, types, and array items
    res = describe_api_endpoint("place_order", workspace_id="default")
    assert res.success is True, f"describe_api_endpoint failed: {res.error}"
    data = res.data
    assert data["method"] == "POST"
    assert "/api/v1/lab/orders" in data["path"]

    # Check that items field contains the resolved object properties
    items_field = next(f for f in data["fields"] if f["name"] == "items")
    assert items_field["type"] == "array"
    assert items_field["items"]["type"] == "object"
    assert "lab_test_id" in items_field["items"]["properties"]
    assert items_field["items"]["properties"]["lab_test_id"]["type"] == "integer"


def test_mcp_step_args_patching_and_validation_removal(isolated_mcp_workspace):
    from core import flow_store as store
    from scripts.mcp_server import (
        create_new_flow,
        add_step_to_flow,
        update_step_args,
        add_validation_check,
        remove_validation_check,
        remove_step_from_flow,
    )

    flow_name = "test_mcp_edit_tools_flow"
    try:
        create_new_flow(flow_name)

        # 1. Add step
        step_res = add_step_to_flow(
            flow_name=flow_name,
            tool="test_tool",
            bind_as="test_bind",
            args={"initial_key": "val1"},
        )
        assert step_res.success is True
        node_id = step_res.data["node_id"]

        # 2. Update step args with patch/merge
        update_res = update_step_args(
            flow_name=flow_name,
            node_id=node_id,
            args={"notify_recipient_type": "self"},
            merge=True,
        )
        assert update_res.success is True
        assert update_res.data["args"]["initial_key"] == "val1"
        assert update_res.data["args"]["notify_recipient_type"] == "self"

        # 3. Add validation check with equals_null=True
        check_res = add_validation_check(
            flow_name=flow_name,
            parent_id=node_id,
            check_type="json",
            path="error_msg",
            equals_null=True,
        )
        assert check_res.success is True
        check_id = check_res.data["check_id"]

        flow = store.load_flow(flow_name)
        check_node = flow["steps"][0]["validate"][0]
        assert check_node["id"] == check_id
        assert check_node["equals"] is None

        # 4. Remove validation check
        rem_check_res = remove_validation_check(flow_name, check_id)
        assert rem_check_res.success is True
        flow = store.load_flow(flow_name)
        assert len(flow["steps"][0]["validate"]) == 0

        # 5. Remove step from flow
        rem_step_res = remove_step_from_flow(flow_name, node_id)
        assert rem_step_res.success is True
        flow = store.load_flow(flow_name)
        assert len(flow["steps"]) == 0
    finally:
        store.delete_flow(flow_name)


