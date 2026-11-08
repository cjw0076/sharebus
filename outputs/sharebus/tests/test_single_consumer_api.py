"""Read/receive and final-dispatch contracts; no native provider is invoked."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import auto_enroll
import mcp_server


@pytest.fixture
def tool_surface(tmp_path, monkeypatch):
    for key in tuple(auto_enroll.os.environ):
        if key.startswith("SHAREBUS_"):
            monkeypatch.delenv(key)
    state = {"own_agent": {"id": "owned-agent", "provider": "claude", "mode": "external_attached"}}
    requests = []

    def authenticated(self, *args):
        return tmp_path, "synthetic-agent-token", state

    def request(self, root, path, token, payload=None):
        assert root == tmp_path and token == "synthetic-agent-token"
        requests.append((path, payload))
        return {"events": [], "last_seq": 0} if path.startswith("/api/events?") else {"messages": [], "jobs": []}

    monkeypatch.setattr(mcp_server.BusClient, "authenticated", authenticated)
    monkeypatch.setattr(mcp_server.BusClient, "request", request)
    monkeypatch.setattr(mcp_server, "_host_metadata", lambda context: {})
    server = mcp_server.build_server("claude", tmp_path)

    def invoke(name, arguments=None):
        return asyncio.run(server._tool_manager.call_tool(name, arguments or {}, context=server.get_context()))

    return server, state, requests, invoke


def test_surface_read_is_explicit_observation_without_ack_or_claim(tool_surface):
    _, _, requests, invoke = tool_surface
    result = invoke("sharebus_read")
    assert not result.isError
    assert requests == [("/api/events?after=0", None),
                        ("/api/agent/inbox", {"after": 0, "ack": [], "claim": False})]


def test_surface_read_ack_never_claims_following_work(tool_surface):
    _, _, requests, invoke = tool_surface
    result = invoke("sharebus_read", {"after": 17, "ack": ["owned-completed-job"]})
    assert not result.isError
    assert requests[-1] == ("/api/agent/inbox", {"after": 17, "ack": ["owned-completed-job"], "claim": False})


def test_surface_native_inventory_exposes_nine_tools_including_receive_and_peer(tool_surface):
    server, _, _, _ = tool_surface
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == set(mcp_server.TOOLS)
    assert len(names) == 9 and {"sharebus_receive", "sharebus_peer"} <= names
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in ("sharebus_read", "sharebus_receive"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.idempotentHint is False


def test_surface_receive_is_explicit_single_manual_claim(tool_surface):
    _, _, requests, invoke = tool_surface
    result = invoke("sharebus_receive", {"after": 7})
    assert not result.isError
    assert requests == [("/api/agent/inbox", {"after": 7, "ack": [], "claim": True})]


@pytest.mark.parametrize("mode", ["native_attached", "managed_new", "managed_resume"])
def test_surface_receive_rejects_automatic_lanes_before_request(tool_surface, mode):
    _, state, requests, invoke = tool_surface
    state["own_agent"]["mode"] = mode
    result = invoke("sharebus_receive")
    assert result.isError and requests == []


def test_surface_managed_inventory_stays_eight_without_receive(tmp_path, monkeypatch):
    for key in tuple(auto_enroll.os.environ):
        if key.startswith("SHAREBUS_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("SHAREBUS_MANAGED", "1")
    monkeypatch.setenv("SHAREBUS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("SHAREBUS_AGENT_ID", "owned-managed")
    monkeypatch.setenv("SHAREBUS_AGENT_TOKEN_FILE", str(tmp_path / ".sharebus/private/credentials/owned-managed.token"))
    server = mcp_server.build_server("claude", tmp_path)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert len(names) == 8
    assert names == set(mcp_server.MANAGED_TOOLS)
    assert "sharebus_peer" in names and "sharebus_receive" not in names


@pytest.mark.parametrize("command,claim", [("read", False), ("receive", True)])
def test_cached_cli_read_and_receive_preserve_private_context(tmp_path, monkeypatch, command, claim):
    context = tmp_path / ".sharebus/private/auto/contexts/owned.json"
    token_file = tmp_path / ".sharebus/private/credentials/owned.token"
    binding = {"agent_id": "owned", "provider": "claude", "native_session_id": "same-native",
               "workspace": str(tmp_path), "token_file": str(token_file)}
    calls = []
    monkeypatch.setattr(auto_enroll, "_root", lambda path: tmp_path)
    monkeypatch.setattr(auto_enroll, "_read", lambda root, path, **kwargs:
                        {"provider": "claude", "native_session_id": "same-native"} if path == context else "synthetic-private-token")
    monkeypatch.setattr(auto_enroll, "connect_context", lambda *args, **kwargs: binding)
    monkeypatch.setattr(auto_enroll, "_request", lambda root, path, token, body=None, **kwargs:
                        calls.append((path, token, body)) or {"jobs": [], "messages": []})
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = auto_enroll.main([command, "--context-file", str(context), "--after", "8"])
    assert code == 0 and errors.getvalue() == ""
    assert calls == [("/api/agent/inbox", "synthetic-private-token", {"after": 8, "ack": [], "claim": claim})]
    assert "synthetic-private-token" not in output.getvalue()
    assert json.loads(output.getvalue()) == {"jobs": [], "messages": []}


def test_cached_cli_receive_still_requires_context_file():
    with pytest.raises(SystemExit) as stopped, redirect_stderr(io.StringIO()):
        auto_enroll.main(["receive"])
    assert stopped.value.code == 2


def test_source_import_provenance_is_candidate():
    for module in (auto_enroll, mcp_server):
        assert Path(module.__file__).resolve().is_relative_to(HERE)


@pytest.fixture
def api_room(tmp_path):
    from daemon import BusHTTPServer

    server = BusHTTPServer(tmp_path, providers="none", enable_native=False, discovery=[])
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), name="surface-fixture-http")
    thread.start()

    def request(path, body, token):
        value = Request(server.url + path, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "X-Sharebus-Request": "1", "Authorization": "Bearer " + token})
        try:
            with urlopen(value, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    try:
        yield server, request
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.close()
        assert not thread.is_alive()


def native_job(server):
    agent, token = server.store.register("claude", mode="native_attached", native_session_id="same-native-session")
    server.store.send("user", agent["id"], "selected native task", "task", budget=1)
    job = server.store.claim(agent["id"])
    server.native_enabled = True  # No Supervisor is started: this fixture only observes reservation state.
    return agent, token, job, {"workspace_id": server.store.workspace_id, "runtime_nonce": server.nonce,
        "job_id": job["id"], "event_id": job["event_id"], "root_id": job["root_id"],
        "native_session_id": agent["native_session_id"], "generation": agent["generation"],
        "provider": agent["provider"], "native_address": agent["native_address"]}


def test_dispatch_http_reserves_once_for_authenticated_actor_at_last_budget_turn(api_room):
    server, request = api_room
    agent, token, job, body = native_job(server)
    status, result = request("/api/native/dispatch", body, token)
    assert status == 200
    reservation = result["dispatch"]
    assert reservation["job_id"] == job["id"] and reservation["execution_owner"] == "native_dispatch"
    assert reservation["native_input_reserved_at"] > 0
    assert request("/api/native/dispatch", body, token)[0] == 409
    current = next(row for row in server.store.jobs() if row["id"] == job["id"])
    assert current["native_input_reserved_at"] == reservation["native_input_reserved_at"]
    assert current["state"] == "running"  # Reservation is not native input/acceptance/completion proof.


@pytest.mark.parametrize("mutation,status", [
    ({"actor": "someone-else"}, 400), ({"agent_id": "someone-else"}, 400),
    ({"workspace_id": "another-room"}, 409), ({"runtime_nonce": "stale-runtime"}, 409),
    ({"native_session_id": "different-native"}, 409), ({"provider": "codex"}, 409),
    ({"generation": 9999}, 409), ({"native_address": "different-address"}, 409),
    ({"generation": True}, 400), ({"native_address": []}, 400), ({"job_id": []}, 400),
])
def test_dispatch_http_rejects_spoofed_stale_or_malformed_binding_without_reservation(api_room, mutation, status):
    server, request = api_room
    _, token, job, body = native_job(server)
    assert request("/api/native/dispatch", {**body, **mutation}, token)[0] == status
    assert next(row for row in server.store.jobs() if row["id"] == job["id"])["native_input_reserved_at"] is None


def test_dispatch_http_operator_and_peer_cannot_reserve_owned_job(api_room):
    server, request = api_room
    _, _, job, body = native_job(server)
    _, peer_token = server.store.register("claude", mode="native_attached", native_session_id="peer-native-session")
    assert request("/api/native/dispatch", body, server.store.operator_token)[0] == 403
    assert request("/api/native/dispatch", body, peer_token)[0] == 409
    assert next(row for row in server.store.jobs() if row["id"] == job["id"])["native_input_reserved_at"] is None


@pytest.mark.parametrize("control", ["paused", "disabled", "closing"])
def test_dispatch_http_control_denies_before_reservation(api_room, control):
    server, request = api_room
    _, token, job, body = native_job(server)
    if control == "paused":
        server.store.set_paused(True)
    elif control == "disabled":
        server.native_enabled = False
    else:
        server.stopping = True
    assert request("/api/native/dispatch", body, token)[0] == 409
    assert next(row for row in server.store.jobs() if row["id"] == job["id"])["native_input_reserved_at"] is None


def test_inbox_http_default_observes_and_explicit_receive_grants_once(api_room):
    server, request = api_room
    agent, token = server.store.register("claude", mode="external_attached", native_session_id="manual-native")
    first = server.store.send("user", agent["id"], "first untouched body", "task")["jobs"][0]
    server.store.send("user", agent["id"], "second untouched body", "task")
    before = server.store.jobs()
    status, preview = request("/api/agent/inbox", {}, token)
    assert status == 200 and preview["jobs"] == [] and server.store.jobs() == before
    assert len(preview["messages"]) == 2 and all(row["can_execute"] is False for row in preview["messages"])
    assert [row["body"] for row in preview["messages"]] == ["first untouched body", "second untouched body"]
    status, received = request("/api/agent/inbox", {"claim": True}, token)
    assert status == 200 and len(received["jobs"]) == 1
    assert received["jobs"][0]["id"] == first["id"] and received["jobs"][0]["can_execute"] is True
    assert request("/api/agent/inbox", {"claim": True}, token)[1]["jobs"] == []
    assert request("/api/agent/inbox", {"ack": [first["id"]]}, token)[1]["jobs"] == []
    assert request("/api/agent/inbox", {"ack": [first["id"]]}, token)[1]["jobs"] == []
    assert sorted(row["state"] for row in server.store.jobs()) == ["completed", "queued"]


def test_inbox_http_native_job_is_preview_only_and_cannot_be_manually_acked(api_room):
    server, request = api_room
    _, token, job, _ = native_job(server)
    before = server.store.jobs()
    status, preview = request("/api/agent/inbox", {}, token)
    assert status == 200 and preview["jobs"] == []
    assert preview["messages"][0]["can_execute"] is False
    assert request("/api/agent/inbox", {"claim": True}, token)[0] == 409
    assert request("/api/agent/inbox", {"ack": [job["id"]]}, token)[0] == 409
    assert server.store.jobs() == before
