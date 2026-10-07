"""FastAPI backend for the flow-builder dashboard: manages workspaces (each with
its own openapi.json, flows, base_url, secrets, and data key-values),
OpenAPI specs and manual tools, saved flows (graph JSON + generated .py),
runs a flow live over a websocket streaming each step's result, and hosts the
in-app AI assistant that builds/edits/runs flows through a websocket chat."""

import config.env
config.env.load_dotenv()  # must run before ui_server.assistant reads OPENAI_* at import time

import asyncio
from dataclasses import asdict
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from core import flow_store as store
from core import manual_tools, workspace_store
from core.client.factory import build_api_client
from core.graph_builder import make_flow_fn, run_single_node
from core.flow_validation import validate_graph
from runner.executor import run_flow
from runner.workspace import run_workspace_flows
from ui_server import assistant
from ui_server.tools_info import list_tools

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="tibb-tester dashboard")


class SaveFlowRequest(BaseModel):
    graph: dict
    version: int | None = None


class RunNodeRequest(BaseModel):
    graph: dict | None = None
    args_override: dict | None = None
    data_override: dict | None = None
    base_url: str | None = None


class RunFlowRequest(BaseModel):
    graph: dict | None = None
    params: dict | None = None
    base_url: str | None = None


class ImportSpecRequest(BaseModel):
    id: str
    title: str
    content: str


class ManualToolRequest(BaseModel):
    operation_id: str
    tag: str = "manual"
    method: str
    path: str
    query_params: list[str] = []
    body_fields: list[str] = []
    requires_auth: bool = False
    content_type: str = "application/json"


class CreateWorkspaceRequest(BaseModel):
    id: str
    name: str | None = None
    base_url: str = "http://localhost:8000"
    data: dict | None = None
    secrets: dict | None = None
    openapi_spec: str | None = None


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    data: dict | None = None
    secrets: dict | None = None
    openapi_spec: str | None = None
    flow_names: list[str] | None = None


class SetActiveWorkspaceRequest(BaseModel):
    workspace_id: str


class UploadSpecRequest(BaseModel):
    content: str


def _safe_build_client(workspace_id: str | None = None, base_url: str | None = None):
    try:
        return build_api_client(workspace_id=workspace_id, base_url=base_url)
    except TypeError:
        try:
            return build_api_client(workspace_id=workspace_id)
        except TypeError:
            return build_api_client()


# ===========================================================================
# Workspace Endpoints
# ===========================================================================

@app.get("/api/workspaces")
def get_workspaces():
    return workspace_store.list_workspaces()


