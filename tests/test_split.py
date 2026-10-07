import time
import pytest
from core.graph_builder import make_flow_fn
from core.codegen import generate_flow_source
from flows.sdk import Call, Check, FlowContext, StepFailed, flow
from runner.executor import run_flow


class MockAPI:
    def __init__(self):
        self.call_log = []

    class MockTag:
        def __init__(self, parent):
            self.parent = parent

        def step_a(self, delay=0.05, value=10):
            if delay > 0:
                time.sleep(delay)
            self.parent.call_log.append(f"step_a_{value}")
            return {"status_code": 200, "result": value * 2}

        def step_b(self, delay=0.05, value=20):
            if delay > 0:
                time.sleep(delay)
            self.parent.call_log.append(f"step_b_{value}")
            return {"status_code": 200, "result": value + 5}

        def step_fail(self):
            self.parent.call_log.append("step_fail")
            return {"status_code": 500, "error": "server error"}

    @property
    def test_tag(self):
        return self.MockTag(self)


def test_flowcontext_sequential_split():
    api = MockAPI()
    ctx = FlowContext(api, data={"seed": 1})

    def b1(c):
        c.run(Call(tool=api.test_tag.step_a, args={"value": 5}, bind_as="res1"))

    def b2(c):
        # Reads res1 bound by b1 in sequential mode
        val = c._bound.get("res1", {}).get("result", 0)
        c.run(Call(tool=api.test_tag.step_b, args={"value": val}, bind_as="res2"))

    ctx.run_split([("Branch 1", b1), ("Branch 2", b2)], mode="sequential", name="Test Sequential")

    assert api.call_log == ["step_a_5", "step_b_10"]
    assert ctx._bound["res1"]["result"] == 10
    assert ctx._bound["res2"]["result"] == 15
    assert len(ctx.steps) == 1
    assert ctx.steps[0].kind == "split"
    assert ctx.steps[0].status == "pass"
    assert len(ctx.steps[0].children) == 2


def test_flowcontext_parallel_split():
    api = MockAPI()
    ctx = FlowContext(api, data={"seed": 1})

    def b1(c):
        c.run(Call(tool=api.test_tag.step_a, args={"delay": 0.1, "value": 1}, bind_as="p1"))

    def b2(c):
        c.run(Call(tool=api.test_tag.step_b, args={"delay": 0.1, "value": 2}, bind_as="p2"))

    t0 = time.time()
    ctx.run_split([("Branch 1", b1), ("Branch 2", b2)], mode="parallel", name="Test Parallel")
    elapsed = time.time() - t0

    # Two 0.1s steps run concurrently; should complete in under 0.18s
    assert elapsed < 0.18
    assert "step_a_1" in api.call_log
    assert "step_b_2" in api.call_log
    assert ctx._bound["p1"]["result"] == 2
    assert ctx._bound["p2"]["result"] == 7
    assert ctx.steps[0].status == "pass"


def test_split_branch_failure_marks_split_failed():
    api = MockAPI()
    ctx = FlowContext(api)

    def b_ok(c):
        c.run(Call(tool=api.test_tag.step_a, args={"value": 1}))

    def b_bad(c):
        c.run(Call(tool=api.test_tag.step_fail, validate=[Check(lambda r, ctx: r["status_code"] == 200, "status 200")]))

    with pytest.raises(StepFailed):
        ctx.run_split([("Good Branch", b_ok), ("Bad Branch", b_bad)], mode="parallel")

    split_rep = ctx.steps[0]
    assert split_rep.kind == "split"
    assert split_rep.status == "fail"


def test_graph_builder_with_split():
    api = MockAPI()
    graph = {
        "name": "split_graph_test",
        "data": {},
        "steps": [
            {
                "id": "split_1",
                "kind": "split",
                "name": "Parallel Execution",
                "mode": "parallel",
                "branches": [
                    {
                        "id": "br_1",
                        "name": "Branch A",
                        "steps": [
                            {"id": "s1", "kind": "call", "tool": "test_tag__step_a", "args": {"value": 3}, "bind_as": "out_a"}
                        ],
                    },
                    {
                        "id": "br_2",
                        "name": "Branch B",
                        "steps": [
                            {"id": "s2", "kind": "call", "tool": "test_tag__step_b", "args": {"value": 4}, "bind_as": "out_b"}
                        ],
                    },
                ],
            }
        ],
    }

    flow_fn = make_flow_fn(graph)
    report = run_flow(flow_fn, api)
    assert report.status == "pass"
    assert len(report.steps) == 1
    assert report.steps[0].kind == "split"
    assert report.steps[0].name == "Parallel Execution"
    assert len(report.steps[0].children) == 2


def test_codegen_with_split():
    graph = {
        "name": "codegen_split_test",
        "data": {},
        "steps": [
            {
                "id": "split_step_1",
                "kind": "split",
                "name": "Parallel Lab and Imaging",
                "mode": "parallel",
                "branches": [
                    {
                        "id": "b1",
                        "name": "Order Lab",
                        "steps": [
                            {"id": "s1", "kind": "call", "tool": "test_tag__step_a", "args": {"value": 10}}
                        ],
                    },
                    {
                        "id": "b2",
                        "name": "Order Imaging",
                        "steps": [
                            {"id": "s2", "kind": "call", "tool": "test_tag__step_b", "args": {"value": 20}}
                        ],
                    },
                ],
            }
        ],
    }

    code = generate_flow_source(graph)
    assert "ctx.run_split(" in code
    assert "mode='parallel'" in code
    # Check that the generated code is syntactically valid Python
    compiled = compile(code, "<string>", "exec")
    assert compiled is not None
