from unittest.mock import MagicMock
import pytest

from core.refs import resolve_value, ref_to_py_source, ref_to_py_expr, eval_resolved
from core.client.base_client import StepResult
from flows.sdk import Call, FlowContext, get_path
from core.graph_builder import make_flow_fn, run_single_node, clear_flow_session
from core.codegen import generate_flow_source


class DummyResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.body = data

    def json(self):
        return self._data


def test_resolve_value_nested_dict_and_list():
    """Verify resolve_value recurses into nested dicts and lists, preserving types."""
    raw = {
        "order_name": "Test Order",
        "items": [
            {
                "lab_test_id": "$ctx.catalog_test.body.id",
                "quantity": 2,
                "nested_meta": {
                    "code": "$ctx.catalog_test.body.code",
                    "flag": True,
                },
            }
        ],
        "tags": ["$data.env", "urgent"],
    }

    resolved = resolve_value(raw)

    ctx = FlowContext(api=MagicMock(), data={"env": "staging"})
    ctx._bound["catalog_test"] = DummyResponse({"id": 105, "code": "CBC_01"})

    evaluated = eval_resolved(resolved, ctx)

    assert evaluated["order_name"] == "Test Order"
    assert len(evaluated["items"]) == 1
    assert evaluated["items"][0]["lab_test_id"] == 105
    assert isinstance(evaluated["items"][0]["lab_test_id"], int)
    assert evaluated["items"][0]["quantity"] == 2
    assert evaluated["items"][0]["nested_meta"]["code"] == "CBC_01"
    assert evaluated["items"][0]["nested_meta"]["flag"] is True
    assert evaluated["tags"] == ["staging", "urgent"]


def test_flowcontext_resolves_nested_callables_and_raw_strings():
    """Verify FlowContext._resolve_args recursively resolves nested callables and raw $-ref strings."""
    ctx = FlowContext(api=MagicMock(), data={"env": "prod"})
    ctx._bound["catalog_test"] = DummyResponse({"id": 999, "name": "Lipid Panel"})

    # 1. Call with resolved callables in nested list
    resolved_structure = resolve_value({
        "items": [{"lab_test_id": "$ctx.catalog_test.body.id"}]
    })
    call1 = Call(tool=MagicMock(), args=resolved_structure)
    res1 = ctx._resolve_args(call1.args)
    assert res1 == {"items": [{"lab_test_id": 999}]}
    assert isinstance(res1["items"][0]["lab_test_id"], int)

    # 2. Call with raw ref strings directly inside nested list (no prior resolve_value)
    call2 = Call(tool=MagicMock(), args={"items": [{"lab_test_id": "$ctx.catalog_test.body.id"}]})
    res2 = ctx._resolve_args(call2.args)
    assert res2 == {"items": [{"lab_test_id": 999}]}
    assert isinstance(res2["items"][0]["lab_test_id"], int)


def test_end_to_end_flow_with_nested_ref():
    """Verify a complete flow execution where step 2 consumes an integer ID from step 1 in a nested list."""
    received_order_args = {}

    class MockCatalogAPI:
        def create_test(self, name):
            return StepResult(
                status_code=201,
                body={"id": 404, "name": name},
                url="http://mock/catalog",
                method="POST",
            )

    class MockOrderAPI:
        def place_order(self, **kwargs):
            nonlocal received_order_args
            received_order_args = dict(kwargs)
            return StepResult(
                status_code=200,
                body={"order_id": "ORD-123", "status": "confirmed"},
                url="http://mock/orders",
                method="POST",
            )

    class MockAPI:
        def __init__(self):
            self.catalog = MockCatalogAPI()
            self.order = MockOrderAPI()

    api = MockAPI()
    graph = {
        "name": "place_order_flow",
        "data": {"test_name": "Thyroid Panel"},
        "steps": [
            {
                "id": "step_catalog",
                "kind": "call",
                "tool": "catalog__create_test",
                "bind_as": "catalog_test",
                "args": {"name": "$data.test_name"},
                "validate": [{"id": "chk_status", "kind": "check", "check_type": "status", "status": 201}],
            },
            {
                "id": "step_order",
                "kind": "call",
                "tool": "order__place_order",
                "bind_as": "placed_order",
                "args": {
                    "items": [
                        {
                            "lab_test_id": "$ctx.catalog_test.body.id",
                            "quantity": 1,
                        }
                    ]
                },
                "validate": [{"id": "chk_order", "kind": "check", "check_type": "status", "status": 200}],
            },
        ],
    }

    flow_fn = make_flow_fn(graph)
    ctx = FlowContext(api=api, data=dict(graph["data"]))
    flow_fn(ctx)

    assert "items" in received_order_args
    assert len(received_order_args["items"]) == 1
    assert received_order_args["items"][0]["lab_test_id"] == 404
    assert isinstance(received_order_args["items"][0]["lab_test_id"], int)
    assert received_order_args["items"][0]["quantity"] == 1


