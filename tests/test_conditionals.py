import pytest
from types import SimpleNamespace

from core.refs import eval_condition, render_condition_py
from core.graph_builder import make_flow_fn
from core.codegen import generate_flow_source
from flows.sdk import FlowContext
from core import workspace_store


@pytest.fixture(autouse=True)
def isolated_conditionals_workspace():
    ws_id = "test_conditionals_isolated"
    if workspace_store.get_workspace_dir(ws_id).exists():
        workspace_store.delete_workspace(ws_id)
    workspace_store.create_workspace(ws_id, name="Conditionals Test WS")
    workspace_store.set_active_workspace_id(ws_id)
    try:
        yield ws_id
    finally:
        workspace_store.set_active_workspace_id("default")
        workspace_store.delete_workspace(ws_id)


def test_eval_condition():
    # Equals
    assert eval_condition("already exists", "==", "already exists") is True
    assert eval_condition("already exists", "==", "different") is False
    assert eval_condition(200, "==", 200) is True
    assert eval_condition("200", "==", 200) is True

    # Not equals
    assert eval_condition("abc", "!=", "xyz") is True
    assert eval_condition("abc", "!=", "abc") is False

    # Contains
    assert eval_condition("already exists in db", "contains", "already exists") is True
    assert eval_condition({"error": "conflict"}, "contains", "error") is True
    assert eval_condition(["a", "b"], "contains", "a") is True

    # Comparison
    assert eval_condition(10, ">", 5) is True
    assert eval_condition(5, "<=", 5) is True

    # Truthy / Falsy
    assert eval_condition("token123", "is_truthy", None) is True
    assert eval_condition("", "is_falsy", None) is True
    assert eval_condition(None, "is_falsy", None) is True


def test_render_condition_py():
    assert render_condition_py("$ctx.step.status_code", "==", 200) == "ctx.step.status_code == 200"
    assert render_condition_py("$ctx.login.body.detail", "==", "exists") == "get_path(ctx.login.json(), 'detail') == 'exists'"
    assert render_condition_py("$ctx.login.body", "contains", "token") == "'token' in ctx.login.json()"


def test_graph_builder_and_execution_then_branch():
    # Mock API
    called = []
    def fake_login():
        return SimpleNamespace(status_code=200, json=lambda: {"detail": "already exists"})
    def fake_step_then():
        called.append("then")
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})
    def fake_step_else():
        called.append("else")
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

    api = SimpleNamespace(
        auth=SimpleNamespace(login=fake_login),
        lab=SimpleNamespace(step_then=fake_step_then, step_else=fake_step_else)
    )

    graph = {
        "name": "test_cond_flow",
        "data": {},
        "steps": [
            {
                "id": "s1",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "login_res",
                "args": {},
                "validate": []
            },
            {
                "id": "c1",
                "kind": "condition",
                "left": "$ctx.login_res.body.detail",
                "op": "==",
                "right": "already exists",
                "then_steps": [
                    {
                        "id": "t1",
                        "kind": "call",
                        "tool": "lab__step_then",
                        "args": {},
                        "validate": []
                    }
                ],
                "else_steps": [
                    {
                        "id": "e1",
                        "kind": "call",
                        "tool": "lab__step_else",
                        "args": {},
                        "validate": []
                    }
                ]
            }
        ]
    }

    flow_fn = make_flow_fn(graph)
    ctx = FlowContext(api=api)
    flow_fn(ctx)

    assert called == ["then"]
    assert any(s.kind == "condition" and "TRUE" in s.name for s in ctx.steps)


