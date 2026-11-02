"""Own stdio MCP subprocess and narrow current-job capability checks; models=0."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError
from support_controller import SupportController, SupportHTTPServer
from support_mcp import create_app, read_context
from support_store import SupportPaths, write_private


@pytest.fixture
def scoped_job(tmp_path):
    workspace = tmp_path / "synthetic-selected-room"
    workspace.mkdir()
    def no_native(*_args, **_kwargs):
        pytest.fail("MCP diagnostics must not create a native/model process")
    controller = SupportController(SupportPaths(workspace, root=tmp_path / "own-support-root"),
        adapter_factory=no_native, diagnostic_reader=lambda: {"paused": True, "agents": []})
    controller.open()
    server = SupportHTTPServer(controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def make_job(request_id):
        job, capability = controller.store.create_job("diagnose", request_id)
        native_id = "own-fixture-native-id"
        controller.store.reserve_native(job["id"], native_id)
        controller.store.receipt(job["id"], {"type": "input_attempt"})
        controller.store.receipt(job["id"], {"type": "session", "session_id": native_id})
        controller.store.profile_ready(job["id"])
        return job, capability
    job, capability = make_job("human-request-001")
    context_file = controller.paths.file("own-diagnostic-context.json")
    def context(job, capability):
        return {"runtime": server.runtime(), "job_id": job["id"], "token": capability, "deadline": job["deadline"]}
    write_private(context_file, json.dumps(context(job, capability)))
    yield controller, server, job, capability, context_file, make_job, context
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()
    controller.close()


def test_actual_isolated_stdio_lists_one_no_argument_tool_and_rejects_other_authority(scoped_job):
    controller, server, job, capability, context_file, _, _ = scoped_job
    async def scenario():
        # Exact production isolation flags; only this diagnostic helper is run.
        environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}}
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        parameters = StdioServerParameters(command=sys.executable,
            args=["-I", "-B", str(HERE / "support_mcp.py"), "--context-file", str(context_file)], env=environment)
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["guide_diagnose"]
                schema = listed.tools[0].inputSchema
                assert schema["properties"] == {} and schema["additionalProperties"] is False
                unsupported = await session.call_tool("Read", {"file_path": "private-file"})
                assert unsupported.isError is True
                switched = await session.call_tool("guide_diagnose", {"target": "peer-room"})
                assert switched.isError is True
                result = await session.call_tool("guide_diagnose", {})
                assert result.isError is False
                value = result.structuredContent
                if value is None:
                    value = json.loads(result.content[0].text)
                assert value["connection"] == "paused"
                assert controller.operator not in json.dumps(value)
                assert capability not in json.dumps(value)
                controller.store.finish(job["id"], text="selected result", native_id="own-fixture-native-id")
                revoked = await session.call_tool("guide_diagnose", {})
                assert revoked.isError is True
    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_old_mcp_context_snapshot_never_gains_later_job(scoped_job):
    controller, _, job, _, context_file, make_job, context = scoped_job
    app = create_app(context_file)
    controller.store.finish(job["id"], text="selected", native_id="own-fixture-native-id")
    next_job, next_capability = make_job("human-request-002")
    write_private(context_file, json.dumps(context(next_job, next_capability)))
    async def scenario():
        with pytest.raises(ToolError, match="This help request could not check the workroom"):
            await app.call_tool("guide_diagnose", {})
        next_app = create_app(context_file)
        response = await next_app.call_tool("guide_diagnose", {})
        assert response
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["expired", "nan", "extra_target", "wrong_service", "wrong_url"])
def test_invalid_context_refused_without_tool_or_native(scoped_job, change):
    controller, _, _, _, context_file, _, _ = scoped_job
    value = read_context(context_file)
    if change == "expired":
        value["deadline"] = time.time() - 1
    elif change == "nan":
        value["deadline"] = float("nan")
    elif change == "extra_target":
        value["target"] = "other-room"
    elif change == "wrong_service":
        value["runtime"]["service"] = "business"
    else:
        value["runtime"]["url"] = "https://not-local.example"
    write_private(context_file, json.dumps(value))
    with pytest.raises(RuntimeError):
        create_app(context_file)


def test_help_parser_has_no_service_model_or_storage_side_effect(monkeypatch):
    import support_controller
    def forbidden(*_args, **_kwargs):
        pytest.fail("--help must not construct a support service or touch its storage")
    monkeypatch.setattr(support_controller, "SupportPaths", forbidden)
    with pytest.raises(SystemExit) as result:
        support_controller.main(["--help"])
    assert result.value.code == 0
