"""Flow authoring SDK.

A flow is a plain Python function decorated with @flow. It calls ctx.run(Call(...))
for each action. A Call can carry a `validate` list mixing:
  - Check: a plain, non-MCP assertion against the just-produced result
  - Call: a nested tool call (e.g. a DB check) that is itself validated the same way

Semantics (deliberately simple, no cleverness):
  - Within one Call's `validate` list, the first failing Check/nested-Call stops
    that chain -- remaining validators in the list are not run.
  - A top-level step (a ctx.run(...) call in the flow body) that fails aborts the
    rest of the flow. Later top-level steps are not attempted.
"""

from __future__ import annotations

import traceback
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Union

_UNSET = object()


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

@dataclass
class StepReport:
    kind: str  # "call" | "check"
    name: str
    status: str  # "pass" | "fail"
    error: str | None = None
    children: list["StepReport"] = field(default_factory=list)
    id: str | None = None  # echoes Call.id/Check.id, for UIs correlating back to a source node
    response: dict | None = None


@dataclass
class FlowReport:
    name: str
    status: str  # "pass" | "fail"
    steps: list[StepReport] = field(default_factory=list)
    duration_s: float = 0.0
    error: str | None = None


class StepFailed(Exception):
    """Raised by ctx.run() when a top-level step fails, to abort the flow."""

    def __init__(self, report: StepReport):
        self.report = report
        super().__init__(report.name)


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------

@dataclass
class Check:
    """A non-MCP assertion evaluated against the most recent result."""

    fn: Callable[[Any, "FlowContext"], bool]
    description: str = ""
    id: str | None = None

    def evaluate(self, result: Any, ctx: "FlowContext") -> StepReport:
        try:
            ok = bool(self.fn(result, ctx))
        except Exception as exc:
            return StepReport(kind="check", name=self.description or "check",
                               status="fail", error=f"raised {exc!r}", id=self.id)
        err = None
        if not ok:
            if "status_code" in self.description and hasattr(result, "status_code"):
                err = f"got status_code={result.status_code}"
            elif hasattr(result, "body") and result.body is not None:
                err = f"response body was {result.body!r}"
        return StepReport(kind="check", name=self.description or "check",
                           status="pass" if ok else "fail", error=err, id=self.id)


@dataclass
class Call:
    """An MCP tool call. May itself be used as a validator inside another Call."""

    tool: Callable
    args: dict = field(default_factory=dict)
    bind_as: str | None = None
    name: str | None = None
    validate: list["Validator"] = field(default_factory=list)
    id: str | None = None


Validator = Union[Check, Call]


# --------------------------------------------------------------------------
# Execution context
# --------------------------------------------------------------------------

