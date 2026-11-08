"""Automatic local service fixtures; no real provider, native client or model."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import time

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError, BusStore, WorkspaceRetired
import hosted as hosted_module
from server import Runtime, Server
import daemon
import sidecar_registry as registry


def wait_for(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Finite local service fixture did not reach its expected event")


@pytest.fixture
def local(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    starts = []

    def forbidden_adapter(*args, **kwargs):
        starts.append(args)
        raise AssertionError("Blank startup attempted a provider process")

    runtime = Runtime(workspace, discovery=False, native_ipc=False,
                      hosted_factory=forbidden_adapter, hosted_discovery=[])
    target = tmp_path / "registry"
    monkeypatch.setattr(registry, "REGISTRY", target)
    actual_register = registry.register_workspace
    registrations = []

    def register(root, *, runtime, workspace_guard):
        registrations.append(runtime)
        return actual_register(root, runtime=runtime, registry_path=target, workspace_guard=workspace_guard)

    monkeypatch.setattr(hosted_module, "register_workspace", register)
    value = SimpleNamespace(runtime=runtime, hosted=runtime.hosted, workspace=workspace, registry=target,
                            registrations=registrations, starts=starts, extra=[], threads=[], errors=[])
    before = set(threading.enumerate())
    yield value
    runtime.begin_closing()
    runtime.hosted.close()
    for server, thread in value.extra:
        if thread.is_alive():
            server.shutdown()
        server.close()
        thread.join(timeout=3)
        assert not thread.is_alive()
    for thread in value.threads:
        thread.join(timeout=4)
        assert not thread.is_alive()
    runtime.stop_peer_reconciler()
    runtime.device_discovery.stop()
    assert runtime._active_work == 0
    wait_for(lambda: not [thread for thread in threading.enumerate() if thread not in before], timeout=4)
    assert value.starts == []


def spawn(local, action):
    result = []

    def run():
        try:
            result.append(action())
        except BaseException as error:
            local.errors.append(error)
    thread = threading.Thread(target=run, daemon=True)
    local.threads.append(thread)
    thread.start()
    return thread, result


def external_bus(local, callback=None):
    store = BusStore(local.workspace)
    bus = daemon.BusHTTPServer(local.workspace, store=store, hosted_only=True, discovery=[], enable_native=False)
    bus.avalokita_peer = callback
    daemon._write_daemon_metadata(store, "runtime.json", bus.runtime())
    thread = threading.Thread(target=bus.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    assert bus.serving.wait(2)
    local.extra.append((bus, thread))
    return bus


@pytest.mark.parametrize("observation", ["exact", "read_failed", "different_providers", "unready", "closing", "foreign_service", "foreign_after_read"])
def test_default_discovery_reconciles_only_exact_owned_configuration_after_uncertain_response(local, monkeypatch, observation):
    import discovery
    resolved, writes, public = [], [], []
    local.hosted.discovery = None

    def executable(provider):
        resolved.append(provider)
        return str(local.workspace / (provider + ".exe"))

    monkeypatch.setattr(discovery, "resolve_executable", executable)
    request = local.runtime.bridge.request

    def uncertain(path, payload=None, **kwargs):
        if path == "/api/hosted" and payload is not None:
            writes.append(payload)
            # Apply the handler's real backend operation before losing its
            # response, independently of the loopback transport's own timing.
            value = local.hosted.server.supervisor.configure_hosted(payload["providers"])
            public.append({"state": value["state"], "reason": value.get("reason"), "sessions": [
                {key: item[key] for key in ("provider", "enabled", "blocked")} for item in value["sessions"]]})
            if observation == "different_providers":
                local.hosted.server.supervisor.configure_hosted(["codex"])
            elif observation == "unready":
                local.hosted._ready.clear()
            elif observation == "closing":
                local.hosted.begin_closing()
            elif observation == "foreign_service":
                monkeypatch.setattr(hosted_module, "verified_runtime", lambda *args, **kwargs:
                    {**local.hosted._runtime, "nonce": "another-owned-generation"})
            raise BridgeError("Sharebus is unavailable; uncertain writes are never automatically retried", 409)
        if path == "/api/hosted" and writes and observation == "read_failed":
            raise BridgeError("Sharebus is unavailable; uncertain writes are never automatically retried", 409)
        value = request(path, payload, **kwargs)
        if path == "/api/hosted" and writes and observation == "foreign_after_read":
            monkeypatch.setattr(hosted_module, "verified_runtime", lambda *args, **kwargs:
                {**local.hosted._runtime, "nonce": "changed-after-read"})
        return value

    monkeypatch.setattr(local.runtime.bridge, "request", uncertain)
    value = local.runtime.configure_hosted({"providers": ["codex", "claude"]})
    assert set(resolved) == {"codex", "claude", "antigravity"}
    assert len(writes) == 1 and public[0]["state"] == "ready", public
    assert local.hosted.providers() == ["codex", "claude"]
    assert value["state"] == ("ready" if observation == "exact" else "blocked"), {"remote": public, "returned_state": value["state"]}
    if observation == "foreign_after_read":
        assert value["sessions"] == [] and value["approvals"] == []
    assert local.hosted.server.store.jobs() == [] and local.starts == []


def test_normal_serving_loop_automatically_registers_idle_workspace_and_cache_reads_do_no_startup(local, monkeypatch):
    server = Server(local.workspace, runtime=local.runtime)
    assert local.hosted.server is None and local.hosted.service_snapshot()["state"] == "idle"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: local.hosted.service_snapshot()["state"] == "ready")
        bus = local.hosted.server
        assert local.hosted._ready.is_set() and bus.serving.is_set() and local.hosted.thread.is_alive()
        assert registry.verified_runtime(local.workspace)["nonce"] == bus.nonce
        assert len(local.registrations) == 1 and local.registrations[0] is local.hosted._runtime
        record = json.loads(next(local.registry.glob("*.json")).read_text(encoding="utf-8"))
        assert record["runtime_nonce"] == bus.nonce
        with monkeypatch.context() as scoped:
            def forbidden(*args, **kwargs):
                raise AssertionError("Observation attempted service initialization or HTTP")
            scoped.setattr(local.hosted, "_ensure_bus", forbidden)
            scoped.setattr(hosted_module, "verified_runtime", forbidden)
            scoped.setattr(hosted_module, "register_workspace", forbidden)
            scoped.setattr(local.runtime.bridge, "observe", forbidden)
            assert local.runtime.summary()["service"]["state"] == "ready"
            assert local.hosted.service_snapshot() == {"state": "ready", "reason_code": None, "retry_in": 0, "retry_count": 0}
        assert local.runtime.summary()["counts"]["goals"] == 0
        assert bus.store.jobs() == [] and bus.store.agents() == []
        assert local.starts == []
    finally:
        server.server_close()
        thread.join(timeout=4)
    assert not thread.is_alive() and not local.hosted._ready.is_set()
    assert not local.hosted._service_thread.is_alive()


@pytest.mark.parametrize("reason", ["denied", "invalid_metadata", "generation_mismatch", "uncertain"])
def test_ineligible_reason_preserves_metadata_and_never_constructs_takes_lease_or_closes_external(local, monkeypatch, reason):
    path = local.runtime.bridge.private / "runtime.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("preserved-external-metadata", encoding="utf-8")
    previous = path.read_bytes()
    counts = {"construct": 0, "metadata_write": 0, "lease": 0, "external_close": 0}

    def refuse(*args, **kwargs):
        raise registry.RuntimeVerificationError("fixture-private-value", reason)

    def forbidden(name):
        def invoke(*args, **kwargs):
            counts[name] += 1
            raise AssertionError("Ineligible verification reached ownership mutation")
        return invoke

    monkeypatch.setattr(hosted_module, "verified_runtime", refuse)
    monkeypatch.setattr(hosted_module, "BusStore", forbidden("construct"))
    monkeypatch.setattr(daemon, "BusHTTPServer", forbidden("construct"))
    monkeypatch.setattr(daemon, "workspace_guard", forbidden("lease"))
    monkeypatch.setattr(daemon, "_write_daemon_metadata", forbidden("metadata_write"))
    monkeypatch.setattr(daemon.BusHTTPServer if isinstance(daemon.BusHTTPServer, type) else SimpleNamespace(),
                        "close", forbidden("external_close"), raising=False)
    with pytest.raises(registry.RuntimeVerificationError) as caught:
        local.hosted._service_once()
    assert caught.value.reason_code == reason
    assert counts == {"construct": 0, "metadata_write": 0, "lease": 0, "external_close": 0}
    assert path.read_bytes() == previous and local.hosted.server is None and local.registrations == []


@pytest.mark.parametrize("reason", ["runtime_missing", "connect_refused"])
def test_only_fresh_missing_or_unchanged_refusal_can_construct_under_lease(local, monkeypatch, reason):
    constructors, writes, closes = [], [], []
    original_bus, original_write, verify = daemon.BusHTTPServer, daemon._write_daemon_metadata, hosted_module.verified_runtime
    stale = {"workspace": str(local.workspace.resolve()), "workspace_id": BusStore(local.workspace).workspace_id,
             "url": "http://127.0.0.1:12345", "pid": 123, "nonce": "fixture-refused-original"}
    if reason == "connect_refused":
        (local.runtime.bridge.private / "runtime.json").write_text(json.dumps(stale), encoding="utf-8")
    probes = []

    def preflight(root, **kwargs):
        probes.append(reason)
        if len(probes) <= 2:
            raise registry.RuntimeVerificationError("fixture", reason, runtime=stale if reason == "connect_refused" else None)
        return verify(root, **kwargs)

    def construct(*args, **kwargs):
        constructors.append(kwargs)
        return original_bus(*args, **kwargs)

    def write(*args):
        writes.append(args[1])
        return original_write(*args)

    monkeypatch.setattr(hosted_module, "verified_runtime", preflight)
    monkeypatch.setattr(daemon, "_legacy_owner_alive", lambda store: False)
    monkeypatch.setattr(daemon, "BusHTTPServer", construct)
    monkeypatch.setattr(daemon, "_write_daemon_metadata", write)
    assert local.hosted._service_once() == (False, True)
    assert len(constructors) == 1 and writes == ["runtime.json"] and closes == []
    assert local.hosted.guard is not None and local.hosted._ready.is_set()
    assert local.registrations == [local.hosted._runtime]


@pytest.mark.parametrize("change", ["nonce", "workspace_id", "missing_to_refused", "legacy_alive"])
def test_lease_fresh_check_and_store_workspace_identity_block_changed_or_legacy_owner(local, monkeypatch, change):
    initial = {"workspace": str(local.workspace.resolve()), "workspace_id": BusStore(local.workspace).workspace_id,
               "url": "http://127.0.0.1:12345", "pid": 123, "nonce": "fixture-original-refusal"}
    if change == "workspace_id":
        initial["workspace_id"] = "not-the-retained-store"
    count = []

    def verify(*args, **kwargs):
        current = dict(initial)
        count.append(1)
        if change == "nonce" and len(count) == 2:
            current["nonce"] = "fixture-changed-refusal"
        code = "runtime_missing" if change == "missing_to_refused" and len(count) == 1 else "connect_refused"
        raise registry.RuntimeVerificationError("fixture", code, runtime=current)

    monkeypatch.setattr(hosted_module, "verified_runtime", verify)
    monkeypatch.setattr(daemon, "_legacy_owner_alive", lambda store: change == "legacy_alive")
    constructor = []
    monkeypatch.setattr(daemon, "BusHTTPServer", lambda *args, **kwargs: constructor.append(1))
    with pytest.raises((registry.RuntimeVerificationError, BridgeError)):
        local.hosted._service_once()
    assert len(count) == 2 and constructor == [] and local.registrations == []


def test_verified_external_owner_is_preserved_without_lease_callback_registration_or_close(local, monkeypatch):
    callback = object()
    external = external_bus(local, callback)
    path = local.runtime.bridge.private / "runtime.json"
    previous = path.read_bytes()
    record = registry.register_workspace(local.workspace, runtime=external.runtime(), registry_path=local.registry)
    previous_record = record.read_bytes()
    counts = {"construct": 0, "lease": 0, "metadata_write": 0, "external_close": 0}

    def forbidden(name):
        def invoke(*args, **kwargs):
            counts[name] += 1
            raise AssertionError("A verified external owner was changed")
        return invoke

    with monkeypatch.context() as scoped:
        scoped.setattr(daemon, "BusHTTPServer", forbidden("construct"))
        scoped.setattr(daemon, "workspace_guard", forbidden("lease"))
        scoped.setattr(daemon, "_write_daemon_metadata", forbidden("metadata_write"))
        scoped.setattr(external, "close", forbidden("external_close"))
        assert local.hosted._service_once() == (False, False)
        local.hosted.close()
    assert counts == {"construct": 0, "lease": 0, "metadata_write": 0, "external_close": 0}
    assert external.avalokita_peer is callback and external.serving.is_set()
    assert path.read_bytes() == previous and record.read_bytes() == previous_record
    assert local.hosted.server is None and local.registrations == []


def test_concurrent_configure_waits_outside_rlock_and_pre_ready_intake_is_refused(local, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original_verify, original_request = hosted_module.verified_runtime, local.runtime.bridge.request
    constructs, snapshots = [], []
    real_bus = daemon.BusHTTPServer

    def construct(*args, **kwargs):
        value = real_bus(*args, **kwargs)
        constructs.append(value)
        return value

    def verify(root, **kwargs):
        if local.hosted.server is not None and not entered.is_set():
            entered.set()
            assert release.wait(4)
        return original_verify(root, **kwargs)

    def request(*args, **kwargs):
        snapshots.append(local.hosted._lock._is_owned())
        return original_request(*args, **kwargs)

    monkeypatch.setattr(daemon, "BusHTTPServer", construct)
    monkeypatch.setattr(hosted_module, "verified_runtime", verify)
    monkeypatch.setattr(local.runtime.bridge, "request", request)
    worker, _ = spawn(local, local.hosted._service_once)
    assert entered.wait(3)
    bus = local.hosted.server
    assert bus.acquire_work() is None and not local.hosted._ready.is_set()
    with pytest.raises(BridgeError) as refused:
        original_request("/api/hosted", {"providers": []})
    assert refused.value.status == 409
    config, result = spawn(local, lambda: local.runtime.configure_hosted({"providers": []}))
    # Even empty configuration sends HTTP outside the old construction RLock.
    config.join(timeout=2)
    assert not config.is_alive()
    config2, result2 = spawn(local, lambda: local.runtime.configure_hosted({"providers": ["codex"]}))
    assert local.runtime.lock.acquire(blocking=False)
    local.runtime.lock.release()
    assert config2.is_alive() and len(constructs) == 1
    release.set()
    worker.join(timeout=4)
    config2.join(timeout=4)
    assert not worker.is_alive() and not config2.is_alive() and local.errors == []
    assert result and result2 and not any(snapshots)
    assert len(constructs) == 1 and bus.acquire_work is not None and local.hosted._ready.is_set()
    permit = bus.acquire_work()
    assert callable(permit)
    permit()


def test_registration_failure_retries_only_exact_owned_runtime_without_reconstruction(local, monkeypatch):
    attempts = []
    register = hosted_module.register_workspace

    def transient(root, **kwargs):
        attempts.append(kwargs["runtime"])
        kwargs["workspace_guard"]()
        if len(attempts) <= 2:
            raise PermissionError("fixture-private-value")
        return register(root, **kwargs)

    monkeypatch.setattr(hosted_module, "register_workspace", transient)
    assert local.hosted._service_once() == (True, True)
    server, thread, guard, nonce = local.hosted.server, local.hosted.thread, local.hosted.guard, local.hosted.server.nonce
    assert local.hosted.service_snapshot()["state"] == "ready_degraded" and local.hosted._ready.is_set()
    assert local.hosted._service_once() == (True, False)
    assert local.hosted._service_once() == (False, True)
    assert local.hosted._service_once() == (False, False)
    assert all(value is local.hosted._runtime for value in attempts) and len(attempts) == 3
    assert (local.hosted.server, local.hosted.thread, local.hosted.guard, local.hosted.server.nonce) == (server, thread, guard, nonce)
    assert local.hosted.service_snapshot()["state"] == "ready"


def test_persistent_registry_failure_uses_one_generation_and_backoff_reaches_thirty(local, monkeypatch):
    clock, waits, attempts = [0.0], [], []

    def failing(root, **kwargs):
        attempts.append(kwargs["runtime"])
        raise registry.RuntimeVerificationError("fixture-private-value", "denied")

    def waiting(seconds):
        waits.append(seconds)
        clock[0] += seconds
        if len(waits) == 7:
            local.hosted.begin_closing()
            return True
        return False

    monkeypatch.setattr(hosted_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(hosted_module, "register_workspace", failing)
    monkeypatch.setattr(local.hosted._closing, "wait", waiting)
    local.hosted._service_loop()
    assert waits == [1, 2, 4, 8, 16, 30, 30]
    assert len(attempts) == 7 and all(value is attempts[0] for value in attempts)
    assert not local.hosted._ready.is_set() and local.hosted.service_snapshot()["state"] == "closing"
    assert "fixture-private-value" not in json.dumps(local.hosted.service_snapshot())


def test_transient_constructor_failure_is_retryable_and_close_during_construction_cannot_resurrect(local, monkeypatch):
    real_bus = daemon.BusHTTPServer
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def construct(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("fixture-transient-construction")
        bus = real_bus(*args, **kwargs)
        entered.set()
        assert release.wait(4)
        return bus

    monkeypatch.setattr(daemon, "BusHTTPServer", construct)
    with pytest.raises(OSError):
        local.hosted._service_once()
    local.hosted.start_service()
    assert entered.wait(3)
    assert local.runtime._active_work == 1
    local.runtime.begin_closing()
    assert local.hosted._closing.is_set() and not local.hosted._ready.is_set()
    release.set()
    local.hosted.stop_service()
    assert not local.hosted._service_thread.is_alive() and local.runtime._active_work == 0
    local.hosted.start_service()
    assert len(attempts) == 2 and local.hosted.server is None and local.registrations == []


def test_owned_a_exit_after_registry_failure_preserves_external_b_metadata_registry_and_handles(local, monkeypatch):
    register = hosted_module.register_workspace
    monkeypatch.setattr(hosted_module, "register_workspace", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError()))
    assert local.hosted._service_once() == (True, True)
    first, first_thread, first_ready = local.hosted.server, local.hosted.thread, local.hosted._ready
    first.shutdown()
    first_thread.join(timeout=3)
    assert not first_thread.is_alive() and not first_ready.is_set()
    second = external_bus(local, object())
    path = local.runtime.bridge.private / "runtime.json"
    record = registry.register_workspace(local.workspace, runtime=second.runtime(), registry_path=local.registry)
    metadata, registered, callback = path.read_bytes(), record.read_bytes(), second.avalokita_peer
    closes = []
    with monkeypatch.context() as scoped:
        scoped.setattr(second, "close", lambda: closes.append("external-b"))
        scoped.setattr(hosted_module, "register_workspace", register)
        assert local.hosted._service_once() == (False, False)
        local.hosted.close()
    assert closes == [] and local.hosted.server is None
    assert first._closed.is_set() and second.serving.is_set()
    assert path.read_bytes() == metadata and record.read_bytes() == registered and second.avalokita_peer is callback


def test_old_serve_finally_cannot_lower_replacement_generation_readiness(local, monkeypatch):
    targets = []
    real_thread = threading.Thread

    def thread(*args, **kwargs):
        if kwargs.get("name") == "avalokita-owned-bus":
            targets.append(kwargs["target"])
        return real_thread(*args, **kwargs)

    monkeypatch.setattr(hosted_module.threading, "Thread", thread)
    assert local.hosted._service_once() == (False, True)
    first, first_thread, first_ready = local.hosted.server, local.hosted.thread, local.hosted._ready
    first.shutdown()
    first_thread.join(timeout=3)
    assert local.hosted._service_once() == (False, True)
    second, second_ready = local.hosted.server, local.hosted._ready
    assert first is not second and second_ready is not first_ready and second_ready.is_set()
    monkeypatch.setattr(first, "serve_forever", lambda **kwargs: None)
    targets[0]()  # A delayed finally still closes over A's Event.
    assert second_ready.is_set() and not first_ready.is_set() and second.serving.is_set()


def test_retirement_is_terminal_and_does_not_start_or_retry_after_guard_refusal(local, monkeypatch):
    refusal = WorkspaceRetired()
    calls = []

    def retired():
        calls.append(1)
        raise refusal

    monkeypatch.setattr(local.hosted, "_workspace_guard", retired)
    with pytest.raises(WorkspaceRetired) as caught:
        local.hosted._service_once()
    assert caught.value is refusal and calls == [1]
    assert local.hosted.phase == "retired" and local.hosted._closing.is_set()
    local.hosted.start_service()
    assert local.hosted._service_thread is None and local.hosted.server is None and local.registrations == []


@pytest.mark.parametrize("raw", ["not-json", "null", "{}", "true", '"codex"', '["codex","codex"]', '["gemini"]', '[[]]'])
def test_first_prompt_preserves_invalid_existing_provider_setting_and_refuses_dispatch(local, monkeypatch, raw):
    with local.runtime.bridge._db() as db:
        db.execute("INSERT INTO settings VALUES ('hosted_providers',?)", (raw,))
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid configuration attempted discovery or dispatch")
    monkeypatch.setattr(local.hosted, "_provider_candidates", forbidden)
    monkeypatch.setattr(local.runtime.bridge, "dispatch", forbidden)
    with pytest.raises(BridgeError):
        local.runtime.prompt({"text": "Selected first input", "idempotency_key": "invalid-config"})
    with local.runtime.bridge._db() as db:
        assert db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone()[0] == raw
    with local.runtime.store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0


def test_oversized_first_prompt_key_has_no_onboarding_side_effects(local, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid key attempted onboarding or dispatch")
    monkeypatch.setattr(local.hosted, "before_prompt", forbidden)
    monkeypatch.setattr(local.runtime.bridge, "dispatch", forbidden)
    with pytest.raises(BridgeError):
        local.runtime.prompt({"text": "Selected first input", "idempotency_key": "k" * 129})
    assert local.hosted.server is None
    with local.runtime.bridge._db() as db:
        assert db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone() is None
    with local.runtime.store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0


@pytest.mark.parametrize("providers", [[], ["claude"]])
def test_first_prompt_preserves_explicit_off_or_custom_provider_list(local, monkeypatch, providers):
    raw = json.dumps(providers, indent=2)
    with local.runtime.bridge._db() as db:
        db.execute("INSERT INTO settings VALUES ('hosted_providers',?)", (raw,))
    local.hosted.discovery = [{"provider": provider, "installed": True, "managed_supported": True}
                              for provider in ("codex", "claude", "antigravity")]
    result = local.hosted.before_prompt()
    with local.runtime.bridge._db() as db:
        assert db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone()[0] == raw
    assert local.hosted.providers() == providers
    if providers:
        assert result["state"] == "ready" and result["providers"] == providers
    else:
        assert result is None and local.hosted.server is None


@pytest.mark.parametrize("control", ["local_pause", "bus_pause", "suspend", "stopped"])
def test_first_prompt_does_not_initialize_through_existing_control(local, control):
    local.hosted.discovery = [{"provider": "claude", "installed": True, "managed_supported": True}]
    if control == "local_pause":
        with local.runtime.bridge._db() as db:
            db.execute("UPDATE settings SET value='true' WHERE key='paused'")
    else:
        owned = local.hosted._ensure_bus()
        if control == "bus_pause":
            owned[0].store.set_paused(True)
        elif control == "suspend":
            with owned[0].store.connection() as db:
                db.execute("UPDATE config SET value='true' WHERE key='auto_suspended'")
        else:
            actor, _ = owned[0].store.register("claude", "Stopped existing session", [], "native_attached", native_session_id="selected-old")
            owned[0].store.update_agent(actor["id"], state="stopped")
    if control == "stopped":
        with pytest.raises(BridgeError):
            local.hosted.before_prompt()
    else:
        assert local.hosted.before_prompt() is None
    with local.runtime.bridge._db() as db:
        assert db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone() is None


def test_first_prompt_selects_only_current_supported_executable_candidates(local, monkeypatch):
    import discovery
    local.hosted.discovery = None
    suffixes = {"codex": ".exe", "claude": ".cmd", "antigravity": ".bat"}
    monkeypatch.setattr(discovery, "resolve_executable", lambda provider: str(local.workspace / (provider + suffixes[provider])))
    selected = local.hosted.before_prompt()
    assert local.hosted.providers() == (["codex"] if os.name == "nt" else ["codex", "claude", "antigravity"])
    assert selected["state"] == "ready" and selected["providers"] == local.hosted.providers()


def test_first_prompt_with_no_usable_candidate_saves_nothing_and_does_not_dispatch(local, monkeypatch):
    local.hosted.discovery = [{"provider": "codex", "installed": True, "managed_supported": False},
                              {"provider": "claude", "installed": False, "managed_supported": True},
                              {"provider": "gemini", "installed": True, "managed_supported": True}]
    def forbidden(*args, **kwargs):
        raise AssertionError("Unavailable onboarding attempted a service or dispatch")
    monkeypatch.setattr(local.hosted, "_ensure_bus", forbidden)
    monkeypatch.setattr(local.runtime.bridge, "dispatch", forbidden)
    with pytest.raises(BridgeError):
        local.runtime.prompt({"text": "Selected first input", "idempotency_key": "no-candidates"})
    with local.runtime.bridge._db() as db:
        assert db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone() is None


def test_first_prompt_preserves_foreign_service_without_saving_defaults(local):
    external = external_bus(local, object())
    local.hosted.discovery = [{"provider": "codex", "installed": True, "managed_supported": True}]
    with pytest.raises(BridgeError):
        local.hosted.before_prompt()
    assert local.hosted.server is None and external.serving.is_set()
    assert external.store.agents() == []
    with local.runtime.bridge._db() as db:
        assert db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone() is None


@pytest.mark.parametrize("boundary", ["discovery", "verified_owner", "local_apply"])
def test_first_prompt_cannot_overwrite_concurrent_explicit_off(local, monkeypatch, boundary):
    local.hosted.discovery = [{"provider": "codex", "installed": True, "managed_supported": True}]
    entered, release = threading.Event(), threading.Event()
    original = local.hosted._provider_candidates if boundary == "discovery" else local.hosted._verify_owned
    if boundary == "local_apply":
        original = local.hosted._ensure_bus()[0].supervisor.configure_hosted
    def held(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(4)
        return original(*args, **kwargs)
    if boundary == "local_apply":
        monkeypatch.setattr(local.hosted.server.supervisor, "configure_hosted", held)
    else:
        monkeypatch.setattr(local.hosted, "_provider_candidates" if boundary == "discovery" else "_verify_owned", held)
    worker, result = spawn(local, local.hosted.before_prompt)
    assert entered.wait(3)
    off, disabled = spawn(local, lambda: local.runtime.configure_hosted({"providers": []}))
    try:
        if boundary != "local_apply":
            off.join(timeout=2)
            assert not off.is_alive()
        assert local.runtime.lock.acquire(blocking=False)
        local.runtime.lock.release()
    finally:
        release.set()
    worker.join(4)
    off.join(4)
    assert not worker.is_alive() and not off.is_alive() and local.errors == []
    assert local.hosted.providers() == [] and disabled[0]["enabled"] is False
    if local.hosted.server is not None:
        assert local.hosted.server.supervisor.hosted_state()["providers"] == []
        assert local.hosted.server.store.jobs() == []


def test_blocked_first_prompt_configuration_prevents_goal_and_dispatch(local, monkeypatch):
    local.hosted.discovery = [{"provider": "codex", "installed": True, "managed_supported": True}]
    supervisor = local.hosted._ensure_bus()[0].supervisor
    monkeypatch.setattr(supervisor, "configure_hosted", lambda providers: {"state": "blocked", "providers": providers})
    def forbidden(*args, **kwargs):
        raise AssertionError("Blocked onboarding continued to dispatch")
    monkeypatch.setattr(local.runtime.bridge, "dispatch", forbidden)
    with pytest.raises(BridgeError):
        local.runtime.prompt({"text": "Selected first input", "idempotency_key": "blocked-defaults"})
    with local.runtime.store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 0


@pytest.mark.parametrize("recovery", [False, True])
def test_reply_and_idempotent_recovery_do_not_initialize_missing_provider_setting(local, monkeypatch, recovery):
    payload = {"text": "Selected saved input", "idempotency_key": "saved-input"}
    if recovery:
        local.runtime.store.new_goal(payload["text"], idempotency_key=payload["idempotency_key"])
    else:
        payload["reply_to"] = "selected-question"
    def forbidden(*args, **kwargs):
        raise AssertionError("Reply or recovery attempted provider initialization")
    monkeypatch.setattr(local.hosted, "before_prompt", forbidden)
    monkeypatch.setattr(local.runtime.bridge, "dispatch", lambda *args, **kwargs: {"state": "waiting"})
    local.runtime.prompt(payload)
    with local.runtime.bridge._db() as db:
        assert db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone() is None
