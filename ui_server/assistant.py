"""In-app AI assistant: an OpenAI-compatible chat-completions tool-calling
loop that builds and runs flows by editing the graph directly -- it has no
tool for calling arbitrary real API endpoints, only for building/editing the
flow graph and running it through the same validated path a human would use
by clicking Run. Credentials come from env vars only; never sent to the
browser.
"""

import json
import os
import random
import string
from dataclasses import asdict
from typing import Any, Callable

import requests

from core import flow_store as store
from core.graph_builder import make_flow_fn
from runner.executor import run_flow
from ui_server.tools_info import list_tools as list_api_tools

OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

MAX_TOOL_ROUNDS = 25

SYSTEM_PROMPT = """You are an assistant embedded in a QA flow-builder dashboard.
You build and run API test flows entirely through the tools you're given --
you have no way to call the target API directly, only by adding steps to the
flow and running it. A flow is a sequence of steps (calls, conditions, or reusable subflows);
each call step may carry a list of validators: status/json checks, or a nested call used as
validation. You can also embed existing flows as modular blocks using add_flow_step --
when run, the subflow's context ($ctx bindings and seed data) is merged directly into the main flow.
Use list_available_tools to see what API endpoints exist, and list_flows to see existing flows.
Use $data.<key> to reference seed values, and $ctx.<bind_as>.body.<path> or $ctx.<bind_as>.status_code
to reference earlier results. You can run the full flow using run_flow, or run a single node using run_node.
When running flows or nodes, you receive the execution report along with the exact HTTP response."""


class AssistantError(Exception):
    pass


def _new_id() -> str:
    return "n" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


def _find(graph: dict, node_id: str):
    """Returns (node, parent_list) for a node id anywhere in the graph tree."""

    def walk(items):
        for item in items:
            if item["id"] == node_id:
                return item, items
            if item.get("kind") == "call":
                found = walk(item.get("validate", []))
                if found[0] is not None:
                    return found
            elif item.get("kind") == "condition":
                found = walk(item.get("then_steps", []))
                if found[0] is not None:
                    return found
                found = walk(item.get("else_steps", []))
                if found[0] is not None:
                    return found
            elif item.get("kind") == "split":
                for b in item.get("branches", []):
                    found = walk(b.get("steps", []))
                    if found[0] is not None:
                        return found
        return None, None

    return walk(graph.get("steps", []))


def _tool_defs() -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "list_available_tools",
            "description": "List the API tools available to use as flow steps.",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "list_flows",
            "description": "List existing saved flows that can be embedded as subflow blocks.",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "new_flow",
            "description": "Start a brand new blank flow with the specified name.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "description": "Name for the new flow"}
            }, "required": ["name"]},
        }},
        {"type": "function", "function": {
            "name": "clean_up_flow",
            "description": "Clean up the current flow: collapses body and params, hides validators, and aligns all nodes in a neat vertical line.",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "add_flow_step",
            "description": "Add an existing flow as a modular subflow step. Its context and variables merge into the main flow.",
            "parameters": {"type": "object", "properties": {
                "flow_name": {"type": "string", "description": "Name of the existing flow to run"},
                "bind_as": {"type": "string", "description": "Optional name to bind the subflow's outputs under"},
            }, "required": ["flow_name"]},
        }},
        {"type": "function", "function": {
            "name": "add_split_step",
            "description": "Add a split block with multiple branches that execute either concurrently in parallel or sequentially.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "description": "Label for the split step"},
                "mode": {"type": "string", "enum": ["parallel", "sequential"], "description": "Execution mode"},
                "branch_names": {"type": "array", "items": {"type": "string"}, "description": "Branch labels"},
            }},
        }},
        {"type": "function", "function": {
            "name": "add_step_to_branch",
            "description": "Add a call step to a specific branch of a split block.",
            "parameters": {"type": "object", "properties": {
                "split_id": {"type": "string", "description": "id of the split block"},
                "branch": {"type": "string", "description": "branch name or index"},
                "tool": {"type": "string", "description": "API tool name"},
                "bind_as": {"type": "string", "description": "Optional result binding name"},
                "args": {"type": "object", "description": "Tool call arguments"},
            }, "required": ["split_id", "branch", "tool"]},
        }},
        {"type": "function", "function": {
            "name": "set_split_mode",
            "description": "Change execution mode of a split block ('parallel' or 'sequential').",
            "parameters": {"type": "object", "properties": {
                "split_id": {"type": "string", "description": "id of the split block"},
                "mode": {"type": "string", "enum": ["parallel", "sequential"], "description": "Execution mode"},
            }, "required": ["split_id", "mode"]},
        }},
        {"type": "function", "function": {
            "name": "add_step",
            "description": "Add a new top-level call step to the end of the flow.",
            "parameters": {"type": "object", "properties": {
                "tool": {"type": "string", "description": "Tool name, e.g. 'auth__login'"},
                "bind_as": {"type": "string", "description": "Optional name to bind the result to, for later reference"},
                "args": {"type": "object", "description": "Argument values; use $data.key or $ctx.bind.body.path to reference other values"},
            }, "required": ["tool"]},
        }},
        {"type": "function", "function": {
            "name": "add_check",
            "description": "Add a status or json validation check under a call step's validate list.",
            "parameters": {"type": "object", "properties": {
                "parent_id": {"type": "string", "description": "id of the call step this check validates"},
                "check_type": {"type": "string", "enum": ["status", "json"]},
                "status": {"type": "integer", "description": "expected status code, for check_type=status"},
                "path": {"type": "string", "description": "dotted json path into the response body, for check_type=json"},
                "not_none": {"type": "boolean"},
                "equals": {"description": "expected value at `path`, for check_type=json"},
            }, "required": ["parent_id", "check_type"]},
        }},
        {"type": "function", "function": {
            "name": "add_nested_call",
            "description": "Add another call (e.g. a DB check) as validation under a call step's validate list.",
            "parameters": {"type": "object", "properties": {
                "parent_id": {"type": "string"},
                "tool": {"type": "string"},
                "bind_as": {"type": "string"},
                "args": {"type": "object"},
            }, "required": ["parent_id", "tool"]},
        }},
        {"type": "function", "function": {
            "name": "set_data",
            "description": "Set a ctx.data seed value, referenced elsewhere as $data.<key>.",
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string"}, "value": {},
            }, "required": ["key", "value"]},
        }},
        {"type": "function", "function": {
            "name": "remove_node",
            "description": "Remove a step, check, or nested call by id (and everything nested under it).",
            "parameters": {"type": "object", "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]},
        }},
        {"type": "function", "function": {
            "name": "get_graph",
            "description": "Get the current flow graph as JSON.",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "run_flow",
            "description": "Save and run the current flow against the real target, returning pass/fail per step and exact HTTP responses.",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "run_node",
            "description": "Run a single node/step in the flow by id against the real target, returning the execution report and exact HTTP response (status_code, headers, body).",
            "parameters": {"type": "object", "properties": {
                "node_id": {"type": "string", "description": "id of the call or condition step to run"}
            }, "required": ["node_id"]},
        }},
    ]