class FlowContext:
    def __init__(self, api: Any, data: dict | None = None,
                 on_step: Callable[[StepReport], None] | None = None,
                 bound: dict | None = None,
                 workspace_data: dict | None = None,
                 secrets: dict | None = None):
        self.api = api
        self.workspace_data = dict(workspace_data or {})
        self.secrets = dict(secrets or {})
        self.flow_data = dict(data or {})
        self.data = {**self.workspace_data, **self.flow_data}
        self.steps: list[StepReport] = []
        self._bound: dict[str, Any] = dict(bound or {})
        self._on_step = on_step

    def __getattr__(self, name: str) -> Any:
        try:
            return self._bound[name]
        except KeyError:
            raise AttributeError(name)

    def run(self, call: Call) -> Any:
        report = self._execute_call(call)
        self.steps.append(report)
        if report.status == "fail":
            raise StepFailed(report)
        return self._bound.get(call.bind_as) if call.bind_as else None

    def wait(self, seconds: float, *, name: str = "wait", id: str | None = None) -> None:
        """Record and perform a bounded, explicit delay in a flow."""
        seconds = max(0.0, float(seconds))
        time.sleep(seconds)
        report = StepReport(kind="wait", name=f"{name} ({seconds:g}s)", status="pass", id=id)
        self.steps.append(report)
        if self._on_step:
            self._on_step(report)

    def run_with_retry(self, fn: Callable[[], Any], *, attempts: int = 3,
                       initial_delay_s: float = 0.25, backoff: float = 2,
                       name: str = "retry", id: str | None = None) -> Any:
        """Run ``fn`` again after StepFailed, exposing one aggregate retry step."""
        attempts = max(1, int(attempts))
        delay = max(0.0, float(initial_delay_s))
        children: list[StepReport] = []
        last_error = None
        for attempt in range(1, attempts + 1):
            start, bound_before = len(self.steps), dict(self._bound)
            try:
                result = fn()
                children.extend(self.steps[start:])
                del self.steps[start:]
                report = StepReport(kind="retry", name=f"{name} ({attempt}/{attempts})", status="pass", children=children, id=id)
                self.steps.append(report)
                if self._on_step:
                    self._on_step(report)
                return result
            except StepFailed as exc:
                children.extend(self.steps[start:])
                del self.steps[start:]
                self._bound = bound_before
                last_error = exc.report.error or exc.report.name
                if attempt < attempts and delay:
                    self.wait(delay, name="retry backoff")
                delay *= max(1.0, float(backoff))
        report = StepReport(kind="retry", name=name, status="fail",
                            error=f"failed after {attempts} attempts: {last_error}",
                            children=children, id=id)
        self.steps.append(report)
        if self._on_step:
            self._on_step(report)
        raise StepFailed(report)

    def run_flow(self, flow_fn: Callable, id: str | None = None, bind_as: str | None = None) -> Any:
        flow_name = getattr(flow_fn, "__flow_name__", getattr(flow_fn, "__name__", "flow"))
        start_step_idx = len(self.steps)
        prev_bound = dict(self._bound)
        try:
            flow_fn(self)
        except StepFailed as sf:
            child_reports = self.steps[start_step_idx:]
            self.steps = self.steps[:start_step_idx]
            report = StepReport(
                kind="flow",
                name=f"flow: {flow_name}",
                status="fail",
                error=sf.report.error or f"Step '{sf.report.name}' failed in flow '{flow_name}'",
                children=child_reports,
                id=id,
            )
            self.steps.append(report)
            if self._on_step:
                self._on_step(report)
            raise StepFailed(report)
        except Exception as exc:
            child_reports = self.steps[start_step_idx:]
            self.steps = self.steps[:start_step_idx]
            report = StepReport(
                kind="flow",
                name=f"flow: {flow_name}",
                status="fail",
                error=str(exc),
                children=child_reports,
                id=id,
            )
            self.steps.append(report)
            if self._on_step:
                self._on_step(report)
            raise StepFailed(report)

        child_reports = self.steps[start_step_idx:]
        self.steps = self.steps[:start_step_idx]
        report = StepReport(
            kind="flow",
            name=f"flow: {flow_name}",
            status="pass",
            children=child_reports,
            id=id,
        )
        self.steps.append(report)
        if self._on_step:
            self._on_step(report)

        if bind_as:
            diff = {k: v for k, v in self._bound.items() if k not in prev_bound or self._bound[k] != prev_bound.get(k)}
            self._bound[bind_as] = diff
        return self._bound.get(bind_as) if bind_as else None

    def run_split(
        self,
        branches: list[Any],
        mode: str = "parallel",
        id: str | None = None,
        name: str | None = None,
    ) -> list[StepReport]:
        """Execute multiple branches either sequentially or concurrently in parallel.

        Args:
            branches: List of (branch_name, branch_fn) tuples or branch_fn callables.
            mode: "parallel" (concurrent thread execution) or "sequential" (in-order execution).
            id: Node id for UI correlation.
            name: Human-readable name for the split block.
        """
        import concurrent.futures

        mode_clean = (mode or "parallel").lower().strip()
        split_name = name or f"split ({mode_clean})"
        branch_reports: list[StepReport] = []
        has_failure = False
        failure_error = None

        if mode_clean == "parallel":
            def execute_branch(idx: int, item: Any):
                if isinstance(item, tuple) and len(item) == 2:
                    b_name, b_fn = item
                else:
                    b_name, b_fn = f"Branch {idx + 1}", item

                branch_ctx = FlowContext(
                    api=self.api,
                    data=dict(self.data),
                    bound=dict(self._bound),
                )
                b_start = len(branch_ctx.steps)
                b_status = "pass"
                b_err = None
                try:
                    b_fn(branch_ctx)
                except StepFailed as sf:
                    b_status = "fail"
                    b_err = sf.report.error or f"Step '{sf.report.name}' failed in branch '{b_name}'"
                except Exception as exc:
                    b_status = "fail"
                    b_err = str(exc)

                b_children = branch_ctx.steps[b_start:]
                rep = StepReport(
                    kind="branch",
                    name=b_name,
                    status=b_status,
                    error=b_err,
                    children=b_children,
                )
                return idx, rep, dict(branch_ctx._bound)

            max_workers = max(1, len(branches))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(execute_branch, i, b) for i, b in enumerate(branches)]
                results = [f.result() for f in futures]

            results.sort(key=lambda r: r[0])
            for _, rep, bound_updates in results:
                branch_reports.append(rep)
                if rep.status == "fail":
                    has_failure = True
                    if not failure_error:
                        failure_error = rep.error
                self._bound.update(bound_updates)

        else:  # sequential
            for idx, item in enumerate(branches):
                if isinstance(item, tuple) and len(item) == 2:
                    b_name, b_fn = item
                else:
                    b_name, b_fn = f"Branch {idx + 1}", item

                b_start = len(self.steps)
                b_status = "pass"
                b_err = None
                try:
                    b_fn(self)
                except StepFailed as sf:
                    b_status = "fail"
                    b_err = sf.report.error or f"Step '{sf.report.name}' failed in branch '{b_name}'"
                except Exception as exc:
                    b_status = "fail"
                    b_err = str(exc)

                b_children = self.steps[b_start:]
                self.steps = self.steps[:b_start]
                rep = StepReport(
                    kind="branch",
                    name=b_name,
                    status=b_status,
                    error=b_err,
                    children=b_children,
                )
                branch_reports.append(rep)
                if b_status == "fail":
                    has_failure = True
                    if not failure_error:
                        failure_error = b_err
                    break

        split_status = "fail" if has_failure else "pass"
        split_report = StepReport(
            kind="split",
            name=split_name,
            status=split_status,
            error=failure_error,
            children=branch_reports,
            id=id,
        )
        self.steps.append(split_report)
        if self._on_step:
            self._on_step(split_report)

        if has_failure:
            raise StepFailed(split_report)

        return branch_reports

    def _resolve_val(self, val: Any) -> Any:
        if callable(val):
            return val(self)
        if isinstance(val, dict):
            return {k: self._resolve_val(v) for k, v in val.items()}
        if isinstance(val, list):
            return [self._resolve_val(v) for v in val]
        if isinstance(val, tuple):
            return tuple(self._resolve_val(v) for v in val)
        if isinstance(val, str) and (
            val.startswith("$ctx.") or
            val.startswith("$data.") or
            val.startswith("$workspace.") or
            val.startswith("$ws.") or
            val.startswith("$secret.") or
            val.startswith("$secrets.") or
            val.startswith("${")
        ):
            try:
                from core.refs import resolve_value
                res = resolve_value(val)
                if callable(res):
                    return res(self)
                return res
            except Exception:
                pass
        return val

    def _resolve_args(self, args: dict) -> dict:
        return {k: self._resolve_val(v) for k, v in args.items()}

    def _persistence_checks(self, call: Call, result: Any, args: dict) -> list[Check]:
        """Return test-key-backed checks for mutations with durable state.

        A flow's normal response assertions prove the public contract. These
        checks additionally ask the QA-only state API for the row that the
        action claims to have created or changed. Workspaces without that API
        keep their existing behavior.
        """
        operation = getattr(call.tool, "operation", None)
        path = getattr(operation, "path", "")
        method = str(getattr(operation, "method", "")).upper()
        if method not in {"POST", "PATCH", "PUT", "DELETE"} or not hasattr(self.api, "testing"):
            return []
        if not hasattr(result, "status_code") or not 200 <= result.status_code < 300:
            return []
        try:
            body = result.json() or {}
        except Exception:
            return []

        def state_ok(tool_name: str, state_args: dict, predicate: Callable[[dict], bool], label: str) -> Check:
            def fn(_result, _ctx):
                try:
                    response = getattr(self.api.testing, tool_name)(**state_args)
                    return response.status_code == 200 and predicate(response.json() or {})
                except Exception:
                    return False
            return Check(fn, label)

        checks: list[Check] = []
        report_id = body.get("id") if isinstance(body, dict) else None
        if path.startswith("/api/v1/lab/orders") and report_id is not None and "fulfillment_status" in body:
            def report_matches(state: dict) -> bool:
                if state.get("id") != report_id:
                    return False
                if path.endswith("/fulfill"):
                    return state.get("fulfillment_status") == "awaiting_verification" and all(i.get("filled") for i in state.get("items", []))
                if path.endswith("/verify"):
                    return state.get("fulfillment_status") == "ready_to_send" and state.get("verified") is True
                if path.endswith("/send"):
                    return state.get("fulfillment_status") == "sent"
                if path.endswith("/cancel"):
                    return state.get("fulfillment_status") == "cancelled"
                if path.endswith("/reject"):
                    return state.get("fulfillment_status") == "awaiting_results" and bool(state.get("rejection_note"))
                return True
            checks.append(state_ok("report_state", {"report_id": report_id}, report_matches, "QA DB state matches report mutation"))

        if path == "/api/v1/admin/labs" and method == "POST" and report_id is not None:
            checks.append(state_ok("lab_state", {"lab_id": report_id}, lambda state: state.get("id") == report_id and state.get("is_active") is True, "QA DB state contains active lab"))
        if path == "/api/v1/lab/tests" and method == "POST" and report_id is not None:
            checks.append(state_ok("test_state", {"test_id": report_id}, lambda state: state.get("id") == report_id and state.get("is_active") is True, "QA DB state contains active test"))
        if "/patients/dependents" in path and method in {"POST", "PATCH"} and report_id is not None:
            # A guardian's display_name is intentionally overlaid on the
            # response while the Patient row keeps the canonical name, so
            # persistence here means the row is addressable after either an
            # add or a guardian-scoped edit.
            checks.append(state_ok("patient_state", {"patient_id": report_id}, lambda state: state.get("id") == report_id, "QA DB state matches dependent mutation"))
        return checks

    def _execute_call(self, call: Call) -> StepReport:
        step_name = call.name or getattr(call.tool, "__name__", "call")
        try:
            resolved_args = self._resolve_args(call.args)
            result = call.tool(**resolved_args)
        except Exception as exc:
            resp_dict = None
            if hasattr(exc, "response") and getattr(exc, "response") is not None:
                r = exc.response
                try:
                    resp_dict = {
                        "status_code": getattr(r, "status_code", None),
                        "headers": dict(getattr(r, "headers", {})),
                        "body": r.json() if hasattr(r, "json") else None,
                        "text": getattr(r, "text", ""),
                        "url": str(getattr(r, "url", "")),
                    }
                except Exception:
                    pass
            return StepReport(kind="call", name=step_name, status="fail",
                               error=f"raised:\n{traceback.format_exc()}", id=call.id, response=resp_dict)

        if call.bind_as:
            self._bound[call.bind_as] = result

        children: list[StepReport] = []
        status = "pass"
        validators = self._persistence_checks(call, result, resolved_args) + call.validate
        for validator in validators:
            if isinstance(validator, Check):
                child = validator.evaluate(result, self)
                if self._on_step:
                    self._on_step(child)
            elif isinstance(validator, Call):
                child = self._execute_call(validator)
            else:
                raise TypeError(f"Unsupported validator type: {type(validator)!r}")
            children.append(child)
            if child.status == "fail":
                status = "fail"
                break  # stop-at-first-failure within this validate chain

        resp_dict = None
        if hasattr(result, "status_code"):
            resp_dict = {
                "status_code": getattr(result, "status_code", None),
                "headers": getattr(result, "headers", {}),
                "body": getattr(result, "body", None),
                "text": getattr(result, "text", "") if getattr(result, "body", None) is None else None,
                "url": getattr(result, "url", ""),
                "method": getattr(result, "method", ""),
            }
        elif isinstance(result, dict):
            resp_dict = {"body": result}

        report = StepReport(kind="call", name=step_name, status=status, children=children, id=call.id, response=resp_dict)
        if self._on_step:
            self._on_step(report)
        return report


