"""Owned loopback/controller tests using fake native edges only."""
from contextlib import contextmanager
from email.message import Message
import http.client
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters import TurnResult
import support_controller as module
from support_controller import SupportClient, SupportController, SupportHTTPServer, project_diagnostic
from support_store import SupportError, SupportPaths, read_private, write_private
from support_auth import request_headers, response_signature, token_key
from support_native import GuideProfileUnavailable


class FakeAdapter:
    instances = []
    behavior = "reply"
    entered = threading.Event()
    release = threading.Event()

    def __init__(self, workspace, context_file, **callbacks):
        self.workspace, self.context_file, self.callbacks = workspace, context_file, callbacks
        self.session_id = None
        self.closed = False
        self.interrupted = False
        self.prompts = []
        self.__class__.instances.append(self)

    def turn(self, prompt, timeout):
        self.prompts.append(prompt)
        native = self.session_id or str(uuid.uuid4())
        self.callbacks["on_reserved"](native)
        if self.behavior == "blocked_before_input":
            self.entered.set()
            self.release.wait(5)
        self.callbacks["on_event"]({"type": "input_attempt"})
        self.callbacks["on_identity"](native)
        self.session_id = native
        self.callbacks["on_profile"]()
        self.entered.set()
        if self.behavior == "raise_after_input":
            raise OSError("PRIVATE_LOCAL_PATH_AND_SECRET")
        if self.behavior == "blocked_after_input":
            self.release.wait(5)
        return TurnResult("이 작업실은 멈춰 있으며 설정을 바꾸지 않았습니다.", native, True,
                          input_attempted=True, accepted=True, status="completed")

    def close(self):
        self.closed = True

    def interrupt(self):
        self.interrupted = True
        self.release.set()
        self.close()


@pytest.fixture
def service(tmp_path, monkeypatch):
    workspace = tmp_path / "선택한 작업실"
    workspace.mkdir()
    paths = SupportPaths(workspace, root=tmp_path / "private-help")
    FakeAdapter.instances = []
    FakeAdapter.behavior = "reply"
    FakeAdapter.entered = threading.Event()
    FakeAdapter.release = threading.Event()

    def forbidden(*_args, **_kwargs):
        pytest.fail("A real OS/native process, model or business control must not run")
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.Client, "start", forbidden)
    monkeypatch.setattr(module.Client, "open", forbidden)
    business = {"paused": True, "auto": {"suspended": True}, "agents": [
        {"provider": "claude", "state": "idle", "native_session_id": "PRIVATE_BUS_NATIVE_ID"},
        {"provider": "codex", "state": "needs_human", "token": "PRIVATE_BUS_TOKEN"}],
        "jobs": [{"body": "PRIVATE_BUS_TRANSCRIPT"}], "workspace": {"path": str(workspace)}}
    reads = []
    def reader():
        reads.append("read")
        return business
    controller = SupportController(paths, adapter_factory=FakeAdapter, diagnostic_reader=reader)
    controller.open()
    server = SupportHTTPServer(controller)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    write_private(paths.file("runtime.json"), json.dumps(server.runtime()))
    yield SimpleNamespace(workspace=workspace, paths=paths, controller=controller, server=server,
                          business=business, reads=reads)
    FakeAdapter.release.set()
    server.shutdown()
    worker.join(timeout=3)
    server.server_close()
    controller.close()


def request(service, path, body=None, *, token=True, origin=True, headers=None, method=None):
    connection = http.client.HTTPConnection("127.0.0.1", service.server.server_port, timeout=10)
    values = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        values.update({"Content-Type": "application/json", "X-Sharebus-Request": "1"})
        if origin:
            values["Origin"] = service.server.url
    if token:
        private = service.controller.operator if token is True else token
        actor = "operator"
        if token is not True:
            with service.controller.store.connection() as db:
                job = db.execute("SELECT id FROM jobs WHERE cap_hash=?", (token_key(token),)).fetchone()
            if job:
                actor = "diagnostic:" + job["id"]
        values.update(request_headers(token_key(private), service.server.runtime(), actor,
            method or ("POST" if body is not None else "GET"), path, payload or b""))
    values.update(headers or {})
    connection.request(method or ("POST" if body is not None else "GET"), path, payload, values)
    response = connection.getresponse()
    raw = response.read()
    result = json.loads(raw) if "json" in response.headers.get("Content-Type", "") else raw
    status, reply_headers = response.status, dict(response.headers)
    connection.close()
    return status, result, reply_headers


