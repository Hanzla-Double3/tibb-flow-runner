"""The small reference syntax used for step arguments in the dashboard, so a
user can wire "order id from the previous step" without writing Python.

Forms (anything not matching one of these is treated as a literal value):
  $data.<key>                 -> ctx.data[<key>]
  $ctx.<bind_as>.status_code  -> ctx.<bind_as>.status_code
  $ctx.<bind_as>.body         -> ctx.<bind_as>.json()
  $ctx.<bind_as>.body.<path>  -> get_path(ctx.<bind_as>.json(), "<path>")

Used two ways: `resolve_value` turns a ref into a live callable(ctx) for
in-process execution; `ref_to_py_source` renders the identical semantics as
Python source text for the generated .py flow file, so a dashboard-built
flow behaves the same whether run live or later replayed by the plain CLI
runner.
"""

import re
from typing import Any

_DATA_RE = re.compile(r"^\$data\.(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<path>[.\[].*)?$")
_WS_DATA_RE = re.compile(r"^\$(?:workspace|ws)\.(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<path>[.\[].*)?$")
_SECRET_RE = re.compile(r"^\$(?:secret|secrets)\.(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<path>[.\[].*)?$")
_CTX_STATUS_RE = re.compile(r"^\$ctx\.(?P<bind>[A-Za-z_][A-Za-z0-9_]*)\.status_code$")
_CTX_BODY_RE = re.compile(r"^\$ctx\.(?P<bind>[A-Za-z_][A-Za-z0-9_]*)\.body(?P<path>[.\[].*)?$")
_CTX_DIRECT_RE = re.compile(r"^\$ctx\.(?P<bind>[A-Za-z_][A-Za-z0-9_]*)(?P<path>[.\[].*)?$")


_EMBEDDED_REF_RE = re.compile(
    r"\$(?:\{((?:ctx|data|workspace|ws|secret|secrets)\.[^{}]+)\}|((?:ctx|data|workspace|ws|secret|secrets)\.[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+|\[-?\d+\])*))"
)


