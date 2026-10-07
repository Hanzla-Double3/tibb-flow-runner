"""A JSON-friendly view of the OpenAPI operations for the dashboard's tool
picker, the assistant's tool set, and MCP inspection."""

from config.settings import SPEC_TAGS
from core import workspace_store
from core.client.dynamic import Operation, parse_operations_from_files, tool_name_for
from core.client.factory import resolve_spec_files

JSON_TYPE_MAP = {
    "string": "string", "integer": "integer", "number": "number",
    "boolean": "boolean", "array": "array", "object": "object",
}


def unwrap_field(schema: dict | None) -> dict:
    """Unwraps anyOf/oneOf, extracts types, enums, defaults, items, and nested properties."""
    if not schema or not isinstance(schema, dict):
        return {"type": "string", "nullable": False}

    nullable = False
    active = schema
    for union_key in ("anyOf", "oneOf"):
        if union_key in active and isinstance(active[union_key], list):
            branches = active[union_key]
            non_null = [b for b in branches if isinstance(b, dict) and b.get("type") != "null"]
            if len(non_null) < len(branches):
                nullable = True
            if non_null:
                active = {**non_null[0], **{k: v for k, v in schema.items() if k != union_key}}
            break

    raw_type = active.get("type")
    if not raw_type and "enum" in active:
        raw_type = "string"
    elif not raw_type and "properties" in active:
        raw_type = "object"

    resolved_type = JSON_TYPE_MAP.get(raw_type, raw_type or "string")

    info = {
        "type": resolved_type,
        "nullable": nullable,
    }
    if "enum" in active:
        info["enum"] = active["enum"]
    if "description" in active:
        info["description"] = active["description"]
    if "default" in active:
        info["default"] = active["default"]
    if "format" in active:
        info["format"] = active["format"]

    if resolved_type == "array" and "items" in active and isinstance(active["items"], dict):
        info["items"] = unwrap_field(active["items"])

    if "properties" in active and isinstance(active["properties"], dict):
        req = set(active.get("required", []))
        info["properties"] = {
            k: {**unwrap_field(v), "required": k in req}
            for k, v in active["properties"].items()
            if isinstance(v, dict)
        }

    info["raw_schema"] = schema
    return info


def _fields_for(op: Operation) -> list[dict]:
    fields = []
    for p in op.parameters:
        schema = p.get("schema") or {"type": "string"}
        details = unwrap_field(schema)
        field_info = {
            "name": p["name"],
            "in": p.get("in", "query"),
            "type": details["type"],
            "required": bool(p.get("required")),
        }
        if details.get("enum"):
            field_info["enum"] = details["enum"]
        desc = p.get("description") or details.get("description")
        if desc:
            field_info["description"] = desc
        if "default" in details:
            field_info["default"] = details["default"]
        if "items" in details:
            field_info["items"] = details["items"]
        if "properties" in details:
            field_info["properties"] = details["properties"]
        if details.get("nullable"):
            field_info["nullable"] = True
        field_info["schema"] = details.get("raw_schema", schema)
        fields.append(field_info)

    if op.has_body and isinstance(op.body_schema, dict):
        required = set(op.body_schema.get("required", []))
        for name, schema in (op.body_schema.get("properties") or {}).items():
            schema = schema or {}
            details = unwrap_field(schema)
            is_file = (
                details.get("format") == "binary" or
                schema.get("format") == "binary" or
                schema.get("type") == "binary" or
                (details.get("type") == "array" and details.get("items", {}).get("format") == "binary") or
                (getattr(op, "body_content_type", "") == "multipart/form-data" and name in ("file", "files", "attachment", "document", "upload", "image"))
            )
            field_type = "file" if is_file else details["type"]
            field_info = {
                "name": name,
                "in": "body",
                "type": field_type,
                "required": name in required,
            }
            if details.get("enum"):
                field_info["enum"] = details["enum"]
            if details.get("description"):
                field_info["description"] = details["description"]
            if "default" in details:
                field_info["default"] = details["default"]
            if "items" in details:
                field_info["items"] = details["items"]
            if "properties" in details:
                field_info["properties"] = details["properties"]
            if details.get("nullable"):
                field_info["nullable"] = True
            field_info["schema"] = details.get("raw_schema", schema)
            fields.append(field_info)

    auth_kind, _ = op.auth
    if auth_kind == "bearer":
        fields.append({"name": "token", "in": "auth", "type": "string", "required": True})
    return fields


def list_tools(workspace_id: str | None = None) -> list[dict]:
    ws_id = workspace_id or workspace_store.get_active_workspace_id()
    operations = parse_operations_from_files(resolve_spec_files(ws_id), tags=SPEC_TAGS)

    custom_ops = set()
    try:
        from core import manual_tools
        custom_ops = {m["operation_id"] for m in manual_tools.list_manual_operations(ws_id)}
    except Exception:
        pass

    return [
        {
            "name": tool_name_for(op),
            "operation_id": op.operation_id,
            "tag": op.tag,
            "method": op.method.upper(),
            "path": op.path,
            "summary": op.summary,
            "description": op.description,
            "body_content_type": getattr(op, "body_content_type", "application/json"),
            "body_schema": op.body_schema,
            "responses": op.responses,
            "requires_testing_key": op.auth[0] == "apiKey",
            "is_manual": op.operation_id in custom_ops or op.tag == "manual",
            "fields": _fields_for(op),
        }
        for op in operations
    ]
