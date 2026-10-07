"""Renders a dashboard graph as a standalone .py flow file -- the same
Call/Check DSL an AI would write by hand, so the plain CLI runner needs no
special-casing for dashboard-built flows, and they stay inspectable/greppable
like any other flow file."""

import json

from core.refs import ref_to_py_source, render_condition_py

INDENT = "    "


def _render_check(node: dict, depth: int) -> str:
    pad = INDENT * depth
    check_type = node["check_type"]
    if check_type == "status":
        return f"{pad}expect_status({node['status']!r}),"
    if check_type in ("json", "json_path"):
        kwargs = []
        if "not_none" in node:
            kwargs.append(f"not_none={node['not_none']!r}")
        if "equals" in node:
            kwargs.append(f"equals={ref_to_py_source(node['equals'])}")
        if "contains" in node:
            kwargs.append(f"equals={ref_to_py_source(node['contains'])}")
            # Older saved graph nodes included both `contains` and an
            # explicit `op: contains`. Emit the keyword only once so every
            # generated flow remains importable by the CLI.
            if "op" not in node:
                kwargs.append("op='contains'")
        if "op" in node:
            kwargs.append(f"op={node['op']!r}")
        args = ", ".join([repr(node["path"])] + kwargs)
        return f"{pad}expect_json({args}),"
    if check_type == "persistence":
        tag, _, op_id = node["tool"].partition("__")
        kwargs = [
            f"status={node.get('status', 200)!r}",
            f"path={node.get('path', '')!r}",
            f"not_none={node.get('not_none', False)!r}",
            f"op={node.get('op', '==')!r}",
        ]
        if "equals" in node:
            kwargs.append(f"equals={ref_to_py_source(node['equals'])}")
        return f"{pad}expect_persistence(lambda ctx: ctx.api.{tag}.{op_id}, {ref_to_py_source(node.get('args', {}))}, {', '.join(kwargs)}),"
    raise ValueError(f"Unknown check_type: {check_type!r}")


def _render_validator(node: dict, depth: int) -> str:
    if node["kind"] == "check":
        return _render_check(node, depth)
    if node["kind"] == "call":
        return _render_call(node, depth)
    raise ValueError(f"Unknown node kind: {node['kind']!r}")


def _render_call(node: dict, depth: int) -> str:
    pad = INDENT * depth
    inner = INDENT * (depth + 1)
    tag, _, op_id = node["tool"].partition("__")

    parts = [f"{pad}Call("]
    parts.append(f"{inner}tool=ctx.api.{tag}.{op_id},")

    args = node.get("args") or {}
    if args:
        parts.append(f"{inner}args={{")
        for k, v in args.items():
            parts.append(f"{inner}{INDENT}{k!r}: {ref_to_py_source(v)},")
        parts.append(f"{inner}}},")

    if node.get("bind_as"):
        parts.append(f"{inner}bind_as={node['bind_as']!r},")

    validate = node.get("validate") or []
    if validate:
        parts.append(f"{inner}validate=[")
        for v in validate:
            parts.append(_render_validator(v, depth + 2))
        parts.append(f"{inner}],")

    parts.append(f"{pad}),")
    return "\n".join(parts)