def test_graph_builder_and_execution_else_branch():
    called = []
    def fake_login():
        return SimpleNamespace(status_code=200, json=lambda: {"detail": "created"})
    def fake_step_then():
        called.append("then")
        return SimpleNamespace(status_code=200, json=lambda: {})
    def fake_step_else():
        called.append("else")
        return SimpleNamespace(status_code=200, json=lambda: {})

    api = SimpleNamespace(
        auth=SimpleNamespace(login=fake_login),
        lab=SimpleNamespace(step_then=fake_step_then, step_else=fake_step_else)
    )

    graph = {
        "name": "test_cond_else_flow",
        "data": {},
        "steps": [
            {
                "id": "s1",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "login_res",
                "args": {},
                "validate": []
            },
            {
                "id": "c1",
                "kind": "condition",
                "left": "$ctx.login_res.body.detail",
                "op": "==",
                "right": "already exists",
                "then_steps": [
                    {
                        "id": "t1",
                        "kind": "call",
                        "tool": "lab__step_then",
                        "args": {},
                        "validate": []
                    }
                ],
                "else_steps": [
                    {
                        "id": "e1",
                        "kind": "call",
                        "tool": "lab__step_else",
                        "args": {},
                        "validate": []
                    }
                ]
            }
        ]
    }

    flow_fn = make_flow_fn(graph)
    ctx = FlowContext(api=api)
    flow_fn(ctx)

    assert called == ["else"]
    assert any(s.kind == "condition" and "ELSE" in s.name for s in ctx.steps)


def test_codegen_with_condition():
    graph = {
        "name": "test_codegen",
        "data": {"foo": "bar"},
        "steps": [
            {
                "id": "s1",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "login",
                "args": {},
                "validate": []
            },
            {
                "id": "c1",
                "kind": "condition",
                "left": "$ctx.login.status_code",
                "op": "==",
                "right": 200,
                "then_steps": [
                    {
                        "id": "t1",
                        "kind": "call",
                        "tool": "lab__create",
                        "args": {},
                        "validate": []
                    }
                ],
                "else_steps": []
            }
        ]
    }
    src = generate_flow_source(graph)
    assert "if ctx.login.status_code == 200:" in src
    assert "ctx.run(" in src
    # Verify syntax by compiling generated source
    compile(src, "<string>", "exec")


def test_flow_store_save_and_load_with_condition():
    from core import flow_store as store
    flow_name = "test_condition_roundtrip"
    graph = {
        "name": flow_name,
        "data": {"user": "admin"},
        "steps": [
            {
                "id": "s1",
                "kind": "call",
                "tool": "auth__login",
                "bind_as": "login_res",
                "args": {"username": "$data.user"},
                "validate": []
            },
            {
                "id": "c1",
                "kind": "condition",
                "left": "$ctx.login_res.body.detail",
                "op": "==",
                "right": "already exists",
                "then_steps": [
                    {
                        "id": "t1",
                        "kind": "call",
                        "tool": "lab__create",
                        "args": {},
                        "validate": []
                    }
                ],
                "else_steps": []
            }
        ]
    }
    try:
        store.save_flow(flow_name, graph)
        loaded = store.load_flow(flow_name)
        assert loaded["name"] == flow_name
        assert len(loaded["steps"]) == 2
        assert loaded["steps"][1]["kind"] == "condition"
        assert loaded["steps"][1]["then_steps"][0]["tool"] == "lab__create"

        _, py_path = store._paths(flow_name)
        py_content = py_path.read_text()
        assert "if get_path(ctx.login_res.json(), 'detail') == 'already exists':" in py_content
    finally:
        store.delete_flow(flow_name)


def test_array_index_references():
    from core.refs import resolve_value, ref_to_py_source
    from flows.sdk import FlowContext, get_path

    class MockResp:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data

    ctx = FlowContext(api=None, data={"arr": ["a", "b", "c"]})
    ctx._bound["prebuilt_cbc"] = MockResp({
        "items": [
            {"id": "cbc_001", "name": "CBC Panel"},
            {"id": "cbc_002", "name": "CBC with Diff"}
        ]
    })

    # Test resolve_value with array bracket notation
    val_fn = resolve_value("$ctx.prebuilt_cbc.body.items[0].id")
    assert callable(val_fn)
    assert val_fn(ctx) == "cbc_001"

    val_fn2 = resolve_value("$ctx.prebuilt_cbc.body.items[1].name")
    assert val_fn2(ctx) == "CBC with Diff"

    # Test resolve_value with $data array indexing
    val_fn_data = resolve_value("$data.arr[2]")
    assert val_fn_data(ctx) == "c"

    # Test py source generation
    py_src = ref_to_py_source("$ctx.prebuilt_cbc.body.items[0].id")
    assert "get_path(ctx.prebuilt_cbc.json(), 'items[0].id')" in py_src


