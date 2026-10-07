"""Static validation for editable flow graphs.

This deliberately validates only facts known before a run (most importantly
OpenAPI-required arguments).  Runtime references such as ``$ctx.create.body.id``
count as supplied values; their existence is checked by the runner instead.
"""

from __future__ import annotations

from typing import Any

from ui_server.tools_info import list_tools


def _walk(steps: list[dict], location: str = "steps"):
    for index, step in enumerate(steps or []):
        here = f"{location}[{index}]"
        yield step, here
        kind = step.get("kind", "call")
        if kind == "condition":
            yield from _walk(step.get("then_steps", []), f"{here}.then_steps")
            yield from _walk(step.get("else_steps", []), f"{here}.else_steps")
        elif kind in {"retry", "wait_until"}:
            yield from _walk(step.get("steps", []), f"{here}.steps")
        elif kind == "split":
            for branch_index, branch in enumerate(step.get("branches", [])):
                yield from _walk(branch.get("steps", []), f"{here}.branches[{branch_index}].steps")


def validate_graph(graph: dict, workspace_id: str | None = None) -> list[dict[str, Any]]:
    """Return actionable, side-effect-free graph validation issues.

    The format is intentionally simple for MCP and the dashboard:
    ``{node_id, code, message, fields}``.
    """
    tools = {tool["name"]: tool for tool in list_tools(workspace_id)}
    issues: list[dict[str, Any]] = []
    for node, location in _walk(graph.get("steps", [])):
        if node.get("kind", "call") != "call":
            continue
        tool_name = node.get("tool", "")
        tool = tools.get(tool_name)
        if tool is None:
            issues.append({
                "node_id": node.get("id"), "code": "unknown_tool",
                "message": f"Unknown API tool: {tool_name!r}", "location": location,
            })
            continue
        args = node.get("args") or {}
        missing = [
            field["name"] for field in tool.get("fields", [])
            if field.get("required") and (field["name"] not in args or args[field["name"]] is None or args[field["name"]] == "")
        ]
        if missing:
            issues.append({
                "node_id": node.get("id"), "code": "missing_required_arguments",
                "message": f"{tool_name} is missing required arguments: {', '.join(missing)}",
                "fields": missing, "location": location,
            })
    return issues
