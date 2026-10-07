import pytest
from dataclasses import dataclass
from fastapi.testclient import TestClient

from core.graph_builder import run_single_node, clear_flow_session
from core.client.base_client import StepResult
from core import workspace_store
from ui_server.app import app
from ui_server import assistant

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_run_node_workspace():
    ws_id = "test_run_node_isolated"
    if workspace_store.get_workspace_dir(ws_id).exists():
        workspace_store.delete_workspace(ws_id)
    workspace_store.create_workspace(ws_id, name="Run Node Test Workspace")
    workspace_store.set_active_workspace_id(ws_id)
    try:
        yield ws_id
    finally:
        workspace_store.set_active_workspace_id("default")
        workspace_store.delete_workspace(ws_id)

class MockAuthAPI:
    def login(self, username, password):
        if username == "admin" and password == "secret":
            return StepResult(status_code=200, headers={"content-type": "application/json"},
                              body={"access_token": "token_123", "user_id": 42}, url="http://mock/login", method="POST")
        return StepResult(status_code=401, headers={"content-type": "application/json"},
                          body={"detail": "invalid credentials"}, url="http://mock/login", method="POST")

class MockLabAPI:
    def create_lab(self, token, name):
        if token == "token_123":
            if name == "existing":
                return StepResult(status_code=400, headers={"content-type": "application/json"},
                                  body={"detail": "already exists"}, url="http://mock/labs", method="POST")
            return StepResult(status_code=201, headers={"content-type": "application/json"},
                              body={"id": 99, "name": name}, url="http://mock/labs", method="POST")
        return StepResult(status_code=401, headers={"content-type": "application/json"},
                          body={"detail": "unauthorized"}, url="http://mock/labs", method="POST")

class MockAPI:
    def __init__(self):
        self.auth = MockAuthAPI()
        self.lab = MockLabAPI()

@pytest.fixture
def mock_api():
    return MockAPI()

def test_run_single_node_standalone(mock_api):
    clear_flow_session("test_flow")
    graph = {
        "name": "test_flow",
        "data": {"user": "admin", "pwd": "secret"},
        "steps": [
            {
                "id": "step_1",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "login_res",
                "args": {"username": "$data.user", "password": "$data.pwd"},
                "validate": [
                    {"id": "check_1", "kind": "check", "check_type": "status", "status": 200},
                    {"id": "check_2", "kind": "check", "check_type": "json", "path": "access_token", "not_none": True},
                ]
            }
        ]
    }

    res = run_single_node(graph, "step_1", mock_api)
    assert res["ok"] is True
    assert res["status"] == "pass"
    assert res["response"]["status_code"] == 200
    assert res["response"]["body"]["access_token"] == "token_123"
    assert res["response"]["url"] == "http://mock/login"
    assert len(res["report"]["children"]) == 2

def test_run_single_node_with_prerequisites(mock_api):
    clear_flow_session("test_chain_flow")
    graph = {
        "name": "test_chain_flow",
        "data": {"lab_name": "existing"},
        "steps": [
            {
                "id": "step_login",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "admin_login",
                "args": {"username": "admin", "password": "secret"},
                "validate": []
            },
            {
                "id": "step_create_lab",
                "kind": "call",
                "tool": "lab__create_lab",
                "bind_as": "create_res",
                "args": {
                    "token": "$ctx.admin_login.body.access_token",
                    "name": "$data.lab_name"
                },
                "validate": [
                    {"id": "check_status", "kind": "check", "check_type": "status", "status": 201}
                ]
            }
        ]
    }

    # Running step_create_lab directly will auto-run step_login first to populate admin_login
    res = run_single_node(graph, "step_create_lab", mock_api)
    # The API returns 400 'already exists', so the 201 check fails, but response is captured!
    assert res["status"] == "fail"
    assert res["response"]["status_code"] == 400
    assert res["response"]["body"] == {"detail": "already exists"}
    assert res["response"]["method"] == "POST"

def test_api_endpoint_run_single_node(mock_api, monkeypatch):
    monkeypatch.setattr("ui_server.app.build_api_client", lambda: mock_api)
    clear_flow_session("endpoint_test_flow")
    graph = {
        "name": "endpoint_test_flow",
        "data": {},
        "steps": [
            {
                "id": "node_test_1",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "login",
                "args": {"username": "admin", "password": "secret"},
                "validate": []
            }
        ]
    }

    res = client.post(
        "/api/flows/endpoint_test_flow/nodes/node_test_1/run",
        json={"graph": graph}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["response"]["status_code"] == 200
    assert data["response"]["body"]["user_id"] == 42

def test_assistant_execute_run_node_tool(mock_api, monkeypatch):
    monkeypatch.setattr("core.client.factory.build_api_client", lambda: mock_api)
    clear_flow_session("assistant_test_flow")
    graph = {
        "name": "assistant_test_flow",
        "data": {},
        "steps": [
            {
                "id": "step_a",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "res",
                "args": {"username": "admin", "password": "wrong_password"},
                "validate": []
            }
        ]
    }

    res = assistant._execute_tool("run_node", {"node_id": "step_a"}, graph)
    # Assistant receives the exact response: status_code 401, detail: invalid credentials
    assert res["status"] == "pass" or res["status"] == "fail"
    assert res["response"]["status_code"] == 401
    assert res["response"]["body"] == {"detail": "invalid credentials"}


def test_assistant_clean_up_and_new_flow():
    graph = {
        "name": "dirty_flow",
        "data": {},
        "steps": [
            {
                "id": "s1",
                "kind": "call",
                "tool": "auth__login",
                "pos": {"x": 500, "y": 900},
                "hide_validators": False,
                "collapse_params": False,
                "collapse_json": False,
                "validate": [
                    {"id": "v1", "kind": "check", "check_type": "status", "status": 200}
                ]
            }
        ]
    }
    # Test clean_up_flow
    res = assistant._execute_tool("clean_up_flow", {}, graph)
    assert res["ok"] is True
    step = graph["steps"][0]
    assert step["hide_validators"] is True
    assert step["collapse_params"] is True
    assert step["collapse_json"] is True
    assert step["pos"]["x"] == 80
    assert step["pos"]["y"] == 80

    # Test new_flow
    res_new = assistant._execute_tool("new_flow", {"name": "clean_slate_flow"}, graph)
    assert res_new["ok"] is True
    assert graph["name"] == "clean_slate_flow"
    assert graph["steps"] == []