@app.post("/api/workspaces")
def post_workspace(body: CreateWorkspaceRequest):
    try:
        return workspace_store.create_workspace(
            workspace_id=body.id,
            name=body.name,
            base_url=body.base_url,
            data=body.data,
            secrets=body.secrets,
            openapi_spec=body.openapi_spec,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/workspaces/active")
def get_active_workspace():
    active_id = workspace_store.get_active_workspace_id()
    return workspace_store.load_workspace(active_id)


@app.post("/api/workspaces/active")
def set_active_workspace(body: SetActiveWorkspaceRequest):
    try:
        workspace_store.set_active_workspace_id(body.workspace_id)
        return {"ok": True, "active": body.workspace_id}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/workspaces/{workspace_id}")
def get_workspace(workspace_id: str):
    try:
        return workspace_store.load_workspace(workspace_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Workspace '{workspace_id}' not found")


@app.put("/api/workspaces/{workspace_id}")
def put_workspace(workspace_id: str, body: UpdateWorkspaceRequest):
    try:
        return workspace_store.save_workspace(
            workspace_id=workspace_id,
            name=body.name,
            base_url=body.base_url,
            data=body.data,
            secrets=body.secrets,
            openapi_spec=body.openapi_spec,
            flow_names=body.flow_names,
        )
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str):
    try:
        workspace_store.delete_workspace(workspace_id)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/workspaces/{workspace_id}/spec")
def upload_workspace_spec(workspace_id: str, body: UploadSpecRequest):
    try:
        yaml.safe_load(body.content)
    except Exception as exc:
        raise HTTPException(400, f"Invalid OpenAPI JSON/YAML: {exc}")
    workspace_store.save_workspace(workspace_id, openapi_spec=body.content)
    return {"ok": True}


@app.get("/api/workspaces/{workspace_id}/tools")
def get_workspace_tools(workspace_id: str):
    return list_tools(workspace_id)


@app.post("/api/workspaces/{workspace_id}/tools/manual")
def post_workspace_manual_tool(workspace_id: str, body: ManualToolRequest):
    tool_name = manual_tools.add_manual_operation(
        workspace_id=workspace_id,
        operation_id=body.operation_id,
        tag=body.tag,
        method=body.method,
        path=body.path,
        query_params=body.query_params,
        body_fields=body.body_fields,
        requires_auth=body.requires_auth,
        content_type=body.content_type,
    )
    return {"ok": True, "tool_name": tool_name}


@app.delete("/api/workspaces/{workspace_id}/tools/manual/{operation_id}")
def delete_workspace_manual_tool(workspace_id: str, operation_id: str):
    removed = manual_tools.remove_manual_operation(operation_id, workspace_id=workspace_id)
    return {"ok": removed}


@app.get("/api/workspaces/{workspace_id}/flows")
def get_workspace_flows(workspace_id: str):
    return store.list_flows(workspace_id)


@app.get("/api/workspaces/{workspace_id}/flows/{name}")
def get_workspace_flow(workspace_id: str, name: str):
    try:
        return store.load_flow(name, workspace_id)
    except FileNotFoundError:
        raise HTTPException(404, f"Flow '{name}' not found in workspace '{workspace_id}'")


@app.put("/api/workspaces/{workspace_id}/flows/{name}")
def put_workspace_flow(workspace_id: str, name: str, body: SaveFlowRequest):
    expected_v = body.version if body.version is not None else body.graph.get("version")
    try:
        updated = store.save_flow(name, body.graph, expected_version=expected_v, workspace_id=workspace_id)
        return {"ok": True, "version": updated.get("version")}
    except store.FlowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/workspaces/{workspace_id}/flows/{name}")
def delete_workspace_flow(workspace_id: str, name: str):
    store.delete_flow(name, workspace_id)
    return {"ok": True}


def _apply_node_overrides(graph: dict, node_id: str, body: RunNodeRequest | None) -> None:
    if not body:
        return
    if body.data_override:
        graph.setdefault("data", {}).update(body.data_override)
    if body.args_override:
        def walk(steps):
            for s in steps:
                if s.get("id") == node_id:
                    s.setdefault("args", {}).update(body.args_override)
                    return True
                if s.get("then") and walk(s["then"]):
                    return True
                if s.get("else") and walk(s["else"]):
                    return True
                for b in s.get("branches", []):
                    if walk(b.get("steps", [])):
                        return True
            return False
        walk(graph.get("steps", []))


@app.get("/api/workspaces/{workspace_id}/flows/{name}/nodes/{node_id}/run")
@app.post("/api/workspaces/{workspace_id}/flows/{name}/nodes/{node_id}/run")
def run_workspace_flow_node(workspace_id: str, name: str, node_id: str, body: RunNodeRequest | None = None):
    try:
        graph = (body.graph if body and body.graph else None) or store.load_flow(name, workspace_id)
    except FileNotFoundError:
        if body and body.graph:
            graph = body.graph
        else:
            raise HTTPException(status_code=404, detail=f"Flow '{name}' not found")
    _apply_node_overrides(graph, node_id, body)
    ws = workspace_store.load_workspace(workspace_id)
    base_url = body.base_url if body else None
    api = _safe_build_client(workspace_id=workspace_id, base_url=base_url)
    try:
        return run_single_node(graph, node_id, api, workspace=ws)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/workspaces/{workspace_id}/flows/{name}/run")
def run_workspace_flow_rest(workspace_id: str, name: str, body: RunFlowRequest | None = None):
    try:
        graph = (body.graph if body and body.graph else None) or store.load_flow(name, workspace_id)
    except FileNotFoundError:
        if body and body.graph:
            graph = body.graph
        else:
            raise HTTPException(status_code=404, detail=f"Flow '{name}' not found")
    if body and body.params:
        graph.setdefault("data", {}).update(body.params)
    issues = validate_graph(graph, workspace_id)
    if issues:
        raise HTTPException(status_code=422, detail={"message": "Flow has incomplete required arguments", "issues": issues})
    ws = workspace_store.load_workspace(workspace_id)
    base_url = body.base_url if body else None
    api = _safe_build_client(workspace_id=workspace_id, base_url=base_url)
    flow_fn = make_flow_fn(graph, workspace=ws)
    report = run_flow(flow_fn, api)
    return asdict(report)


@app.get("/api/workspaces/{workspace_id}/flows/{name}/validate")
def validate_workspace_flow(workspace_id: str, name: str):
    try:
        graph = store.load_flow(name, workspace_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Flow '{name}' not found")
    issues = validate_graph(graph, workspace_id)
    return {"valid": not issues, "issues": issues}


@app.websocket("/ws/workspaces/{workspace_id}/flows/{name}/run")
async def run_workspace_flow_ws(websocket: WebSocket, workspace_id: str, name: str):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    try:
        graph = store.load_flow(name, workspace_id)
        params_raw = websocket.query_params.get("params")
        if params_raw:
            try:
                p_dict = json.loads(params_raw)
                if isinstance(p_dict, dict):
                    graph.setdefault("data", {}).update(p_dict)
            except Exception:
                pass
        issues = validate_graph(graph, workspace_id)
        if issues:
            await websocket.send_json({"type": "invalid", "issues": issues})
            return
        ws = workspace_store.load_workspace(workspace_id)
        base_url = websocket.query_params.get("base_url")
        api = _safe_build_client(workspace_id=workspace_id, base_url=base_url)
        flow_fn = make_flow_fn(graph, workspace=ws)

        def on_step(report):
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "step", "step": asdict(report)}), loop,
            )

        report = await loop.run_in_executor(None, lambda: run_flow(flow_fn, api, on_step=on_step))
        await websocket.send_json({"type": "done", "report": asdict(report)})
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        await websocket.close()