def flow(name: str | None = None):
    def decorator(fn):
        fn.__flow_name__ = name or fn.__name__
        fn.__is_flow__ = True
        return fn
    return decorator


# --------------------------------------------------------------------------
# Sugar helpers
# --------------------------------------------------------------------------

def expect_status(code: int) -> Check:
    return Check(lambda r, ctx: r.status_code == code, f"status_code == {code}")


def get_path(data: Any, path: str) -> Any:
    """Path lookup through nested dicts and lists, supporting dotted and bracket notation.
    e.g. get_path(body, 'user.id'), get_path(body, 'items[0].id'), get_path(body, '[0].id'),
    get_path(body, 'matrix[0][1]'), get_path(body, 'items.0.id')."""
    if not path:
        return data

    import re

    tokens: list[str | int] = []
    for match in re.finditer(r"\[(-?\d+)\]|\[[\"\x27]([^\"]+)[\"\x27]\]|([^.\[\]]+)", path):
        idx, key, prop = match.groups()
        if idx is not None:
            tokens.append(int(idx))
        elif key is not None:
            tokens.append(key)
        elif prop is not None:
            tokens.append(int(prop) if prop.isdigit() or (prop.startswith("-") and prop[1:].isdigit()) else prop)

    cur = data
    for token in tokens:
        if cur is None:
            return None
        if isinstance(cur, (list, tuple)):
            if isinstance(token, int):
                if -len(cur) <= token < len(cur):
                    cur = cur[token]
                else:
                    return None
            else:
                return None
        elif isinstance(cur, dict):
            if token in cur:
                cur = cur[token]
            elif str(token) in cur:
                cur = cur[str(token)]
            elif isinstance(token, str) and token.isdigit() and int(token) in cur:
                cur = cur[int(token)]
            else:
                return None
        elif hasattr(cur, str(token)):
            cur = getattr(cur, str(token))
        else:
            return None
    return cur


