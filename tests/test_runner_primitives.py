from types import SimpleNamespace

from core.codegen import generate_flow_source
from core.flow_validation import validate_graph
from core.graph_builder import make_flow_fn
from runner.executor import run_flow


def _response(status=200, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body or {})


def test_assertion_condition_fails_and_finally_runs():
    cleaned = []
    api = SimpleNamespace(
        api=SimpleNamespace(create=lambda: _response(200, {"patient_id": 7}),
                            cleanup=lambda: (cleaned.append(True) or _response(204))),
    )
    graph = {
        "name": "primitive_assert", "steps": [
            {"id": "create", "kind": "call", "tool": "api__create", "bind_as": "created", "args": {}, "validate": []},
            {"id": "assert", "kind": "condition", "left": "$ctx.created.body.patient_id", "op": "==", "right": 8, "assert": True},
        ],
        "finally_steps": [{"id": "cleanup", "kind": "call", "tool": "api__cleanup", "args": {}, "validate": []}],
    }
    report = run_flow(make_flow_fn(graph), api)
    assert report.status == "fail"
    assert cleaned == [True]
    assert any(step.id == "assert" and step.status == "fail" for step in report.steps)


def test_dynamic_json_and_persistence_validators():
    calls = []
    def create():
        return _response(200, {"patient_id": 7})
    def patient_state(patient_id):
        calls.append(patient_id)
        return _response(200, {"patient_id": patient_id, "exists": True})

    api = SimpleNamespace(api=SimpleNamespace(create=create), testing=SimpleNamespace(patient_state=patient_state))
    graph = {"name": "primitive_persistence", "steps": [{
        "id": "create", "kind": "call", "tool": "api__create", "bind_as": "created", "args": "", "validate": [
            {"id": "same", "kind": "check", "check_type": "json_path", "path": "patient_id", "equals": "$ctx.created.body.patient_id"},
            {"id": "state", "kind": "check", "check_type": "persistence", "tool": "testing__patient_state",
             "args": {"patient_id": "$ctx.created.body.patient_id"}, "path": "patient_id", "equals": "$ctx.created.body.patient_id"},
        ],
    }]}
    report = run_flow(make_flow_fn(graph), api)
    assert report.status == "pass"
    assert calls == [7]


def test_retry_replays_failed_call_and_codegen_compiles():
    attempts = []
    def flaky():
        attempts.append(1)
        return _response(200 if len(attempts) == 2 else 500)
    api = SimpleNamespace(api=SimpleNamespace(flaky=flaky))
    graph = {"name": "primitive_retry", "steps": [{
        "id": "flaky", "kind": "call", "tool": "api__flaky", "args": {},
        "retry": {"attempts": 2, "initial_delay_s": 0, "backoff": 1},
        "validate": [{"id": "ok", "kind": "check", "check_type": "status", "status": 200}],
    }]}
    report = run_flow(make_flow_fn(graph), api)
    assert report.status == "pass"
    assert len(attempts) == 2
    assert report.steps[0].kind == "retry"
    compile(generate_flow_source(graph), "generated", "exec")


def test_openapi_preflight_reports_missing_required_arguments():
    graph = {"steps": [{
        "id": "dependent", "kind": "call",
        "tool": "patients__add_dependent_api_v1_patient_patients_dependents_post", "args": {},
    }]}
    issues = validate_graph(graph, "default")
    assert issues and issues[0]["node_id"] == "dependent"
    assert "full_name" in issues[0]["fields"]