def wait_idle(service, timeout=8):
    deadline = time.monotonic() + timeout
    while service.controller.state()["guide"]["state"] == "working" and time.monotonic() < deadline:
        time.sleep(0.02)
    for worker in service.controller._threads:
        worker.join(timeout=2)
    return service.controller.state()


def test_open_reuses_binding_without_model_or_business_mutation(service):
    before = json.dumps(service.business, sort_keys=True)
    for _ in range(3):
        code, state, _ = request(service, "/api/support/open", {"provider": "claude"})
        assert code == 200 and state["guide"]["state"] == "ready"
    assert FakeAdapter.instances == []
    assert list(service.workspace.iterdir()) == []
    assert json.dumps(service.business, sort_keys=True) == before
    assert state["workspace"]["status"] == "paused"


@pytest.mark.parametrize("reason", ["native_missing", "unsupported_version", "profile_unverified"])
def test_unavailable_profile_is_disabled_error_not_native_approval(service, monkeypatch, reason):
    def unavailable(*_args, **_kwargs):
        raise GuideProfileUnavailable(reason)
    monkeypatch.setattr(FakeAdapter, "turn", unavailable)
    before_business = json.dumps(service.business, sort_keys=True)
    assert request(service, "/api/support/message", {"text": "help", "request_id": "profile-request-001"})[0] == 202
    state = wait_idle(service)
    assert state["guide"]["state"] == "error" and state["guide"]["reason_code"] == reason
    assert not state["guide"]["can_send"] and state["guide"]["active_request_id"] is None
    assert state["messages"][0]["status"] == "failed"
    assert [message["role"] for message in state["messages"]] == ["user"]
    assert request(service, "/api/support/message", {"text": "retry", "request_id": "profile-request-002"})[0] == 409
    with service.controller.store.connection() as db:
        row = db.execute("SELECT attempted,cap_hash FROM jobs").fetchone()
        assert row["attempted"] == 0 and row["cap_hash"] is None
    assert service.controller.store.binding()["native_id"] is None
    assert FakeAdapter.instances[0].closed and len(FakeAdapter.instances) == 1
    assert json.dumps(service.business, sort_keys=True) == before_business
    assert list(service.workspace.iterdir()) == []


def test_explicit_reopen_reprobes_profile_without_model_preserving_native_and_history(service, monkeypatch):
    assert request(service, "/api/support/message", {"text": "first", "request_id": "profile-request-001"})[0] == 202
    wait_idle(service)
    native_id = service.controller.store.binding()["native_id"]
    def unavailable(*_args, **_kwargs):
        raise GuideProfileUnavailable("unsupported_version")
    monkeypatch.setattr(FakeAdapter, "turn", unavailable)
    assert request(service, "/api/support/message", {"text": "later", "request_id": "profile-request-002"})[0] == 202
    before = wait_idle(service)
    probes = []
    def still_unverified():
        probes.append("version-only")
        raise GuideProfileUnavailable("profile_unverified")
    monkeypatch.setattr(module, "verified_executable", still_unverified)
    assert service.controller.state()["guide"]["reason_code"] == "unsupported_version"
    assert probes == []  # Polling never probes or clears an unsupported profile.
    code, unavailable_state, _ = request(service, "/api/support/open", {"provider": "claude"})
    assert code == 200 and unavailable_state["guide"]["reason_code"] == "profile_unverified"
    assert not unavailable_state["guide"]["can_send"]
    monkeypatch.setattr(module, "verified_executable", lambda: probes.append("verified-version") or "verified-native")
    code, ready, _ = request(service, "/api/support/open", {"provider": "claude"})
    assert code == 200 and ready["guide"]["state"] == "ready" and ready["guide"]["can_send"]
    assert ready["guide"]["reason_code"] is None and ready["messages"] == before["messages"]
    assert service.controller.store.binding()["native_id"] == native_id
    assert probes == ["version-only", "verified-version"] and len(FakeAdapter.instances) == 2


