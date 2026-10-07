"""Sequential execution of every saved flow in one workspace."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Callable

from core import flow_store, workspace_store
from core.client.factory import build_api_client
from core.flow_validation import validate_graph
from core.graph_builder import make_flow_fn
from flows.sdk import FlowReport
from runner.executor import run_flow

WorkspaceEvent = Callable[[dict[str, Any]], None]


def run_workspace_flows(
    workspace_id: str,
    *,
    on_event: WorkspaceEvent | None = None,
    base_url: str | None = None,
) -> list[FlowReport]:
    """Run saved workspace flows in their deterministic name order.

    A failing or invalid flow does not stop the suite: later flows still run,
    which gives a complete pass/fail picture for the workspace.
    """
    workspace = workspace_store.load_workspace(workspace_id)
    flow_names = list(workspace.get("flow_names", []))
    api = build_api_client(workspace_id=workspace_id, base_url=base_url)
    reports: list[FlowReport] = []

    def emit(event: dict[str, Any]) -> None:
        if on_event:
            on_event(event)

    total = len(flow_names)
    for index, flow_name in enumerate(flow_names, start=1):
        emit({"type": "flow_start", "flow": flow_name, "index": index, "total": total})
        try:
            graph = flow_store.load_flow(flow_name, workspace_id)
        except FileNotFoundError:
            report = FlowReport(name=flow_name, status="fail", error="flow definition was not found")
            reports.append(report)
            emit({"type": "flow_done", "flow": flow_name, "index": index, "total": total,
                  "report": asdict(report)})
            continue

        issues = validate_graph(graph, workspace_id)
        if issues:
            report = FlowReport(
                name=flow_name,
                status="fail",
                error="OpenAPI preflight failed: " + "; ".join(issue["message"] for issue in issues),
            )
            reports.append(report)
            emit({"type": "flow_invalid", "flow": flow_name, "index": index, "total": total,
                  "issues": issues, "report": asdict(report)})
            continue

        flow_fn = make_flow_fn(graph, workspace=workspace)

        def on_step(step: Any, *, _flow=flow_name, _index=index) -> None:
            emit({"type": "step", "flow": _flow, "index": _index, "total": total,
                  "step": asdict(step)})

        report = run_flow(flow_fn, api, on_step=on_step)
        reports.append(report)
        emit({"type": "flow_done", "flow": flow_name, "index": index, "total": total,
              "report": asdict(report)})

    return reports