# ===========================================================================
# Tools, Specs & Flows (Active / Default Workspace Context)
# ===========================================================================

@app.get("/api/tools")
def get_tools():
    active_id = workspace_store.get_active_workspace_id()
    return list_tools(active_id)


@app.get("/api/specs")
def get_specs():
    active_id = workspace_store.get_active_workspace_id()
    spec_path = workspace_store.get_workspace_spec_path(active_id)
    if spec_path.exists() and spec_path.stat().st_size > 0:
        return [{"id": active_id, "title": f"Workspace ({active_id}) Spec"}]
    return []


@app.post("/api/specs")
def post_spec(body: ImportSpecRequest):
    try:
        yaml.safe_load(body.content)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse spec content: {exc}")
    active_id = workspace_store.get_active_workspace_id()
    workspace_store.save_workspace(active_id, openapi_spec=body.content)
    return {"ok": True}


@app.delete("/api/specs/{spec_id}")
def delete_spec(spec_id: str):
    active_id = workspace_store.get_active_workspace_id()
    spec_path = workspace_store.get_workspace_spec_path(active_id)
    if spec_path.exists():
        spec_path.unlink()
    return {"ok": True}


@app.post("/api/manual-tools")
def post_manual_tool(body: ManualToolRequest):
    active_id = workspace_store.get_active_workspace_id()
    tool_name = manual_tools.add_manual_operation(
        workspace_id=active_id,
        operation_id=body.operation_id,
        tag=body.tag,
        method=body.method,
        path=body.path,
        query_params=body.query_params,
        body_fields=body.body_fields,
        requires_auth=body.requires_auth,
        content_type=body.content_type,
    )
    return {"ok": True, "tool_name": tool_name}


@app.delete("/api/manual-tools/{operation_id}")
def delete_manual_tool(operation_id: str):
    active_id = workspace_store.get_active_workspace_id()
    removed = manual_tools.remove_manual_operation(operation_id, workspace_id=active_id)
    return {"ok": removed}


@app.get("/api/flows")
def get_flows():
    active_id = workspace_store.get_active_workspace_id()
    return store.list_flows(active_id)


@app.get("/api/flows/{name}")
def get_flow(name: str):
    active_id = workspace_store.get_active_workspace_id()
    return store.load_flow(name, active_id)


@app.put("/api/flows/{name}")
def put_flow(name: str, body: SaveFlowRequest):
    active_id = workspace_store.get_active_workspace_id()
    expected_v = body.version if body.version is not None else body.graph.get("version")
    try:
        updated = store.save_flow(name, body.graph, expected_version=expected_v, workspace_id=active_id)
        return {"ok": True, "version": updated.get("version")}
    except store.FlowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.delete("/api/flows/{name}")
def delete_flow(name: str):
    active_id = workspace_store.get_active_workspace_id()
    store.delete_flow(name, active_id)
    return {"ok": True}


