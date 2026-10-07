"""Turns OpenAPI spec files into plain Python callables, grouped by tag.

This is the one place that understands OpenAPI. Everything downstream (MCP
tools, flows) just calls the resulting functions -- they don't know or care
that OpenAPI was involved.

Auth handling:
  - `http`/`bearer` and `oauth2` security schemes require the caller to pass
    a `token=` kwarg explicitly (per-user session tokens can't be static
    config).
  - `apiKey` security schemes (e.g. the tester API key) are injected
    automatically from Settings -- flow authors never see or pass the key.

Body/parameter schemas may use `$ref` (and single-branch `allOf`, the common
FastAPI pattern) -- these are resolved against the spec's own
components/schemas at parse time, so everything downstream just sees plain
inline schemas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

import json
import os
import yaml

from core.client.base_client import BaseClient, StepResult

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


@dataclass
class Operation:
    operation_id: str
    tag: str
    method: str
    path: str
    parameters: list[dict] = field(default_factory=list)
    body_schema: dict | None = None
    body_content_type: str = "application/json"
    security: list[dict] = field(default_factory=list)
    security_schemes: dict = field(default_factory=dict)
    summary: str = ""
    description: str = ""
    responses: dict = field(default_factory=dict)

    @property
    def has_body(self) -> bool:
        return self.body_schema is not None

    @property
    def auth(self) -> tuple[str | None, str | None]:
        """Returns (kind, header_name) where kind is 'bearer', 'apiKey', or None."""
        for requirement in self.security:
            for scheme_name in requirement:
                scheme = self.security_schemes.get(scheme_name, {})
                scheme_type = scheme.get("type")
                if scheme_type == "oauth2" or (scheme_type == "http" and scheme.get("scheme") == "bearer"):
                    return "bearer", "Authorization"
                if scheme_type == "apiKey" and scheme.get("in") == "header":
                    return "apiKey", scheme["name"]
        return None, None


def _resolve_schema(spec: dict, schema: dict | None, _seen: frozenset[str] = frozenset()) -> dict:
    if not schema or not isinstance(schema, dict):
        return schema or {}
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in _seen:
            return {"$ref": ref}
        node = spec
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        resolved = _resolve_schema(spec, node, _seen | {ref})
        return {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}
    if "allOf" in schema and isinstance(schema["allOf"], list):
        merged = {}
        for sub in schema["allOf"]:
            r = _resolve_schema(spec, sub, _seen)
            if "properties" in r and isinstance(r["properties"], dict):
                merged.setdefault("properties", {}).update(r["properties"])
            if "required" in r and isinstance(r["required"], list):
                merged.setdefault("required", []).extend(r["required"])
            for k, v in r.items():
                if k not in ("properties", "required"):
                    merged[k] = v
        for k, v in schema.items():
            if k != "allOf":
                merged[k] = v
        return merged

    res = dict(schema)
    if "anyOf" in res and isinstance(res["anyOf"], list):
        res["anyOf"] = [_resolve_schema(spec, sub, _seen) for sub in res["anyOf"]]
    if "oneOf" in res and isinstance(res["oneOf"], list):
        res["oneOf"] = [_resolve_schema(spec, sub, _seen) for sub in res["oneOf"]]
    if "items" in res and isinstance(res["items"], dict):
        res["items"] = _resolve_schema(spec, res["items"], _seen)
    if "properties" in res and isinstance(res["properties"], dict):
        res["properties"] = {k: _resolve_schema(spec, v, _seen) for k, v in res["properties"].items()}
    return res


def parse_operations(spec: dict, tags: set[str] | None = None) -> list[Operation]:
    security_schemes = spec.get("components", {}).get("securitySchemes", {})
    operations = []
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method.lower() not in HTTP_METHODS:
                continue
            op_tag = (op.get("tags") or ["default"])[0]
            if tags is not None and op_tag not in tags:
                continue

            body_schema = None
            body_content_type = "application/json"
            request_body = op.get("requestBody")
            if request_body:
                content = request_body.get("content", {})
                if "multipart/form-data" in content:
                    body_content_type = "multipart/form-data"
                    raw_schema = content["multipart/form-data"].get("schema")
                elif "application/json" in content:
                    body_content_type = "application/json"
                    raw_schema = content["application/json"].get("schema")
                elif "application/x-www-form-urlencoded" in content:
                    body_content_type = "application/x-www-form-urlencoded"
                    raw_schema = content["application/x-www-form-urlencoded"].get("schema")
                else:
                    first_ct = next(iter(content.keys()), None)
                    if first_ct:
                        body_content_type = first_ct
                        raw_schema = content[first_ct].get("schema")
                    else:
                        raw_schema = None

                if raw_schema:
                    body_schema = _resolve_schema(spec, raw_schema)

            parameters = [
                {**p, "schema": _resolve_schema(spec, p.get("schema", {"type": "string"}))}
                for p in op.get("parameters", [])
            ]

            responses = {}
            for code, resp_obj in (op.get("responses") or {}).items():
                resolved_resp = dict(resp_obj) if isinstance(resp_obj, dict) else {}
                resp_content = resolved_resp.get("content", {})
                for ct, ct_obj in resp_content.items():
                    if isinstance(ct_obj, dict) and "schema" in ct_obj:
                        ct_obj["schema"] = _resolve_schema(spec, ct_obj["schema"])
                responses[str(code)] = resolved_resp

            operations.append(Operation(
                operation_id=op.get("operationId") or f"{method}_{path}",
                tag=op_tag,
                method=method.upper(),
                path=path,
                parameters=parameters,
                body_schema=body_schema,
                body_content_type=body_content_type,
                security=op.get("security", []),
                security_schemes=security_schemes,
                summary=op.get("summary", ""),
                description=op.get("description", ""),
                responses=responses,
            ))
    return operations


def parse_operations_from_files(spec_paths: list[str], tags: set[str] | None = None) -> list[Operation]:
    operations = []
    for path in spec_paths:
        with open(path) as f:
            spec = yaml.safe_load(f)
        operations.extend(parse_operations(spec, tags=tags))
    return operations


def tool_name_for(op: Operation) -> str:
    return f"{op.tag}__{op.operation_id}"


def build_callable(op: Operation, client: BaseClient) -> Callable[..., StepResult]:
    def fn(**kwargs) -> StepResult:
        import os

        remaining = dict(kwargs)
        token = remaining.pop("token", None)
        explicit_files = remaining.pop("files", None)
        explicit_data = remaining.pop("data", None)

        path_params: dict[str, Any] = {}
        query_params: dict[str, Any] = {}
        for param in op.parameters:
            name = param["name"]
            if name not in remaining:
                continue
            if param["in"] == "path":
                path_params[name] = remaining.pop(name)
            elif param["in"] == "query":
                query_params[name] = remaining.pop(name)

        path = op.path.format(**path_params)
        body = remaining if op.has_body else None

        req_json = None
        req_data = explicit_data
        req_files = explicit_files

        if op.body_content_type == "multipart/form-data" or explicit_files is not None:
            if req_files is None:
                req_files = {}
            if req_data is None:
                req_data = {}

            if isinstance(body, dict):
                for k, v in body.items():
                    prop_schema = (op.body_schema.get("properties", {}).get(k, {})) if isinstance(op.body_schema, dict) else {}
                    is_file = (
                        prop_schema.get("format") == "binary" or
                        prop_schema.get("type") == "binary" or
                        k in ("file", "files", "document", "upload", "attachment", "image") or
                        (isinstance(v, str) and (v.startswith("@") or os.path.isfile(v))) or
                        (isinstance(v, tuple) and len(v) in (2, 3))
                    )
                    if is_file:
                        req_files[k] = v
                    else:
                        if isinstance(v, (dict, list)):
                            req_data[k] = json.dumps(v)
                        elif isinstance(v, bool):
                            req_data[k] = "true" if v else "false"
                        elif v is not None:
                            req_data[k] = str(v)
        elif op.body_content_type == "application/x-www-form-urlencoded":
            req_data = body if req_data is None else req_data
        else:
            req_json = body

        headers: dict[str, str] = {}
        auth_kind, header_name = op.auth
        if auth_kind == "bearer":
            if not token:
                raise ValueError(f"{op.operation_id} requires a bearer token (pass token=...)")
            headers[header_name] = f"Bearer {token}"
        elif auth_kind == "apiKey":
            if not client.testing_api_key:
                raise ValueError(
                    f"{op.operation_id} requires a testing API key, but none is configured "
                    "(set TIBB_TESTING_API_KEY)"
                )
            headers[header_name] = client.testing_api_key

        return client.request(
            op.method,
            path,
            params=query_params or None,
            json=req_json,
            data=req_data,
            files=req_files,
            headers=headers or None,
        )

    fn.__name__ = op.operation_id
    fn.operation = op
    return fn


def load_client(spec_paths: list[str], client: BaseClient, tags: set[str] | None = None) -> SimpleNamespace:
    """Builds a nested namespace of callables, grouped by tag: api.auth.login(...)"""
    operations = parse_operations_from_files(spec_paths, tags=tags)
    by_tag: dict[str, dict[str, Callable]] = {}
    for op in operations:
        by_tag.setdefault(op.tag, {})[op.operation_id] = build_callable(op, client)
    ns = SimpleNamespace(**{tag: SimpleNamespace(**fns) for tag, fns in by_tag.items()})
    ns._client = client
    return ns