def test_reopen_of_unknown_input_does_not_probe_clear_or_replay(service, monkeypatch):
    FakeAdapter.behavior = "raise_after_input"
    request(service, "/api/support/message", {"text": "help", "request_id": "unknown-request-001"})
    before = wait_idle(service)
    assert before["guide"]["state"] == "unknown"
    monkeypatch.setattr(module, "verified_executable", lambda: pytest.fail("Unknown work must not trigger a profile reprobe"))
    code, state, _ = request(service, "/api/support/open", {"provider": "claude"})
    assert code == 200 and state == before and len(FakeAdapter.instances) == 1


def test_help_worker_start_failure_cancels_unattempted_job_and_revokes_capability(service, monkeypatch):
    original_start = threading.Thread.start
    def start(thread):
        if thread.name == "sharebus-help":
            raise RuntimeError("PRIVATE_THREAD_START_FAILURE")
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, "start", start)
    code, error, _ = request(service, "/api/support/message", {"text": "help", "request_id": "thread-request-001"})
    assert code == 503 and error["code"] == "connection" and "PRIVATE_THREAD" not in json.dumps(error)
    state = service.controller.state()
    assert state["guide"]["state"] == "ready" and state["guide"]["can_send"]
    assert state["messages"][0]["status"] == "cancelled"
    with service.controller.store.connection() as db:
        job = db.execute("SELECT * FROM jobs").fetchone()
        assert job["state"] == "cancelled" and job["attempted"] == 0 and job["cap_hash"] is None
    assert service.controller._threads == [] and FakeAdapter.instances == []
    assert request(service, "/api/support/message", {"text": "help", "request_id": "thread-request-001"})[1]["job_id"] == job["id"]
    assert FakeAdapter.instances == []
    monkeypatch.setattr(threading.Thread, "start", original_start)
    assert request(service, "/api/support/message", {"text": "new human request", "request_id": "thread-request-002"})[0] == 202
    assert wait_idle(service)["guide"]["state"] == "ready" and len(FakeAdapter.instances) == 1


def test_help_worker_constructor_failure_does_not_leave_working_job(service, monkeypatch):
    def no_thread(*_args, **_kwargs):
        raise RuntimeError("private thread construction failure")
    with monkeypatch.context() as patch:
        patch.setattr(module.threading, "Thread", no_thread)
        with pytest.raises(SupportError) as failure:
            service.controller.message("help", "thread-request-001")
    assert failure.value.code == "connection" and service.controller._threads == []
    state = service.controller.state()
    assert state["guide"]["state"] == "ready" and state["messages"][0]["status"] == "cancelled"
    assert FakeAdapter.instances == []


def test_selected_reply_same_native_resume_and_lost_ack_reconciliation(service):
    payload = {"text": "왜 멈춰 있나요?", "request_id": "human-request-001"}
    code, accepted, _ = request(service, "/api/support/message", payload)
    assert code == 202
    state = wait_idle(service)
    assert state["guide"]["state"] == "ready"
    assert {message["request_id"] for message in state["messages"]} == {payload["request_id"]}
    code, duplicate, _ = request(service, "/api/support/message", payload)
    assert code == 202 and duplicate["job_id"] == accepted["job_id"]
    assert len(FakeAdapter.instances) == 1
    assert request(service, "/api/support/message", {**payload, "text": "changed"})[0] == 409
    assert request(service, "/api/support/message", {"text": "기억하시나요?", "request_id": "human-request-002"})[0] == 202
    state = wait_idle(service)
    assert len(FakeAdapter.instances) == 2
    assert FakeAdapter.instances[0].session_id == FakeAdapter.instances[1].session_id
    assert all(adapter.closed for adapter in FakeAdapter.instances)
    assert service.controller.operator not in "\n".join(FakeAdapter.instances[0].prompts)
    assert str(service.workspace) not in "\n".join(FakeAdapter.instances[0].prompts)


def test_exception_in_existing_native_after_attempt_is_unknown_no_retry(service):
    request(service, "/api/support/message", {"text": "first", "request_id": "human-request-001"})
    wait_idle(service)
    FakeAdapter.behavior = "raise_after_input"
    request(service, "/api/support/message", {"text": "second", "request_id": "human-request-002"})
    state = wait_idle(service)
    assert state["guide"]["state"] == "unknown" and state["guide"]["can_send"] is False
    assert request(service, "/api/support/message", {"text": "replay", "request_id": "human-request-003"})[0] == 409
    assert len(FakeAdapter.instances) == 2
    assert "PRIVATE_LOCAL_PATH_AND_SECRET" not in json.dumps(state)