def _render_step(step: dict, depth: int) -> list[str]:
    pad = INDENT * depth
    lines = []
    kind = step.get("kind", "call")
    if kind == "call":
        retry = step.get("retry")
        if retry:
            lines.append(f"{pad}ctx.run_with_retry(lambda: ctx.run(")
            lines.append(_render_call(step, depth + 2))
            lines.append(f"{pad}{INDENT}), attempts={retry.get('attempts', 3)!r}, initial_delay_s={retry.get('initial_delay_s', retry.get('delay_s', .25))!r}, backoff={retry.get('backoff', 2)!r}, name={step.get('bind_as') or step.get('tool')!r}, id={step.get('id')!r})")
        else:
            lines.append(f"{pad}ctx.run(")
            lines.append(_render_call(step, depth + 1))
            lines.append(f"{pad})")
    elif kind == "condition":
        cond_py = render_condition_py(step.get("left", ""), step.get("op", "=="), step.get("right", ""))
        lines.append(f"{pad}if {cond_py}:")
        then_steps = step.get("then_steps", [])
        if then_steps:
            for s in then_steps:
                lines.extend(_render_step(s, depth + 1))
        else:
            lines.append(f"{pad}{INDENT}pass")

        else_steps = step.get("else_steps", [])
        if step.get("assert") is True:
            lines.append(f"{pad}else:")
            lines.append(f"{pad}{INDENT}raise AssertionError({('condition assertion failed: ' + str(step.get('left', '')))!r})")
        elif else_steps:
            lines.append(f"{pad}else:")
            for s in else_steps:
                lines.extend(_render_step(s, depth + 1))
    elif kind == "flow":
        subflow_name = step.get("flow_name")
        bind_as = step.get("bind_as")
        lines.append(f"{pad}from flows.dashboard.{subflow_name} import {subflow_name}")
        if bind_as:
            lines.append(f"{pad}ctx.run_flow({subflow_name}, bind_as={bind_as!r})")
        else:
            lines.append(f"{pad}ctx.run_flow({subflow_name})")
    elif kind == "split":
        mode = step.get("mode", "parallel")
        name = step.get("name") or "Split"
        step_id_clean = step.get("id", "split").replace("-", "_")
        lines.append(f"{pad}# Split: {name} ({mode})")
        branch_tuples = []
        for i, b in enumerate(step.get("branches", [])):
            fn_name = f"_{step_id_clean}_branch_{i+1}"
            b_name = b.get("name") or f"Branch {i+1}"
            lines.append(f"{pad}def {fn_name}(c):")
            b_steps = b.get("steps", [])
            if b_steps:
                for s in b_steps:
                    lines.extend(_render_step(s, depth + 1))
            else:
                lines.append(f"{pad}{INDENT}pass")
            branch_tuples.append(f"({b_name!r}, {fn_name})")
        joined_branches = ", ".join(branch_tuples)
        lines.append(f"{pad}ctx.run_split([{joined_branches}], mode={mode!r}, id={step.get('id')!r}, name={name!r})")
    elif kind == "wait":
        lines.append(f"{pad}ctx.wait({step.get('seconds', step.get('duration_s', 1))!r}, name={step.get('name', 'wait')!r}, id={step.get('id')!r})")
    elif kind in ("retry", "wait_until"):
        # These blocks require graph-runtime state (nested polling / replay).
        # Keep generated artifacts executable and make their limitation explicit.
        lines.append(f"{pad}# {kind} block: run this graph through the dashboard/MCP runtime to preserve its policy")
        for sub_step in step.get("steps", []):
            lines.extend(_render_step(sub_step, depth))
    return lines


def generate_flow_source(graph: dict) -> str:
    name = graph["name"]
    body_lines = []
    if graph.get("data"):
        body_lines.append(f"    ctx.data.update({json.dumps(graph['data'])})")
        body_lines.append("")
    if graph.get("finally_steps") or graph.get("teardown_steps"):
        body_lines.append("    try:")
        for step in graph["steps"]:
            body_lines.extend(_render_step(step, depth=2))
            body_lines.append("")
        body_lines.append("    finally:")
        for step in graph.get("finally_steps", graph.get("teardown_steps", [])):
            body_lines.extend(_render_step(step, depth=2))
            body_lines.append("")
    else:
        for step in graph["steps"]:
            body_lines.extend(_render_step(step, depth=1))
            body_lines.append("")
    body = "\n".join(body_lines).rstrip("\n")

    imports = "from flows.sdk import Call, expect_json, expect_persistence, expect_status, flow"
    if "get_path(" in body:
        imports += ", get_path"

    return (
        "# Generated by the tibb-tester dashboard.\n"
        "# Editing this file directly is fine, but re-saving from the dashboard\n"
        "# overwrites it -- edit the .flow.json instead to keep changes editable there.\n"
        f"{imports}\n"
        "\n"
        "\n"
        f"@flow(name={name!r})\n"
        f"def {name}(ctx):\n"
        f"{body}\n"
    )