def expect_json(path: str, *, equals: Any = _UNSET, not_none: bool = False,
                op: str = "==") -> Check:
    """Assert a JSON-path value, resolving reference/callable expectations at run time.

    ``equals`` can therefore be a regular value, a ``$ctx``/``$data`` reference
    (when built from a graph), or a lambda in a hand-authored Python flow.
    """
    def fn(r, ctx):
        value = get_path(r.json() or {}, path)
        if not_none and value is None:
            return False
        if equals is not _UNSET:
            expected = ctx._resolve_val(equals)
            if op in ("==", "equals"):
                ok = value == expected
            elif op in ("!=", "not_equals"):
                ok = value != expected
            elif op == "contains":
                ok = expected in value if isinstance(value, (list, tuple, set, dict)) else str(expected) in str(value)
            elif op == "not_contains":
                ok = expected not in value if isinstance(value, (list, tuple, set, dict)) else str(expected) not in str(value)
            elif op in (">", ">=", "<", "<="):
                from core.refs import eval_condition
                ok = eval_condition(value, op, expected)
            else:
                raise ValueError(f"Unknown JSON comparison operator: {op!r}")
            if not ok:
                return False
        return True

    desc = [f"json.{path}"]
    if not_none:
        desc.append("is not None")
    if equals is not _UNSET:
        desc.append(f"{op} {equals!r}")
    return Check(fn, " ".join(desc))