def test_cancel_help_only_revokes_diagnostic_and_fences_late_output(service):
    FakeAdapter.behavior = "blocked_after_input"
    before = json.dumps(service.business, sort_keys=True)
    request(service, "/api/support/message", {"text": "check", "request_id": "human-request-001"})
    assert FakeAdapter.entered.wait(5)
    context = json.loads(read_private(FakeAdapter.instances[0].context_file))
    code, diagnostic, _ = request(service, "/api/internal/diagnose", {}, token=context["token"])
    assert code == 200 and diagnostic["connection"] == "paused"
    assert request(service, "/api/support/cancel", {"request_id": "human-request-001"})[0] == 200
    assert request(service, "/api/internal/diagnose", {}, token=context["token"])[0] in {403, 409}
    state = wait_idle(service)
    assert state["guide"]["state"] == "unknown"
    assert [message["role"] for message in state["messages"]] == ["user"]
    assert FakeAdapter.instances[0].interrupted
    assert json.dumps(service.business, sort_keys=True) == before


def test_operator_and_current_job_diagnostic_authority_are_disjoint(service):
    FakeAdapter.behavior = "blocked_after_input"
    request(service, "/api/support/message", {"text": "check", "request_id": "human-request-001"})
    assert FakeAdapter.entered.wait(5)
    context = json.loads(read_private(FakeAdapter.instances[0].context_file))
    cap = context["token"]
    assert cap != service.controller.operator
    assert request(service, "/api/support/state", token=cap)[0] == 403
    assert request(service, "/api/support/message", {"text": "forged", "request_id": "peer-forged-001"}, token=cap)[0] == 403
    assert request(service, "/api/internal/diagnose", {})[0] == 403
    assert request(service, "/api/internal/diagnose", {"target": "another room"}, token=cap)[0] == 400
    code, value, _ = request(service, "/api/internal/diagnose", {}, token=cap)
    assert code == 200
    for private in ("PRIVATE_BUS_NATIVE_ID", "PRIVATE_BUS_TOKEN", "PRIVATE_BUS_TRANSCRIPT", str(service.workspace)):
        assert private not in json.dumps(value)


@pytest.mark.parametrize("path,body", [("/api/support/open", {"provider": "codex"}),
    ("/api/support/message", {"text": "hello", "request_id": "human-request-001", "to": "all"}),
    ("/api/control", {"action": "resume"})])
def test_no_provider_fallback_peer_route_or_business_control(service, path, body):
    assert request(service, path, body)[0] in {404, 409}
    assert FakeAdapter.instances == []
    assert service.business["paused"] is True


@pytest.mark.parametrize("headers,origin", [({"Origin": "https://other.example"}, True),
    ({}, False), ({"Host": "other.example"}, True), ({"X-Sharebus-Request": "0"}, True),
    ({"Transfer-Encoding": "chunked"}, True), ({"Content-Type": "text/plain"}, True)])
def test_host_origin_json_and_request_markers_guard_mutations(service, headers, origin):
    code, error, _ = request(service, "/api/support/open", {"provider": "claude"}, headers=headers, origin=origin)
    assert code == 403
    assert set(error) == {"code", "message"}
    assert FakeAdapter.instances == []


def test_cookie_bootstrap_is_scoped_no_cors_and_no_credential_response_body(service):
    assert request(service, "/api/support/state", token=False)[0] == 403
    ticket = request(service, "/api/support/ticket", {})[1]["ticket"]
    assert request(service, "/api/auth", {"ticket": ticket}, token=False, origin=False)[0] == 403
    code, value, headers = request(service, "/api/auth", {"ticket": ticket}, token=False)
    assert code == 200 and value["ok"] is True
    assert set(value) == {"ok", "browser_proof"}
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert service.controller.operator not in cookie
    assert "Access-Control-Allow-Origin" not in headers
    assert request(service, "/api/support/state", token=False, headers={"Cookie": cookie.split(";")[0]})[0] == 403
    browser = {"Cookie": cookie.split(";")[0], "X-Sharebus-Support-Proof": value["browser_proof"]}
    assert request(service, "/api/support/state", token=False, headers=browser)[0] == 200
    assert request(service, "/api/support/ticket", {}, token=False, headers=browser)[0] == 403
    assert request(service, "/api/auth", {"ticket": ticket}, token=False)[0] == 403
    assert request(service, "/api/auth", {"token": service.controller.operator}, token=False)[0] == 403
    assert request(service, "/api/support/state", token="wrong-token")[0] == 403
    assert service.controller.operator not in json.dumps(value)