def _report_to_dict(report) -> dict:
    return asdict(report)


def _execute_tool(name: str, args: dict, graph: dict) -> Any:
    if name == "list_available_tools":
        return list_api_tools()

    if name == "list_flows":
        return store.list_flows()

    if name == "new_flow":
        flow_name = args["name"].strip()
        graph.clear()
        graph.update({"name": flow_name, "data": {}, "steps": []})
        store.save_flow(flow_name, graph)
        return {"ok": True, "name": flow_name}

    if name == "clean_up_flow":
        def tidy(node):
            if node.get("kind") == "call":
                node["hide_validators"] = True
                node["collapse_params"] = True
                node["collapse_json"] = True
                for v in node.get("validate", []):
                    tidy(v)
            elif node.get("kind") == "condition":
                for s in node.get("then_steps", []):
                    tidy(s)
                for s in node.get("else_steps", []):
                    tidy(s)

        y = 80
        for step in graph.get("steps", []):
            tidy(step)
            step["pos"] = {"x": 80, "y": y}
            if step.get("kind") == "condition":
                ty = y - 40
                tx = 80 + 370
                for s in step.get("then_steps", []):
                    s["pos"] = {"x": tx, "y": ty}
                    tx += 360
                ey = y + 160
                ex = 80 + 370
                for s in step.get("else_steps", []):
                    s["pos"] = {"x": ex, "y": ey}
                    ex += 360
            elif step.get("kind") == "split":
                by = y - 20
                for b in step.get("branches", []):
                    bx = 80 + 370
                    for s in b.get("steps", []):
                        s["pos"] = {"x": bx, "y": by}
                        bx += 360
                    by += 150
                y += max(180, len(step.get("branches", [])) * 150)
            elif step.get("kind") == "flow":
                y += 120
            else:
                y += 135

        return {"ok": True, "message": "Flow cleaned up: hidden validators, collapsed body/params, aligned in line."}

    if name == "add_flow_step":
        node = {
            "id": _new_id(),
            "kind": "flow",
            "flow_name": args["flow_name"],
            "bind_as": args.get("bind_as") or "",
        }
        graph.setdefault("steps", []).append(node)
        return {"id": node["id"]}

    if name == "add_split_step":
        labels = args.get("branch_names") or ["Branch 1", "Branch 2"]
        branches = [{"id": _new_id(), "name": b_name, "steps": []} for b_name in labels]
        node = {
            "id": _new_id(),
            "kind": "split",
            "name": args.get("name") or "Split Paths",
            "mode": (args.get("mode") or "parallel").lower().strip(),
            "branches": branches,
        }
        graph.setdefault("steps", []).append(node)
        return {"id": node["id"], "mode": node["mode"], "branches": [b["name"] for b in branches]}

    if name == "add_step_to_branch":
        split_id = args["split_id"]
        branch_ref = args["branch"]
        split_node, _ = _find(graph, split_id)
        if not split_node or split_node.get("kind") != "split":
            return {"error": f"no such split node: {split_id}"}
        branches = split_node.setdefault("branches", [])
        target_branch = None
        for b in branches:
            if b.get("name") == branch_ref or b.get("id") == branch_ref:
                target_branch = b
                break
        if not target_branch and str(branch_ref).isdigit():
            idx = int(branch_ref)
            if 0 <= idx < len(branches):
                target_branch = branches[idx]
        if not target_branch:
            return {"error": f"branch {branch_ref} not found in split {split_id}"}

        step = {
            "id": _new_id(),
            "kind": "call",
            "tool": args["tool"],
            "bind_as": args.get("bind_as") or "",
            "args": args.get("args") or {},
            "validate": [],
        }
        target_branch.setdefault("steps", []).append(step)
        return {"id": step["id"], "branch": target_branch.get("name")}

    if name == "set_split_mode":
        split_id = args["split_id"]
        mode = (args.get("mode") or "parallel").lower().strip()
        split_node, _ = _find(graph, split_id)
        if not split_node or split_node.get("kind") != "split":
            return {"error": f"no such split node: {split_id}"}
        split_node["mode"] = mode
        return {"ok": True, "mode": mode}

    if name == "add_step":
        node = {"id": _new_id(), "kind": "call", "tool": args["tool"],
                "bind_as": args.get("bind_as") or "", "args": args.get("args") or {}, "validate": []}
        graph.setdefault("steps", []).append(node)
        return {"id": node["id"]}

    if name == "add_check":
        node = {"id": _new_id(), "kind": "check", "check_type": args["check_type"]}
        for field in ("status", "path", "not_none", "equals"):
            if field in args:
                node[field] = args[field]
        parent, _ = _find(graph, args["parent_id"])
        if parent is None:
            return {"error": f"no such node: {args['parent_id']}"}
        parent.setdefault("validate", []).append(node)
        return {"id": node["id"]}

    if name == "add_nested_call":
        node = {"id": _new_id(), "kind": "call", "tool": args["tool"],
                "bind_as": args.get("bind_as") or "", "args": args.get("args") or {}, "validate": []}
        parent, _ = _find(graph, args["parent_id"])
        if parent is None:
            return {"error": f"no such node: {args['parent_id']}"}
        parent.setdefault("validate", []).append(node)
        return {"id": node["id"]}

    if name == "set_data":
        graph.setdefault("data", {})[args["key"]] = args["value"]
        return {"ok": True}

    if name == "remove_node":
        node, parent_list = _find(graph, args["node_id"])
        if node is None:
            return {"error": f"no such node: {args['node_id']}"}
        parent_list.remove(node)
        return {"ok": True}

    if name == "get_graph":
        return graph

    if name == "run_flow":
        from core.client.factory import build_api_client  # local import: avoids a heavy import at module load
        store.save_flow(graph["name"], graph)
        api = build_api_client()
        flow_fn = make_flow_fn(graph)
        report = run_flow(flow_fn, api)
        return _report_to_dict(report)

    if name == "run_node":
        from core.client.factory import build_api_client
        from core.graph_builder import run_single_node
        store.save_flow(graph["name"], graph)
        api = build_api_client()
        return run_single_node(graph, args["node_id"], api)

    return {"error": f"unknown tool: {name}"}