def expect_persistence(
    tool: Callable | str,
    args: dict | None = None,
    *,
    status: int = 200,
    path: str = "",
    equals: Any = _UNSET,
    not_none: bool = False,
    op: str = "==",
) -> Check:
    """First-class QA-state assertion, normally backed by a testing/state API.

    ``tool`` may be a callable or a callable receiving ``ctx``.  The latter is
    useful in generated flows: ``lambda ctx: ctx.api.testing.patient_state``.
    """
    def fn(_result, ctx):
        state_tool = tool(ctx) if callable(tool) and not hasattr(tool, "operation") else tool
        if not callable(state_tool):
            raise ValueError("persistence validator did not resolve to a callable")
        response = state_tool(**ctx._resolve_args(args or {}))
        if getattr(response, "status_code", None) != status:
            return False
        if path or not_none or equals is not _UNSET:
            return expect_json(path, equals=equals, not_none=not_none, op=op).fn(response, ctx)
        return True

    label = f"persistence state status_code == {status}"
    if path:
        label += f"; json.{path} {op} {equals!r}" if equals is not _UNSET else f"; json.{path}"
    return Check(fn, label)


def expect_true(fn: Callable[[Any, "FlowContext"], bool], description: str = "") -> Check:
    return Check(fn, description or "custom check")