def is_ref(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    val = value.strip()
    if val.startswith("${") and val.endswith("}"):
        val = "$" + val[2:-1]
    return (
        val.startswith("$data.") or
        val.startswith("$ctx.") or
        val.startswith("$workspace.") or
        val.startswith("$ws.") or
        val.startswith("$secret.") or
        val.startswith("$secrets.")
    )


def has_refs(value: Any) -> bool:
    if is_ref(value):
        return True
    if isinstance(value, str) and bool(_EMBEDDED_REF_RE.search(value)):
        return True
    if isinstance(value, dict):
        return any(has_refs(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_refs(v) for v in value)
    return False


def _resolve_exact_ref(value: str):
    from flows.sdk import get_path  # local import: core.refs stays a leaf, sdk stays dependency-free

    val = value.strip()
    if val.startswith("${") and val.endswith("}"):
        val = "$" + val[2:-1]

    m = _DATA_RE.match(val)
    if m:
        key = m.group("key")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return lambda ctx: get_path(ctx.data.get(key), path)
        return lambda ctx: ctx.data[key]

    m = _WS_DATA_RE.match(val)
    if m:
        key = m.group("key")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return lambda ctx: get_path((getattr(ctx, "workspace_data", None) or getattr(ctx, "data", {})).get(key), path)
        return lambda ctx: (getattr(ctx, "workspace_data", None) or getattr(ctx, "data", {})).get(key)

    m = _SECRET_RE.match(val)
    if m:
        key = m.group("key")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return lambda ctx: get_path(getattr(ctx, "secrets", {}).get(key), path)
        return lambda ctx: getattr(ctx, "secrets", {}).get(key)

    m = _CTX_STATUS_RE.match(val)
    if m:
        bind = m.group("bind")
        return lambda ctx: getattr(ctx, bind).status_code

    m = _CTX_BODY_RE.match(val)
    if m:
        bind = m.group("bind")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None

        def _get_body(ctx):
            obj = getattr(ctx, bind)
            if hasattr(obj, "json") and callable(obj.json):
                body = obj.json()
            elif hasattr(obj, "body"):
                body = obj.body
            elif isinstance(obj, dict) and "body" in obj:
                body = obj["body"]
            else:
                body = obj
            if path:
                return get_path(body, path)
            return body

        return _get_body

    m = _CTX_DIRECT_RE.match(val)
    if m:
        bind = m.group("bind")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None

        def _get_direct(ctx):
            obj = getattr(ctx, bind)
            if path:
                if hasattr(obj, "json") and callable(obj.json):
                    base = obj.json()
                elif hasattr(obj, "body"):
                    base = obj.body
                else:
                    base = obj
                return get_path(base, path)
            return obj

        return _get_direct

    raise ValueError(f"Unrecognized reference syntax: {value!r}")


def _resolve_interpolated_str(value: str):
    def fn(ctx):
        def repl(m):
            ref_str = "$" + (m.group(1) or m.group(2))
            try:
                resolver = _resolve_exact_ref(ref_str)
                res = resolver(ctx)
                return str(res) if res is not None else ""
            except Exception:
                return m.group(0)
        return _EMBEDDED_REF_RE.sub(repl, value)
    return fn


def resolve_value(value: Any):
    """Returns the literal unchanged, a callable(ctx) if `value` is a $-reference,
    or a recursively resolved structure if `value` is a dict/list/tuple containing references."""
    if isinstance(value, dict):
        return {k: resolve_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(resolve_value(v) for v in value)

    if not isinstance(value, str):
        return value

    val = value.strip()
    if is_ref(val):
        return _resolve_exact_ref(val)

    if _EMBEDDED_REF_RE.search(value):
        return _resolve_interpolated_str(value)

    return value


def eval_resolved(val: Any, ctx: Any) -> Any:
    """Recursively evaluates any callables in val using ctx, or resolves $-refs if present."""
    if callable(val):
        return val(ctx)
    if isinstance(val, dict):
        return {k: eval_resolved(v, ctx) for k, v in val.items()}
    if isinstance(val, list):
        return [eval_resolved(v, ctx) for v in val]
    if isinstance(val, tuple):
        return tuple(eval_resolved(v, ctx) for v in val)
    if isinstance(val, str) and is_ref(val):
        try:
            fn = _resolve_exact_ref(val)
            return fn(ctx)
        except Exception:
            return val
    return val


def _interpolated_to_py_source(value: str) -> str:
    parts = []
    last_idx = 0
    for m in _EMBEDDED_REF_RE.finditer(value):
        prefix = value[last_idx:m.start()]
        if prefix:
            parts.append(repr(prefix))
        ref_str = "$" + (m.group(1) or m.group(2))
        expr = ref_to_py_expr(ref_str)
        parts.append(f"str({expr})")
        last_idx = m.end()
    suffix = value[last_idx:]
    if suffix:
        parts.append(repr(suffix))
    return f"lambda ctx: {' + '.join(parts)}"


def ref_to_py_source(value: Any) -> str:
    """Renders `value` as Python source: a literal repr, or a lambda expression."""
    if isinstance(value, dict):
        items_src = ", ".join(f"{repr(k)}: {ref_to_py_source(v)}" for k, v in value.items())
        return f"{{{items_src}}}"
    if isinstance(value, list):
        items_src = ", ".join(ref_to_py_source(v) for v in value)
        return f"[{items_src}]"
    if isinstance(value, tuple):
        items_src = ", ".join(ref_to_py_source(v) for v in value)
        return f"({items_src},)" if len(value) == 1 else f"({items_src})"

    if not isinstance(value, str):
        return repr(value)

    val = value.strip()
    if is_ref(val):
        return f"lambda ctx: {ref_to_py_expr(val)}"

    if _EMBEDDED_REF_RE.search(value):
        return _interpolated_to_py_source(value)

    return repr(value)


def ref_to_py_expr(value: Any) -> str:
    """Renders `value` as a direct Python expression (without lambda ctx:)."""
    if isinstance(value, dict):
        items_src = ", ".join(f"{repr(k)}: {ref_to_py_expr(v)}" for k, v in value.items())
        return f"{{{items_src}}}"
    if isinstance(value, list):
        items_src = ", ".join(ref_to_py_expr(v) for v in value)
        return f"[{items_src}]"
    if isinstance(value, tuple):
        items_src = ", ".join(ref_to_py_expr(v) for v in value)
        return f"({items_src},)" if len(value) == 1 else f"({items_src})"

    if not isinstance(value, str):
        return repr(value)

    val = value.strip()
    if val.startswith("${") and val.endswith("}"):
        val = "$" + val[2:-1]

    if not is_ref(val):
        if _EMBEDDED_REF_RE.search(value):
            parts = []
            last_idx = 0
            for m in _EMBEDDED_REF_RE.finditer(value):
                prefix = value[last_idx:m.start()]
                if prefix:
                    parts.append(repr(prefix))
                ref_str = "$" + (m.group(1) or m.group(2))
                expr = ref_to_py_expr(ref_str)
                parts.append(f"str({expr})")
                last_idx = m.end()
            suffix = value[last_idx:]
            if suffix:
                parts.append(repr(suffix))
            return " + ".join(parts)
        return repr(value)

    m = _DATA_RE.match(val)
    if m:
        key = m.group("key")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return f"get_path(ctx.data.get({key!r}), {path!r})"
        return f"ctx.data[{key!r}]"

    m = _WS_DATA_RE.match(val)
    if m:
        key = m.group("key")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return f"get_path((getattr(ctx, 'workspace_data', None) or ctx.data).get({key!r}), {path!r})"
        return f"(getattr(ctx, 'workspace_data', None) or ctx.data).get({key!r})"

    m = _SECRET_RE.match(val)
    if m:
        key = m.group("key")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return f"get_path(getattr(ctx, 'secrets', {{}}).get({key!r}), {path!r})"
        return f"getattr(ctx, 'secrets', {{}}).get({key!r})"

    m = _CTX_STATUS_RE.match(val)
    if m:
        return f"ctx.{m.group('bind')}.status_code"

    m = _CTX_BODY_RE.match(val)
    if m:
        bind = m.group("bind")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return f"get_path(ctx.{bind}.json(), {path!r})"
        return f"ctx.{bind}.json()"

    m = _CTX_DIRECT_RE.match(val)
    if m:
        bind = m.group("bind")
        raw_path = m.group("path")
        path = raw_path.lstrip(".") if raw_path else None
        if path:
            return f"get_path(ctx.{bind}.json() if hasattr(ctx.{bind}, 'json') else ctx.{bind}, {path!r})"
        return f"ctx.{bind}"

    raise ValueError(f"Unrecognized reference syntax: {value!r}")


def eval_condition(left: Any, op: str, right: Any) -> bool:
    """Evaluates a binary or unary condition."""
    if op == "is_truthy":
        return bool(left)
    if op == "is_falsy":
        return not bool(left)

    # Attempt loose number coercion if one is numeric
    if isinstance(right, (int, float)) and isinstance(left, str):
        try:
            left = float(left) if "." in left else int(left)
        except ValueError:
            pass
    elif isinstance(left, (int, float)) and isinstance(right, str):
        try:
            right = float(right) if "." in right else int(right)
        except ValueError:
            pass

    if op in ("==", "equals"):
        return left == right
    if op in ("!=", "not_equals"):
        return left != right
    if op == "contains":
        if left is None:
            return False
        if isinstance(left, (list, tuple, set, dict)):
            return right in left
        return str(right) in str(left)
    if op == "not_contains":
        if left is None:
            return True
        if isinstance(left, (list, tuple, set, dict)):
            return right not in left
        return str(right) not in str(left)
    if op == ">":
        return float(left) > float(right)
    if op == ">=":
        return float(left) >= float(right)
    if op == "<":
        return float(left) < float(right)
    if op == "<=":
        return float(left) <= float(right)

    raise ValueError(f"Unknown condition operator: {op!r}")


def render_condition_py(left: Any, op: str, right: Any) -> str:
    """Emits Python boolean expression for codegen."""
    left_expr = ref_to_py_expr(left)
    if op == "is_truthy":
        return f"bool({left_expr})"
    if op == "is_falsy":
        return f"not bool({left_expr})"

    right_expr = ref_to_py_expr(right)
    if op in ("==", "equals"):
        return f"{left_expr} == {right_expr}"
    if op in ("!=", "not_equals"):
        return f"{left_expr} != {right_expr}"
    if op == "contains":
        return f"{right_expr} in {left_expr}"
    if op == "not_contains":
        return f"{right_expr} not in {left_expr}"
    if op in (">", ">=", "<", "<="):
        return f"{left_expr} {op} {right_expr}"

    return f"{left_expr} == {right_expr}"