def test_only_finite_static_resources_are_served(service):
    for path in ("/../support_store.py", "/assets/../../support_store.py", "/api/support/state?token=secret", "/operator.token"):
        assert request(service, path, token=False)[0] == 404
    assert request(service, "/assets/characters/sharebus-claude.svg", token=False)[0] == 200
    assert request(service, "/health", token=False)[1]["service"] == module.SERVICE


def test_diagnostic_offline_never_infers_unpaused_and_unknown_is_honest(service):
    service.controller._diagnostic_reader = lambda: (_ for _ in ()).throw(FileNotFoundError())
    offline = service.controller.diagnostic()
    assert offline["connection"] == "offline"
    assert offline["control"] == {"paused": None, "stopped": None}
    service.controller._diagnostic_reader = lambda: (_ for _ in ()).throw(RuntimeError("PRIVATE_OS_ERROR"))
    unknown = service.controller.diagnostic()
    assert unknown["connection"] == "unknown" and "PRIVATE_OS_ERROR" not in json.dumps(unknown)


def test_client_reuses_service_and_open_is_no_model_no_business_start(service, monkeypatch):
    opened = []
    monkeypatch.setattr(module.webbrowser, "open", opened.append)
    client = SupportClient(service.workspace, root=service.paths.root)
    state = client.start()
    assert state["guide"]["state"] == "ready" and state["workspace"]["status"] == "paused"
    assert len(opened) == 1 and opened[0].startswith(service.server.url + "/#ticket=")
    assert service.controller.operator not in opened[0]
    assert FakeAdapter.instances == []
    runtime = json.loads(read_private(service.paths.file("runtime.json")))
    write_private(service.paths.file("runtime.json"), json.dumps({**runtime, "nonce": "wrong-but-long-enough"}))
    with pytest.raises(SupportError) as error:
        client.start(open_browser=False)
    assert error.value.code == "auth"


def test_controller_restart_preserves_selected_history_exact_identity(service):
    request(service, "/api/support/message", {"text": "first", "request_id": "human-request-001"})
    before = wait_idle(service)
    native_id = service.controller.store.binding()["native_id"]
    service.controller.close()
    replacement = SupportController(service.paths, adapter_factory=FakeAdapter, diagnostic_reader=lambda: service.business)
    try:
        after = replacement.open()
        assert after["messages"] == before["messages"]
        assert replacement.store.binding()["native_id"] == native_id
        assert len(FakeAdapter.instances) == 1
    finally:
        replacement.close()


def test_close_keeps_os_lease_for_live_worker(service, monkeypatch):
    worker = SimpleNamespace(is_alive=lambda: True, join=lambda **_kwargs: None)
    service.controller._threads.append(worker)
    with pytest.raises(SupportError) as error:
        service.controller.close()
    assert error.value.code == "busy"
    assert not service.controller._lease_released
    with pytest.raises(SupportError):
        with service.paths.lease():
            pytest.fail("A live worker lost ownership")
    service.controller._threads.remove(worker)
    service.controller.close()


def test_projection_bounds_provider_fields_and_hides_operational_content():
    state = {"paused": False, "auto": {}, "agents": [
        {"provider": "CLAUDE_PRIVATE", "state": "working", "label": "PRIVATE_LABEL",
         "channel_verified": True, "wake_channel_available": True, "attachment_status": "native_delivery_verified"},
        {"provider": "codex", "state": "idle"}], "jobs": [{"body": "PRIVATE_BODY"}]}
    projected = project_diagnostic(state)
    assert projected["apps"]["by_provider"] == {"claude": 0, "codex": 1, "antigravity": 0, "other": 1}
    assert projected["apps"]["working"] == 1
    assert "PRIVATE" not in json.dumps(projected)


