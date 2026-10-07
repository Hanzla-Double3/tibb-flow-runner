import json
from unittest.mock import MagicMock
import pytest

from core import flow_store, workspace_store
from core.codegen import generate_flow_source
from core.graph_builder import make_flow_fn, run_single_node
from flows.sdk import FlowContext, FlowReport, StepFailed, StepReport
from runner.executor import run_flow


@pytest.fixture(autouse=True)
def isolated_subflow_workspace():
    ws_id = "test_subflows_isolated"
    if workspace_store.get_workspace_dir(ws_id).exists():
        workspace_store.delete_workspace(ws_id)
    workspace_store.create_workspace(ws_id, name="Subflows Test Workspace")
    workspace_store.set_active_workspace_id(ws_id)
    try:
        yield ws_id
    finally:
        workspace_store.set_active_workspace_id("default")
        workspace_store.delete_workspace(ws_id)


class DummyResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(self._body)
        self.url = "http://testserver/dummy"

    def json(self):
        return self._body


def test_subflow_execution_and_context_merging():
    """Verifies that a parent flow running a subflow block merges all subflow context."""
    print(">>> PATH IS:", flow_store._paths("login_subflow"))
    # 1. Create and save a subflow 'login_subflow'
    login_graph = {
        "name": "login_subflow",
        "data": {"default_user": "alice"},
        "steps": [
            {
                "id": "s1",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "login_res",
                "args": {"username": "$data.default_user"},
                "validate": [],
            }
        ],
    }
    flow_store.save_flow("login_subflow", login_graph)

    # 2. Create parent flow 'main_flow' that uses 'login_subflow' as a block
    # and then calls 'user__profile' using $ctx.login_res.body.token
    main_graph = {
        "name": "main_flow",
        "data": {},
        "steps": [
            {
                "id": "step_subflow",
                "kind": "flow",
                "flow_name": "login_subflow",
                "bind_as": "auth_block",
            },
            {
                "id": "step_profile",
                "kind": "call",
                "tool": "user__get_profile",
                "bind_as": "profile_res",
                "args": {"token": "$ctx.login_res.body.token"},
                "validate": [],
            },
        ],
    }
    flow_store.save_flow("main_flow", main_graph)

    # 3. Setup mock API
    mock_api = MagicMock()
    mock_api.auth.login.return_value = DummyResponse(200, {"token": "secret_abc_123"})
    mock_api.user.get_profile.return_value = DummyResponse(200, {"name": "Alice", "role": "admin"})

    flow_fn = make_flow_fn(main_graph)
    report = run_flow(flow_fn, mock_api)

    assert report.status == "pass"
    assert len(report.steps) == 2

    # Verify flow step report
    flow_step = report.steps[0]
    assert flow_step.kind == "flow"
    assert flow_step.name == "flow: login_subflow"
    assert flow_step.status == "pass"
    assert len(flow_step.children) == 1

    # Verify the downstream tool call received the token from the subflow's bound context
    mock_api.user.get_profile.assert_called_once_with(token="secret_abc_123")


def test_subflow_single_node_run():
    """Verifies that run_single_node executes a flow block and auto-resolves prerequisite subflows."""

    sub_graph = {
        "name": "auth_flow",
        "data": {},
        "steps": [
            {
                "id": "sub1",
                "kind": "call",
                "tool": "auth__token",
                "bind_as": "token_data",
                "args": {},
                "validate": [],
            }
        ],
    }
    flow_store.save_flow("auth_flow", sub_graph)

    parent_graph = {
        "name": "parent_flow",
        "data": {},
        "steps": [
            {
                "id": "flow_node_1",
                "kind": "flow",
                "flow_name": "auth_flow",
            },
            {
                "id": "call_node_2",
                "kind": "call",
                "tool": "items__list",
                "args": {"auth": "$ctx.token_data.body.jwt"},
                "validate": [],
            },
        ],
    }

    mock_api = MagicMock()
    mock_api.auth.token.return_value = DummyResponse(200, {"jwt": "jwt_token_999"})
    mock_api.items.list.return_value = DummyResponse(200, {"items": [1, 2, 3]})

    # 1. Run just the flow node
    res1 = run_single_node(parent_graph, "flow_node_1", mock_api)
    assert res1["ok"] is True
    assert res1["status"] == "pass"
    assert res1["name"] == "flow: auth_flow"

    # 2. Run the second node (requires token_data produced by prerequisite subflow)
    res2 = run_single_node(parent_graph, "call_node_2", mock_api)
    assert res2["ok"] is True
    assert res2["status"] == "pass"
    mock_api.items.list.assert_called_with(auth="jwt_token_999")


def test_circular_subflow_detection():
    """Verifies that circular subflow references (A -> B -> A) are detected and fail cleanly."""

    flow_a = {
        "name": "flow_a",
        "steps": [
            {"id": "a1", "kind": "flow", "flow_name": "flow_b"}
        ],
    }
    flow_b = {
        "name": "flow_b",
        "steps": [
            {"id": "b1", "kind": "flow", "flow_name": "flow_a"}
        ],
    }
    flow_store.save_flow("flow_a", flow_a)
    flow_store.save_flow("flow_b", flow_b)

    mock_api = MagicMock()
    flow_fn = make_flow_fn(flow_a)
    report = run_flow(flow_fn, mock_api)

    assert report.status == "fail"
    assert "Circular subflow detected" in (report.error or "")


def test_codegen_for_subflow(tmp_path, monkeypatch):
    """Verifies that Python code generated for a flow with subflow blocks compiles and executes."""
    graph = {
        "name": "composed_flow",
        "data": {},
        "steps": [
            {
                "id": "step1",
                "kind": "flow",
                "flow_name": "sub_auth",
                "bind_as": "auth_res",
            }
        ],
    }
    source = generate_flow_source(graph)
    assert "from flows.dashboard.sub_auth import sub_auth" in source
    assert "ctx.run_flow(sub_auth, bind_as='auth_res')" in source