def test_codegen_with_nested_refs():
    """Verify codegen outputs valid Python syntax for nested references with get_path."""
    graph = {
        "name": "codegen_test_flow",
        "steps": [
            {
                "id": "step_order",
                "kind": "call",
                "tool": "order__place_order",
                "args": {
                    "items": [
                        {
                            "lab_test_id": "$ctx.catalog_test.body.id",
                            "quantity": 2,
                        }
                    ]
                },
            }
        ],
    }

    code = generate_flow_source(graph)

    # Must import get_path
    assert "get_path" in code
    # Must render nested lambda
    assert "'items': [{'lab_test_id': lambda ctx: get_path(ctx.catalog_test.json(), 'id'), 'quantity': 2}]" in code

    # Verify generated Python code compiles cleanly
    compiled = compile(code, "<string>", "exec")
    assert compiled is not None


def test_string_interpolation():
    """Verify embedded $ctx... references in strings interpolate properly."""
    ctx = FlowContext(api=MagicMock(), data={"user": "Alice"})
    ctx._bound["auth"] = DummyResponse({"token": "secret_jwt_xyz", "id": 12})

    fn = resolve_value("Bearer $ctx.auth.body.token")
    assert callable(fn)
    assert fn(ctx) == "Bearer secret_jwt_xyz"

    fn2 = resolve_value("User $data.user has ID $ctx.auth.body.id")
    assert callable(fn2)
    assert fn2(ctx) == "User Alice has ID 12"

    # Python source generation
    py_src = ref_to_py_source("Bearer $ctx.auth.body.token")
    assert "str(get_path(ctx.auth.json(), 'token'))" in py_src


def test_run_single_node_with_nested_ref():
    """Verify run_single_node resolves nested refs across prerequisite steps."""
    received_order_args = {}

    class MockCatalogAPI:
        def create_test(self, name):
            return StepResult(
                status_code=201,
                body={"id": 888, "name": name},
                url="http://mock/catalog",
                method="POST",
            )

    class MockOrderAPI:
        def place_order(self, **kwargs):
            nonlocal received_order_args
            received_order_args = dict(kwargs)
            return StepResult(
                status_code=200,
                body={"order_id": "ORD-888", "items": kwargs.get("items")},
                url="http://mock/orders",
                method="POST",
            )

    class MockAPI:
        def __init__(self):
            self.catalog = MockCatalogAPI()
            self.order = MockOrderAPI()

    api = MockAPI()
    clear_flow_session("single_node_nested_test")
    graph = {
        "name": "single_node_nested_test",
        "data": {"test_name": "CBC"},
        "steps": [
            {
                "id": "step_catalog",
                "kind": "call",
                "tool": "catalog__create_test",
                "bind_as": "catalog_test",
                "args": {"name": "$data.test_name"},
                "validate": [],
            },
            {
                "id": "step_order",
                "kind": "call",
                "tool": "order__place_order",
                "bind_as": "placed_order",
                "args": {
                    "items": [
                        {
                            "lab_test_id": "$ctx.catalog_test.body.id",
                            "quantity": 3,
                        }
                    ]
                },
                "validate": [
                    {"id": "chk_code", "kind": "check", "check_type": "status", "status": 200},
                ],
            },
        ],
    }

    # Run only step_order directly from UI
    res = run_single_node(graph, "step_order", api)
    assert res["ok"] is True
    assert res["status"] == "pass"
    assert received_order_args["items"][0]["lab_test_id"] == 888
    assert isinstance(received_order_args["items"][0]["lab_test_id"], int)
    assert res["response"]["body"]["items"][0]["lab_test_id"] == 888