def run_assistant_turn(graph: dict, history: list[dict] | None, user_message: str,
                        on_event: Callable[[dict], None] | None = None) -> tuple[str, list[dict]]:
    """Runs one user turn through the tool-calling loop, mutating `graph` in place.
    Returns (reply_text, updated_history)."""
    if not OPENAI_API_KEY:
        raise AssistantError(
            "OPENAI_API_KEY is not configured on the server -- set OPENAI_API_KEY "
            "(and optionally OPENAI_BASE_URL / OPENAI_MODEL) and restart the dashboard."
        )

    messages = list(history) if history else [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.append({"role": "user", "content": user_message})

    for _ in range(MAX_TOOL_ROUNDS):
        response = requests.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": OPENAI_MODEL, "messages": messages, "tools": _tool_defs(), "tool_choice": "auto"},
            timeout=60,
        )
        if not response.ok:
            raise AssistantError(f"LLM request failed ({response.status_code}): {response.text[:500]}")
        choice = response.json()["choices"][0]
        message = choice["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return message.get("content") or "", messages

        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                fn_args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                fn_args = {}

            if on_event:
                on_event({"type": "tool_call", "name": fn_name, "args": fn_args})
            result = _execute_tool(fn_name, fn_args, graph)
            if on_event:
                on_event({"type": "tool_result", "name": fn_name, "result": result})
                on_event({"type": "graph", "graph": graph})

            messages.append({
                "role": "tool", "tool_call_id": call["id"],
                "content": json.dumps(result, default=str),
            })

    return "(stopped after too many tool-call rounds -- try a narrower request)", messages