def test_static_hardlink_to_private_fixture_refused(service, tmp_path, monkeypatch):
    import os
    root = tmp_path / "shipped-resource-fixture"
    folder = root / "web/assets/characters"
    folder.mkdir(parents=True)
    private = tmp_path / "private-file"
    private.write_text("NEVER_SERVE_THIS_PRIVATE_CONTENT", encoding="utf-8")
    os.link(private, folder / "sharebus-claude.svg")
    monkeypatch.setattr(module, "HERE", root)
    code, body, _ = request(service, "/assets/characters/sharebus-claude.svg", token=False)
    assert code == 403
    assert "NEVER_SERVE" not in json.dumps(body)


def test_actual_broker_schema_does_not_count_pending_or_unavailable_bridges(tmp_path):
    from daemon import BusHTTPServer
    from store import BusStore
    workspace = tmp_path / "synthetic-business-room"
    workspace.mkdir()
    store = BusStore(workspace)
    pending, _ = store.register("claude", "pending", [], "native_attached", native_session_id="fixture-id-one")
    store.update_agent(pending["id"], state="idle")
    verified, _ = store.register("codex", "verified", [], "native_attached", native_session_id="fixture-id-two")
    store.update_agent(verified["id"], state="idle", channel_verified=True)
    unavailable, _ = store.register("claude", "unavailable", [], "native_attached", native_session_id="fixture-id-three")
    store.update_agent(unavailable["id"], state="unavailable", channel_verified=True)
    fixture_server = SimpleNamespace(store=store, supervisor=SimpleNamespace(hosted_state=lambda: {"sessions": []}),
        native_enabled=True, discovery=[], discovery_state="ready")
    actual = BusHTTPServer.state(fixture_server)
    assert "stopping" not in actual
    projected = project_diagnostic(actual)
    assert projected["apps"]["registered"] == 3
    assert projected["apps"]["connected"] == 1
    assert projected["apps"]["liveness"] == "not_verified"
    assert projected["control"]["stopped"] is None


@pytest.fixture
def restart_client(tmp_path, monkeypatch):
    """Real private path/lease/signature checks; inert transport and child only."""
    workspace = tmp_path / "restart-room"
    workspace.mkdir()
    client = SupportClient(workspace, root=tmp_path / "help")
    client.paths.ensure()
    operator = "fixture-operator-" + uuid.uuid4().hex
    write_private(client.paths.file("operator.token"), operator)
    old = {"service": module.SERVICE, "key": client.paths.key, "nonce": uuid.uuid4().hex,
           "pid": 424242, "url": "http://127.0.0.1:29999"}
    new = {**old, "nonce": uuid.uuid4().hex, "pid": 434343, "url": "http://127.0.0.1:29998"}
    write_private(client.paths.file("runtime.json"), json.dumps(old))
    case = SimpleNamespace(client=client, old=old, new=new, operator=operator, clock=0,
                           calls=[], launches=[], failure=TimeoutError, on_health=None, on_launch=None)

    class Reply:
        code = 200
        def __init__(self, request, runtime, value, signed):
            self.body = json.dumps(value).encode()
            self.headers = Message()
            if signed:
                nonce = request.get_header("X-sharebus-nonce")
                self.headers["X-Sharebus-Request-Nonce"] = nonce
                self.headers["X-Sharebus-Response-Signature"] = response_signature(
                    token_key(operator), runtime, "operator", nonce, self.code, self.body)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def read(self, maximum):
            return self.body[:maximum]

    def reply(request, runtime, *, signed=True):
        value = {"ok": True, **runtime} if request.full_url.endswith("/health") else {"guide": {"state": "ready"}}
        return Reply(request, runtime, value, signed)

    def publish(runtime):
        write_private(client.paths.file("runtime.json"), json.dumps(runtime))

    def opened(request, timeout):
        case.calls.append((request.full_url, dict(request.header_items()), timeout))
        if request.full_url.endswith("/health"):
            if case.on_health:
                return case.on_health(request)
            if not case.launches:
                raise URLError(case.failure())
        return reply(request, case.new)

    def launch(command, **kwargs):
        case.launches.append((command, kwargs))
        if case.on_launch:
            case.on_launch()
        else:
            publish(case.new)
        return SimpleNamespace(poll=lambda: 1)

    def advance(seconds):
        case.clock += seconds

    def forbidden(*_args, **_kwargs):
        pytest.fail("Restart checks must not call a real child, native profile or business control")

    case.reply, case.publish = reply, publish
    client.opener = SimpleNamespace(open=opened)
    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(module.Client, "start", forbidden)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: case.clock, sleep=advance))
    yield case
    assert operator not in json.dumps(case.calls)


