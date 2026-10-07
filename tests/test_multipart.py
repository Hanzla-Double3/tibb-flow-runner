import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.client.base_client import BaseClient, StepResult
from core.client.dynamic import Operation, build_callable
from core import manual_tools
from ui_server.tools_info import list_tools


def test_base_client_multipart_file_paths_and_data():
    """Verify BaseClient automatically opens files with @ or path, and sends data."""
    with tempfile.NamedTemporaryFile(mode="w+b", delete=False, suffix=".txt") as tf:
        tf.write(b"Hello multipart world")
        tf_path = tf.name

    try:
        mock_session = MagicMock(spec=requests.Session)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"uploaded": True}
        mock_resp.text = '{"uploaded": true}'
        mock_session.request.return_value = mock_resp

        client = BaseClient("http://test.local", session=mock_session)

        # Call with @path for file and regular string in data
        res = client.request(
            "POST",
            "/upload",
            data={"title": "Test Doc", "active": "true"},
            files={"document": f"@{tf_path}"},
            headers={"Content-Type": "multipart/form-data"},  # should be stripped so requests adds boundary
        )

        assert res.status_code == 200
        assert res.body == {"uploaded": True}

        # Check mock call
        call_args, call_kwargs = mock_session.request.call_args
        assert call_args[0] == "POST"
        assert call_args[1] == "http://test.local/upload"
        assert call_kwargs["data"] == {"title": "Test Doc", "active": "true"}

        # Prepared files should have the opened file stream
        files_sent = call_kwargs["files"]
        assert "document" in files_sent
        filename, f_stream = files_sent["document"]
        assert filename == os.path.basename(tf_path)

        # Header with multipart/form-data should have been stripped so boundary is not lost
        req_headers = call_kwargs["headers"]
        assert req_headers is None or "content-type" not in {k.lower(): v for k, v in req_headers.items()}
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)


def test_base_client_bytes_file():
    """Verify passing raw bytes as a file entry is packaged with a default filename."""
    mock_session = MagicMock(spec=requests.Session)
    mock_resp = MagicMock(status_code=201, headers={}, text="OK")
    mock_resp.json.side_effect = ValueError("Not JSON")
    mock_session.request.return_value = mock_resp

    client = BaseClient("http://test.local", session=mock_session)
    res = client.request(
        "POST",
        "/upload-bytes",
        files={"avatar": b"fake image bytes"},
    )
    assert res.status_code == 201
    call_kwargs = mock_session.request.call_args[1]
    filename, f_bytes = call_kwargs["files"]["avatar"]
    assert filename == "avatar.bin"
    assert f_bytes == b"fake image bytes"


def test_build_callable_multipart_auto_sorting():
    """Verify build_callable sorts file vs data fields when body_content_type is multipart/form-data."""
    with tempfile.NamedTemporaryFile(mode="w+b", delete=False, suffix=".csv") as tf:
        tf.write(b"col1,col2\n1,2\n")
        tf_path = tf.name

    try:
        mock_client = MagicMock(spec=BaseClient)
        mock_client.testing_api_key = None
        mock_client.request.return_value = StepResult(status_code=200, body={"ok": True})

        op = Operation(
            operation_id="upload_csv",
            tag="data_ops",
            method="POST",
            path="/import/csv",
            parameters=[],
            body_schema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "format": "binary"},
                    "description": {"type": "string"},
                    "tags": {"type": "array"},
                    "overwrite": {"type": "boolean"},
                },
            },
            body_content_type="multipart/form-data",
        )

        fn = build_callable(op, mock_client)
        fn(file=f"@{tf_path}", description="Important data", tags=["a", "b"], overwrite=True)

        call_args, call_kwargs = mock_client.request.call_args
        assert call_args[0] == "POST"
        assert call_args[1] == "/import/csv"

        # Files dict should contain the file
        assert "file" in call_kwargs["files"]
        assert call_kwargs["files"]["file"] == f"@{tf_path}"

        # Data dict should contain description, serialized tags, and string boolean
        assert call_kwargs["data"]["description"] == "Important data"
        assert call_kwargs["data"]["tags"] == '["a", "b"]'
        assert call_kwargs["data"]["overwrite"] == "true"
        assert call_kwargs["json"] is None
    finally:
        if os.path.exists(tf_path):
            os.remove(tf_path)


@pytest.fixture(autouse=True)
def clean_manual_spec():
    orig = manual_tools.load_custom_spec()
    yield
    manual_tools.save_custom_spec(orig)


def test_manual_tools_multipart_generation():
    """Verify add_manual_operation generates multipart spec and tools_info reports it."""
    manual_tools.add_manual_operation(
        operation_id="upload_test_receipt",
        tag="receipts",
        method="POST",
        path="/receipts/upload",
        body_fields=["receipt_file", "amount", "notes"],
        content_type="multipart/form-data",
    )

    tools = list_tools()
    receipt_tool = next((t for t in tools if t["name"] == "receipts__upload_test_receipt"), None)
    assert receipt_tool is not None
    assert receipt_tool["body_content_type"] == "multipart/form-data"

    fields_by_name = {f["name"]: f for f in receipt_tool["fields"]}
    assert fields_by_name["receipt_file"]["type"] == "file"
    assert fields_by_name["amount"]["type"] == "string"
    assert fields_by_name["notes"]["type"] == "string"


def test_mcp_create_manual_api_tool_multipart():
    """Verify MCP create_manual_api_tool tool works seamlessly for agents."""
    from scripts.mcp_server import create_manual_api_tool

    res = create_manual_api_tool(
        operation_id="upload_avatar",
        tag="profile",
        method="POST",
        path="/profile/avatar",
        body_fields=["avatar", "caption"],
        content_type="multipart/form-data",
    )
    assert res.success is True
    assert res.data["tool_name"] == "profile__upload_avatar"
    assert res.data["content_type"] == "multipart/form-data"


def test_run_multipart_node_execution():
    """Verify executing a multipart node in a flow runs properly and returns response."""
    from core.graph_builder import run_single_node, clear_flow_session

    class MockProfileAPI:
        def upload_avatar(self, avatar, caption):
            return StepResult(
                status_code=200,
                headers={"content-type": "application/json"},
                body={"avatar_url": "http://img.local/avatar.png", "caption": caption},
                url="http://mock/profile/avatar",
                method="POST",
            )

    class MockAPI:
        def __init__(self):
            self.profile = MockProfileAPI()

    clear_flow_session("test_multipart_flow")
    graph = {
        "name": "test_multipart_flow",
        "steps": [
            {
                "id": "step_upload",
                "kind": "call",
                "tool": "profile__upload_avatar",
                "bind_as": "upload_res",
                "args": {"avatar": "@/tmp/test.png", "caption": "My new photo"},
                "validate": [
                    {"id": "c1", "kind": "check", "check_type": "status", "status": 200},
                    {"id": "c2", "kind": "check", "check_type": "json", "path": "caption", "equals": "My new photo"},
                ],
            }
        ],
    }

    res = run_single_node(graph, "step_upload", MockAPI())
    assert res["ok"] is True
    assert res["status"] == "pass"
    assert res["response"]["status_code"] == 200
    assert res["response"]["body"]["caption"] == "My new photo"