@app.get("/api/flows/{name}/validate")
def validate_saved_flow(name: str):
    """Preflight a saved graph against its workspace OpenAPI contract."""
    active_id = workspace_store.get_active_workspace_id()
    try:
        graph = store.load_flow(name, active_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Flow '{name}' not found")
    issues = validate_graph(graph, active_id)
    return {"valid": not issues, "issues": issues}


@app.get("/api/flows/{name}/nodes/{node_id}/run")
@app.post("/api/flows/{name}/nodes/{node_id}/run")
def run_flow_node(name: str, node_id: str, body: RunNodeRequest | None = None):
    active_id = workspace_store.get_active_workspace_id()
    try:
        graph = (body.graph if body and body.graph else None) or store.load_flow(name, active_id)
    except FileNotFoundError:
        if body and body.graph:
            graph = body.graph
        else:
            raise HTTPException(status_code=404, detail=f"Flow '{name}' not found")
    _apply_node_overrides(graph, node_id, body)
    ws = workspace_store.load_workspace(active_id)
    base_url = body.base_url if body else None
    api = _safe_build_client(workspace_id=active_id, base_url=base_url)
    try:
        return run_single_node(graph, node_id, api, workspace=ws)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/flows/{name}/run")
def run_flow_rest(name: str, body: RunFlowRequest | None = None):
    active_id = workspace_store.get_active_workspace_id()
    graph = (body.graph if body and body.graph else None) or store.load_flow(name, active_id)
    if body and body.params:
        graph.setdefault("data", {}).update(body.params)
    issues = validate_graph(graph, active_id)
    if issues:
        raise HTTPException(status_code=422, detail={"message": "Flow has incomplete required arguments", "issues": issues})
    # Preserve the established runner path, but pass the preflighted ephemeral graph.
    if body and body.graph:
        body.graph = graph
    return run_workspace_flow_rest(active_id, name, body)


@app.websocket("/ws/flows/{name}/run")
async def run_flow_ws(websocket: WebSocket, name: str):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    try:
        active_id = workspace_store.get_active_workspace_id()
        graph = store.load_flow(name, active_id)
        params_raw = websocket.query_params.get("params")
        if params_raw:
            try:
                p_dict = json.loads(params_raw)
                if isinstance(p_dict, dict):
                    graph.setdefault("data", {}).update(p_dict)
            except Exception:
                pass
        issues = validate_graph(graph, active_id)
        if issues:
            await websocket.send_json({"type": "invalid", "issues": issues})
            return
        ws = workspace_store.load_workspace(active_id)
        base_url = websocket.query_params.get("base_url")
        api = _safe_build_client(workspace_id=active_id, base_url=base_url)
        flow_fn = make_flow_fn(graph, workspace=ws)

        def on_step(report):
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "step", "step": asdict(report)}), loop,
            )

        report = await loop.run_in_executor(None, lambda: run_flow(flow_fn, api, on_step=on_step))
        await websocket.send_json({"type": "done", "report": asdict(report)})
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        await websocket.close()


@app.websocket("/ws/workspaces/{workspace_id}/run")
async def run_workspace_ws(websocket: WebSocket, workspace_id: str):
    await websocket.accept()
    loop = asyncio.get_event_loop()
    try:
        def on_event(event: dict):
            asyncio.run_coroutine_threadsafe(websocket.send_json(event), loop)

        reports = await loop.run_in_executor(
            None, lambda: run_workspace_flows(workspace_id, on_event=on_event),
        )
        flow_results = [{"flow": report.name, "status": report.status} for report in reports]
        overall = "pass" if all(r["status"] == "pass" for r in flow_results) else "fail"
        await websocket.send_json({"type": "done", "status": overall, "flows": flow_results})
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        await websocket.close()


@app.websocket("/ws/assistant/{name}")
async def assistant_ws(websocket: WebSocket, name: str):
    await websocket.accept()
    loop = asyncio.get_event_loop()

    active_id = workspace_store.get_active_workspace_id()
    try:
        graph = store.load_flow(name, active_id)
    except FileNotFoundError:
        graph = {"name": name, "data": {}, "steps": []}
    history: list[dict] | None = None

    try:
        while True:
            raw = await websocket.receive_json()
            user_message = raw.get("message", "")

            def on_event(event: dict) -> None:
                asyncio.run_coroutine_threadsafe(websocket.send_json(event), loop)

            def do_turn() -> str:
                nonlocal history
                reply, new_history = assistant.run_assistant_turn(graph, history, user_message, on_event=on_event)
                history = new_history
                return reply

            try:
                reply = await loop.run_in_executor(None, do_turn)
            except assistant.AssistantError as exc:
                await websocket.send_json({"type": "error", "message": str(exc)})
                continue

            store.save_flow(graph["name"], graph, workspace_id=active_id)
            await websocket.send_json({"type": "reply", "message": reply})
            await websocket.send_json({"type": "graph", "graph": graph})
    except WebSocketDisconnect:
        pass


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