@pytest.mark.parametrize("failure", ["timeout", "reset", "refused", "echo", "missing"])
def test_restart_held_os_lease_refuses_candidate(restart_client, failure):
    case = restart_client
    expected = "auth"
    if failure == "echo":
        case.on_health = lambda request: case.reply(request, case.old, signed=False)
    elif failure == "missing":
        case.client.paths.file("runtime.json").unlink()
        expected = "connection"
    elif failure == "refused":
        case.failure = ConnectionRefusedError
        expected = "connection"
    elif failure == "reset":
        case.failure = ConnectionResetError
    with case.client.paths.lease():
        with pytest.raises(SupportError) as caught:
            case.client.start(open_browser=False)
    assert caught.value.code == expected
    assert case.launches == []
    assert not any(url.endswith("/api/support/open") for url, *_ in case.calls)


@pytest.mark.parametrize("failure", [TimeoutError, ConnectionResetError])
def test_restart_stale_transport_waits_for_authenticated_new_generation(restart_client, failure):
    case = restart_client
    case.failure = failure
    old_bytes = case.client.paths.file("runtime.json").read_bytes()
    token_bytes = case.client.paths.file("operator.token").read_bytes()
    case.on_launch = lambda: None
    health_calls = []
    def health(request):
        health_calls.append(request.full_url)
        if len(health_calls) < 4:
            assert case.client.paths.file("runtime.json").read_bytes() == old_bytes
            if len(health_calls) == 3:
                case.publish(case.new)
            raise URLError(failure())
        return case.reply(request, case.new)
    case.on_health = health
    assert case.client.start(open_browser=False)["guide"]["state"] == "ready"
    assert len(case.launches) == 1 and len(health_calls) >= 4
    assert case.client.paths.file("operator.token").read_bytes() == token_bytes
    assert all(0 < timeout <= 2 for url, _, timeout in case.calls if url.endswith("/health"))


def test_restart_missing_runtime_starts_one_candidate(restart_client):
    case = restart_client
    case.client.paths.file("runtime.json").unlink()
    assert case.client.start(open_browser=False)["guide"]["state"] == "ready"
    assert len(case.launches) == 1


def test_restart_missing_snapshot_does_not_hide_unobserved_auth(restart_client, monkeypatch):
    case = restart_client
    case.client.paths.file("runtime.json").unlink()
    original_read = module.read_private
    def denied_before_observation(path, **kwargs):
        if path.name == "runtime.json" and case.launches:
            raise PermissionError("private fixture access denied")
        return original_read(path, **kwargs)
    monkeypatch.setattr(module, "read_private", denied_before_observation)
    with pytest.raises(SupportError) as caught:
        case.client.start(open_browser=False)
    assert caught.value.code == "auth" and case.clock == 0
    assert len(case.launches) == 1
    assert not any(url.endswith("/api/support/open") for url, *_ in case.calls)


def test_restart_authenticated_old_generation_never_completes_recovery(restart_client):
    case = restart_client
    case.on_launch = lambda: None
    case.on_health = lambda request: (case.reply(request, case.old) if case.launches
                                     else (_ for _ in ()).throw(URLError(TimeoutError())))
    with pytest.raises(SupportError) as caught:
        case.client.start(open_browser=False)
    assert caught.value.code == "connection" and case.clock == 15
    assert len(case.launches) == 1
    assert not any(url.endswith("/api/support/open") for url, *_ in case.calls)


@pytest.mark.parametrize("replacement", ["malformed", "unsigned"])
def test_restart_changed_runtime_auth_fails_closed(restart_client, replacement):
    case = restart_client
    def publish_bad():
        if replacement == "malformed":
            write_private(case.client.paths.file("runtime.json"), "{invalid-new-generation")
        else:
            case.publish(case.new)
    case.on_launch = publish_bad
    case.on_health = lambda request: (case.reply(request, case.new, signed=False) if case.launches
                                     else (_ for _ in ()).throw(URLError(TimeoutError())))
    with pytest.raises(SupportError) as caught:
        case.client.start(open_browser=False)
    assert caught.value.code == "auth" and case.clock == 0
    assert len(case.launches) == 1
    assert not any(url.endswith("/api/support/open") for url, *_ in case.calls)


@pytest.mark.parametrize("replacement", ["workspace", "storage"])
def test_restart_revalidates_paths_after_releasing_probe(restart_client, replacement, monkeypatch):
    case = restart_client
    original_lease = case.client.paths.lease
    @contextmanager
    def changed_after_probe():
        with original_lease():
            yield
        path = case.client.paths.selection.path if replacement == "workspace" else case.client.paths.private
        path.rename(path.with_name(path.name + "-preserved"))
        path.mkdir()
    monkeypatch.setattr(case.client.paths, "lease", changed_after_probe)
    with pytest.raises(SupportError) as caught:
        case.client.start(open_browser=False)
    assert caught.value.code == replacement
    assert case.launches == []


def test_restart_concurrent_candidates_elect_one_owner_preserve_unknown_history(service, monkeypatch):
    request(service, "/api/support/message", {"text": "first", "request_id": "human-request-001"})
    for worker in service.controller._threads:
        worker.join(timeout=8)
        assert not worker.is_alive()
    FakeAdapter.behavior = "raise_after_input"
    request(service, "/api/support/message", {"text": "second", "request_id": "human-request-002"})
    for worker in service.controller._threads:
        worker.join(timeout=8)
        assert not worker.is_alive()
    before = service.controller.state()
    native_id = service.controller.store.binding()["native_id"]
    token_bytes = service.paths.file("operator.token").read_bytes()
    business = json.dumps(service.business, sort_keys=True)
    old = service.server.runtime()
    service.server.shutdown()
    service.server.server_close()
    service.controller.close()
    rendezvous = threading.Barrier(2)
    first_probe_released = threading.Event()
    launches, owners, candidates, failures, results = [], [], [], [], []

    def launch(command, **kwargs):
        assert "--serve" in command and kwargs["cwd"] == str(service.paths.directory)
        process = SimpleNamespace(code=None, poll=lambda: process.code)
        launches.append(process)
        first_probe_released.set()
        rendezvous.wait(timeout=5)
        def candidate():
            controller = None
            try:
                paths = SupportPaths(service.workspace, root=service.paths.root)
                controller = SupportController(paths, adapter_factory=FakeAdapter,
                                               diagnostic_reader=lambda: service.business)
                server = SupportHTTPServer(controller)
                owners.append((controller, server))
                write_private(paths.file("runtime.json"), json.dumps(server.runtime()))
                server.serve_forever(poll_interval=0.05)
                process.code = 0
            except SupportError as error:
                if error.code != "busy":
                    failures.append(error.code)
                process.code = 1
            finally:
                if controller is not None:
                    controller.close()
        thread = threading.Thread(target=candidate, daemon=True)
        candidates.append(thread)
        thread.start()
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    def start():
        client = SupportClient(service.workspace, root=service.paths.root)
        real = client.opener
        def opened(request, timeout):
            if request.full_url.startswith(old["url"] + "/"):
                raise URLError(TimeoutError())
            return real.open(request, timeout=timeout)
        client.opener = SimpleNamespace(open=opened)
        try:
            results.append(client.start(open_browser=False))
        except Exception as error:
            failures.append(type(error).__name__)
    clients = [threading.Thread(target=start, daemon=True) for _ in range(2)]
    try:
        clients[0].start()
        assert first_probe_released.wait(5)
        clients[1].start()
        for thread in clients:
            thread.join(timeout=20)
        assert not any(thread.is_alive() for thread in clients)
        assert not failures and len(results) == 2
        assert len(launches) == 2 and len(owners) == 1
        assert sum(process.poll() == 1 for process in launches) == 1
        controller, server = owners[0]
        assert server.nonce != old["nonce"]
        assert all(result["guide"]["state"] == "unknown" and not result["guide"]["can_send"]
                   and result["messages"] == before["messages"] for result in results)
        assert controller.store.binding()["native_id"] == native_id
        assert service.paths.file("operator.token").read_bytes() == token_bytes
        assert len(FakeAdapter.instances) == 2
        assert json.dumps(service.business, sort_keys=True) == business
        with pytest.raises(SupportError) as caught:
            with service.paths.lease():
                pytest.fail("The elected owner must retain the real OS lease")
        assert caught.value.code == "busy"
    finally:
        for _, server in owners:
            server.shutdown()
            server.server_close()
        for thread in candidates:
            thread.join(timeout=7)
        assert not any(thread.is_alive() for thread in candidates)
