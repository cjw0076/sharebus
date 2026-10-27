"""Native-hook wire, ownership, cancellation and model-free idle contract checks."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_watch
from mcp_server import BusError


@pytest.fixture
def room(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_watch, "_PROCESS_STARTED", auto_watch.time.monotonic())
    monkeypatch.delenv("SHAREBUS_MANAGED", raising=False)
    monkeypatch.delenv("SHAREBUS_AGENT_ID", raising=False)
    monkeypatch.delenv("SHAREBUS_AGENT_TOKEN_FILE", raising=False)
    root = tmp_path.resolve()
    private = root / ".sharebus/private/auto/contexts"
    private.mkdir(parents=True)
    (root / ".sharebus/auto.json").write_text(json.dumps({"version": 1, "workspace": str(root),
        "workspace_id": str(uuid.uuid4()), "enabled": True, "suspended": False, "updated_at": 10}), encoding="utf-8")
    actor, native = str(uuid.uuid4()), str(uuid.uuid4())
    token_file = root / ".sharebus/private/credentials" / (actor + ".token")
    token_file.parent.mkdir()
    token_file.write_text("test-private-token-never-in-nudge", encoding="utf-8")
    context_file = private / "native-context.json"
    context_file.write_text("{}", encoding="utf-8")
    binding = {"workspace": str(root), "workspace_id": str(uuid.uuid4()), "provider": "codex",
        "native_session_id": native, "agent_id": actor, "token_file": str(token_file),
        "context_file": str(context_file), "hook_source": "codex:Stop"}
    fixture = SimpleNamespace(root=root, actor=actor, native=native, binding=binding, time=0.0,
        bootstraps=[], connects=[], observations=[], requests=[], response=None, responses=[],
        on_pending=None, factory_kwargs=[], pending_count=0, request_timeouts=[])

    def payload(provider="codex", **extra):
        return {"session_id": native, "cwd": str(root), "hook_event_name": "Stop", "turn_id": "native-turn-1",
                "stop_hook_active": False, "last_assistant_message": "PRIVATE_NATIVE_BODY", **extra}

    def pending(**extra):
        return {"agent": {"id": actor, "provider": binding["provider"], "native_session_id": native,
                          "role": "CEO", "mode": "external_attached", "state": "idle", "active_root": None},
                "paused": False, "auto": {"enabled": True, "suspended": False, "active": True, "updated_at": 10},
                "control_epoch": 10, "wake_allowed": True, "queued": [], **extra}

    def job(**extra):
        return {"id": str(uuid.uuid4()), "event_id": str(uuid.uuid4()), "root_id": str(uuid.uuid4()),
                "state": "queued", "budget_remaining": 2, "created_at": 1, "deadline": 2000,
                "body": "PRIVATE_PEER_BODY", **extra}

    def bootstrap(provider, value, **kwargs):
        fixture.bootstraps.append((provider, value["session_id"], kwargs))
        return context_file

    def connect(context, provider, workspace, **kwargs):
        fixture.connects.append((context, provider, workspace, kwargs))
        return copy.deepcopy(binding)

    class Client:
        def __init__(self, *args, **kwargs):
            fixture.factory_kwargs.append(kwargs)
            self.request_timeout = kwargs["request_timeout"]

        def request(self, workspace, path, token, body=None):
            fixture.requests.append(path)
            fixture.request_timeouts.append(self.request_timeout)
            assert workspace == root and token == "test-private-token-never-in-nudge"
            if path == "/api/auto/observe":
                fixture.observations.append(body)
                return {}
            assert path == "/api/agent/pending" and body is None
            fixture.pending_count += 1
            if fixture.on_pending:
                fixture.on_pending(fixture.pending_count)
            if fixture.responses:
                return copy.deepcopy(fixture.responses.pop(0))
            return copy.deepcopy(fixture.response or pending())

    def sleep(seconds):
        fixture.time += seconds

    def run(provider="codex", value=None, **kwargs):
        return auto_watch.watch(provider, value or payload(provider), root, timeout=kwargs.pop("timeout", 1),
            poll_interval=0.1, clock=lambda: fixture.time, wall_clock=lambda: 1000,
            sleep=sleep, bootstrap=bootstrap, connect=connect, client_factory=Client, **kwargs)

    fixture.payload, fixture.pending, fixture.job, fixture.run = payload, pending, job, run
    fixture.bootstrap, fixture.connect, fixture.Client, fixture.sleep = bootstrap, connect, Client, sleep
    return fixture


def test_idle_polls_without_models_native_processes_claims_or_bodies(room, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Idle watcher must not launch a process/model.")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = room.run()
    assert result["state"] == "waiting" and result["poll_count"] >= 10
    assert result["model_requests"] == 0 and result["jobs"] == [] and "reason" not in result
    assert set(room.requests) == {"/api/agent/pending", "/api/auto/observe"}
    assert room.factory_kwargs == [{"request_timeout": 1}]
    assert room.connects[0][-1] == {"request_timeout": 1, "start_daemon": False}
    assert room.observations[-1] == {"state": "waiting", "note": "no_pending"}
    status = next((room.root / ".sharebus/private/auto/watchers").glob("*.status.json"))
    assert json.loads(status.read_text())["poll_count"] == result["poll_count"]
    assert "PRIVATE" not in status.read_text() and "token" not in status.read_text()


def test_new_canonical_job_nudges_exact_actor_without_injecting_body_or_claiming(room):
    job = room.job()
    room.response = room.pending(queued=[job])
    result = room.run()
    assert result["state"] == "nudge" and result["poll_count"] == 2
    assert result["jobs"] == [{key: job[key] for key in ("id", "event_id", "root_id")}]
    assert result["reason"].startswith(auto_watch.WAKE_PREFIX)
    assert str(room.binding["context_file"]).replace("\\", "\\\\") in result["reason"]
    assert job["id"] in result["reason"] and job["event_id"] in result["reason"]
    assert "PRIVATE" not in result["reason"] and "test-private-token" not in result["reason"]
    assert "not new human authority" in result["reason"]


def test_stop_hook_active_keeps_waiting_for_distinct_jobs_and_suppresses_duplicate_nudges(room):
    first, second = room.job(), room.job()
    room.response = room.pending(queued=[first, second])
    assert room.run()["jobs"][0]["id"] == first["id"]
    active = room.payload(stop_hook_active=True)
    result = room.run(value=active)
    assert result["state"] == "nudge" and result["jobs"][0]["id"] == second["id"]
    repeated = room.run(value=active)
    assert repeated["state"] == "waiting" and "reason" not in repeated


def test_control_changes_never_repeat_a_previously_notified_job(room):
    job = room.job()
    room.response = room.pending(queued=[job])
    assert room.run()["state"] == "nudge"
    room.response = room.pending(queued=[job], control_epoch=1001)
    assert room.run(value=room.payload(stop_hook_active=True))["state"] == "waiting"


def test_notification_history_does_not_drop_old_ids_at_512_jobs(room):
    job = room.job()
    _, _, notified = auto_watch._files(room.root, "codex", room.native)
    history = [job["id"], *(str(uuid.uuid4()) for _ in range(512))]
    auto_watch._save(notified, {"jobs": history})
    room.response = room.pending(queued=[job], control_epoch=1001)
    assert room.run()["state"] == "waiting"


@pytest.mark.parametrize("mutation", [
    {"state": "running"}, {"state": "awaiting_native_reply"}, {"state": "completed"},
    {"budget_remaining": 0}, {"budget_remaining": True}, {"deadline": 999},
    {"deadline": float("nan")}, {"agent_id": "another-role"}, {"event_id": "noncanonical"},
])
def test_ineligible_other_role_active_expired_or_malformed_job_never_wakes(room, mutation):
    room.response = room.pending(queued=[room.job(**mutation)])
    assert room.run()["state"] == "waiting"


def test_wrong_native_or_provider_response_cannot_wake_this_role(room):
    other = room.pending(queued=[room.job()])
    other["agent"].update(native_session_id="other-session", provider="claude", role="CTO")
    room.response = other
    assert room.run()["state"] == "waiting"


@pytest.mark.parametrize("state", ["running", "awaiting_native_reply"])
def test_existing_native_activity_is_not_nudged_or_changed(room, state):
    pending = room.pending(queued=[room.job()])
    pending["agent"]["state"] = state
    room.response = pending
    assert room.run()["state"] == "waiting"
    assert not list((room.root / ".sharebus/private/auto/watchers").glob("*.cancel.json"))


def test_second_control_check_prevents_nudge_if_pause_arrives(room):
    ready = room.pending(queued=[room.job()])
    paused = copy.deepcopy(ready)
    paused.update(paused=True)
    room.responses = [ready, paused]
    result = room.run()
    assert result["state"] == "blocked" and "reason" not in result
    assert room.pending_count == 2


def test_pause_cancellation_requires_explicit_control_epoch_before_rearm(room):
    ready = room.pending(queued=[room.job()])
    room.response = {**ready, "paused": True}
    assert room.run()["state"] == "blocked"
    room.response = ready
    assert room.run()["state"] == "blocked"
    room.response = {**ready, "control_epoch": 1001}
    assert room.run()["state"] == "nudge"


@pytest.mark.parametrize("control", ["disabled", "suspended", "stopped", "needs_human"])
def test_controls_never_auto_restart_or_launch_native_sessions(room, control):
    ready = room.pending(queued=[room.job()])
    if control in {"stopped", "needs_human"}:
        ready["agent"]["state"] = control
    else:
        ready["auto"].update(active=False, enabled=control != "disabled", suspended=control == "suspended")
    room.response = ready
    assert room.run()["state"] == "blocked"


def test_disabled_workspace_has_zero_watcher_files_or_enrollment(room):
    mirror = room.root / ".sharebus/auto.json"
    value = json.loads(mirror.read_text()); value["enabled"] = False
    mirror.write_text(json.dumps(value))
    assert room.run()["state"] == "offline"
    assert room.bootstraps == [] and room.connects == [] and room.requests == []
    assert not (room.root / ".sharebus/private/auto/watchers").exists()


def test_codex_native_queue_mode_skips_stop_hold_without_cancel_marker(room):
    mirror = room.root / ".sharebus/auto.json"
    value = json.loads(mirror.read_text())
    value["codex_delivery"] = "native_queue"
    mirror.write_text(json.dumps(value))
    assert room.run()["state"] == "offline"
    assert room.bootstraps == [] and room.requests == []
    assert not (room.root / ".sharebus/private/auto/watchers").exists()


def test_switch_to_native_queue_ends_existing_hold_without_cancelling_actor(room):
    ready = room.pending(queued=[room.job()])
    queued = copy.deepcopy(ready)
    queued["auto"]["codex_delivery"] = "native_queue"
    room.responses = [ready, queued]
    result = room.run()
    assert result["state"] == "offline" and "reason" not in result
    assert not list((room.root / ".sharebus/private/auto/watchers").glob("*.cancel.json"))
    assert not list((room.root / ".sharebus/private/auto/watchers").glob("*.notified.json"))


def test_claude_stop_watcher_remains_active_when_codex_uses_native_queue(room):
    mirror = room.root / ".sharebus/auto.json"
    value = json.loads(mirror.read_text())
    value["codex_delivery"] = "native_queue"
    mirror.write_text(json.dumps(value))
    room.binding["provider"] = "claude"
    pending = room.pending()
    pending["auto"]["codex_delivery"] = "native_queue"
    room.response = pending
    assert room.run("claude")["state"] == "waiting"
    assert room.bootstraps and room.requests


def test_duplicate_os_lock_avoids_duplicate_enrollment_and_waiter(room):
    path, _, _ = auto_watch._files(room.root, "codex", room.native)
    with auto_watch.native_lock(path) as acquired:
        assert acquired
        result = room.run()
        assert result["state"] == "duplicate" and room.bootstraps == []
    assert room.run()["state"] == "waiting"


def test_two_native_roles_have_independent_os_locks_and_cancel_markers(room):
    first, cancel_first, _ = auto_watch._files(room.root, "codex", room.native)
    second, cancel_second, _ = auto_watch._files(room.root, "codex", "another-native-role")
    with auto_watch.native_lock(first) as one, auto_watch.native_lock(second) as two:
        assert one and two
    assert auto_watch.cancel("codex", room.payload())
    assert cancel_first.exists() and not cancel_second.exists()


def test_native_file_lock_excludes_an_independent_process_and_releases(room):
    lock, _, _ = auto_watch._files(room.root, "codex", room.native)
    script = "\n".join([
        "import sys",
        "from pathlib import Path",
        "sys.path.insert(0, sys.argv[1])",
        "from auto_watch import native_lock",
        "with native_lock(Path(sys.argv[2])) as acquired:",
        "    print(int(acquired))",
    ])
    arguments = [sys.executable, "-c", script, str(Path(auto_watch.__file__).parent), str(lock)]
    with auto_watch.native_lock(lock) as acquired:
        assert acquired
        blocked = subprocess.run(arguments, capture_output=True, text=True, timeout=20, check=True)
        assert blocked.stdout.strip() == "0"
    released = subprocess.run(arguments, capture_output=True, text=True, timeout=20, check=True)
    assert released.stdout.strip() == "1"


def test_cancel_at_last_poll_leaves_job_unclaimed_and_no_nudge(room):
    ready = room.pending(queued=[room.job()])
    room.response = ready
    cancelled = threading.Event()
    room.on_pending = lambda count: cancelled.set() if count == 2 else None
    result = room.run(cancelled=cancelled)
    assert result["state"] == "cancelled" and "reason" not in result
    assert not list((room.root / ".sharebus/private/auto/watchers").glob("*.notified.json"))
    assert room.pending_count == 2


def test_host_death_and_signal_cancellation_release_lock_without_rearming(room):
    result = room.run(parent_alive=lambda: False)
    assert result["state"] == "cancelled" and room.pending_count == 0 and room.bootstraps == []
    room.response = room.pending(queued=[room.job()])
    assert room.run()["state"] == "blocked"
    lock, _, _ = auto_watch._files(room.root, "codex", room.native)
    with auto_watch.native_lock(lock) as acquired:
        assert acquired


@pytest.mark.parametrize("field", ["cancelled", "interrupted", "aborted"])
def test_cancelled_hook_payload_never_enrolls(room, field):
    assert room.run(value=room.payload(**{field: True}))["state"] == "cancelled"
    assert room.bootstraps == [] and room.requests == []


@pytest.mark.parametrize("timeout", [-1, 0, float("nan"), float("inf"), auto_watch.MAX_TIMEOUT + 1])
def test_wait_deadline_is_explicit_and_bounded(room, timeout):
    with pytest.raises(ValueError, match="supported bound"):
        room.run(timeout=timeout)


def test_configured_wait_defaults_to_24_hours_with_a_seven_day_bound():
    assert auto_watch.DEFAULT_TIMEOUT == 24 * 60 * 60
    assert auto_watch.MAX_TIMEOUT == 7 * 24 * 60 * 60


def test_process_import_and_input_time_count_toward_wait_deadline(room):
    room.time = 2
    result = room.run(timeout=1, started_at=0)
    assert result["state"] == "waiting"
    assert room.bootstraps == [] and room.connects == [] and room.requests == []
    assert not (room.root / ".sharebus/private/auto/watchers").exists()


def test_normal_timeout_allows_the_next_native_turn_to_listen_again(room):
    assert room.run()["state"] == "waiting"
    room.response = room.pending(queued=[room.job()])
    assert room.run(value=room.payload(turn_id="next-ordinary-native-turn"))["state"] == "nudge"


def test_preexisting_cancel_signal_never_enrolls_or_connects(room):
    stopped = threading.Event()
    stopped.set()
    assert room.run(cancelled=stopped)["state"] == "cancelled"
    assert room.bootstraps == [] and room.connects == [] and room.requests == []


def test_bootstrap_startup_time_counts_toward_overall_wait_deadline(room):
    def bootstrap(*args, **kwargs):
        room.time = 2
        return Path(room.binding["context_file"])
    result = auto_watch.watch("codex", room.payload(), room.root, timeout=1,
        clock=lambda: room.time, wall_clock=lambda: 1000, bootstrap=bootstrap,
        connect=room.connect, client_factory=room.Client)
    assert result["state"] == "waiting" and room.connects == [] and room.requests == []


def test_connection_startup_time_counts_toward_overall_wait_deadline(room):
    def connect(*args, **kwargs):
        assert 0 < kwargs["request_timeout"] <= 1 and kwargs["start_daemon"] is False
        room.time = 2
        return copy.deepcopy(room.binding)
    result = auto_watch.watch("codex", room.payload(), room.root, timeout=1,
        clock=lambda: room.time, wall_clock=lambda: 1000, bootstrap=room.bootstrap,
        connect=connect, client_factory=room.Client)
    assert result["state"] == "waiting" and room.factory_kwargs == [] and room.requests == []


def test_last_poll_cannot_nudge_or_send_final_http_after_deadline(room):
    room.response = room.pending(queued=[room.job()])
    room.on_pending = lambda count: setattr(room, "time", 0.19 if count == 1 else 0.21)
    result = room.run(timeout=0.2)
    assert result["state"] == "waiting" and "reason" not in result
    assert room.request_timeouts[0] <= 0.1 and room.request_timeouts[-1] <= 0.0051
    assert room.requests == ["/api/auto/observe", "/api/agent/pending", "/api/agent/pending"]
    assert not list((room.root / ".sharebus/private/auto/watchers").glob("*.notified.json"))


def test_private_lock_link_is_rejected(room):
    if os.name == "nt":
        pytest.skip("Symlink creation requires a host privilege; hardlink boundary is tested separately.")
    path, _, _ = auto_watch._files(room.root, "codex", room.native)
    target = room.root / "outside-lock"
    target.write_text("0")
    path.symlink_to(target)
    with pytest.raises(BusError):
        room.run()


def test_private_lock_hardlink_is_rejected(room):
    path, _, _ = auto_watch._files(room.root, "codex", room.native)
    source = room.root / "outside-lock"
    source.write_text("0")
    os.link(source, path)
    with pytest.raises(BusError):
        room.run()


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_fake_native_host_receives_only_official_wake_wire(room, monkeypatch, capsys, provider):
    room.binding["provider"] = provider
    room.response = room.pending(queued=[room.job()])
    original_watch = auto_watch.watch

    def run(*args, **kwargs):
        kwargs.update(bootstrap=room.bootstrap, connect=room.connect, client_factory=room.Client,
                      clock=lambda: room.time, wall_clock=lambda: 1000, sleep=room.sleep, started_at=0)
        return original_watch(*args, **kwargs)

    @contextmanager
    def alive_guard():
        open_handle = True
        def alive():
            assert open_handle, "Native parent handle must remain open through result emission."
            return True
        yield alive
        open_handle = False

    monkeypatch.setattr(auto_watch, "watch", run)
    monkeypatch.setattr(auto_watch, "parent_liveness", alive_guard)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(room.payload(provider)).encode())))
    code = auto_watch.main(["--provider", provider, "--timeout", "1"])
    captured = capsys.readouterr()
    if provider == "codex":
        assert code == 0 and captured.err == ""
        response = json.loads(captured.out)
        assert response["decision"] == "block" and response["reason"].startswith(auto_watch.WAKE_PREFIX)
    else:
        assert code == 2 and captured.out == "" and captured.err.startswith(auto_watch.WAKE_PREFIX)
    assert "PRIVATE" not in captured.out + captured.err


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_native_host_timeout_exits_zero_with_no_continuation(room, monkeypatch, capsys, provider):
    room.binding["provider"] = provider
    original_watch = auto_watch.watch

    def run(*args, **kwargs):
        kwargs.update(bootstrap=room.bootstrap, connect=room.connect, client_factory=room.Client,
                      clock=lambda: room.time, wall_clock=lambda: 1000, sleep=room.sleep, started_at=0)
        return original_watch(*args, **kwargs)

    @contextmanager
    def alive_guard():
        yield lambda: True

    monkeypatch.setattr(auto_watch, "watch", run)
    monkeypatch.setattr(auto_watch, "parent_liveness", alive_guard)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(room.payload(provider)).encode())))
    assert auto_watch.main(["--provider", provider, "--timeout", "1"]) == 0
    output = capsys.readouterr()
    assert output.out == ("{}\n" if provider == "codex" else "") and output.err == ""
    assert room.time < 1


def test_interrupt_cli_only_records_cancel_and_never_connects(room, monkeypatch, capsys):
    payload = room.payload(hook_event_name="Interrupt")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    assert auto_watch.main(["--provider", "codex", "--cancel"]) == 0
    assert capsys.readouterr().out == "" and room.bootstraps == [] and room.requests == []
    assert auto_watch._files(room.root, "codex", room.native)[1].exists()


def test_missing_stdin_has_a_bounded_wait_without_enrollment(room, monkeypatch):
    released = threading.Event()

    class Input:
        def read(self, size):
            assert size == auto_watch.MAX_INPUT_BYTES + 1
            released.wait(2)
            return b"{}"

    monkeypatch.setattr(auto_watch, "STDIN_TIMEOUT", 0.01)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=Input()))
    try:
        assert auto_watch._read_input(auto_watch.time.monotonic() + 1) is None
        assert room.bootstraps == [] and room.requests == []
    finally:
        released.set()


def test_hook_input_byte_limit_rejects_oversized_payload(room, monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b" " * (auto_watch.MAX_INPUT_BYTES + 1))))
    assert auto_watch._read_input(auto_watch.time.monotonic() + 1) is None
    assert room.bootstraps == [] and room.requests == []


def test_expired_process_budget_exits_before_reading_stdin(room, monkeypatch, capsys):
    class Input:
        def read(self, size):
            pytest.fail("Expired hook must not wait on stdin.")

    monkeypatch.setattr(auto_watch, "_PROCESS_STARTED", auto_watch.time.monotonic() - 2)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=Input()))
    assert auto_watch.main(["--provider", "codex", "--timeout", "1"]) == 0
    assert capsys.readouterr().out == "" and room.bootstraps == []


def test_wrong_workspace_and_managed_native_are_not_adopted(room, monkeypatch):
    assert auto_watch.identity("codex", room.payload(), room.root / "other") is None
    monkeypatch.setenv("SHAREBUS_MANAGED", "1")
    assert room.run()["state"] == "offline" and room.bootstraps == []


def test_partial_managed_token_binding_never_enrolls_a_native_role(room, monkeypatch):
    monkeypatch.setenv("SHAREBUS_AGENT_TOKEN_FILE", "managed-private-path")
    assert room.run()["state"] == "offline" and room.bootstraps == [] and room.connects == []


def test_bus_error_is_private_and_never_becomes_a_wake(room):
    room.on_pending = lambda count: (_ for _ in ()).throw(BusError("SENSITIVE_ERROR_BODY"))
    result = room.run()
    assert result["state"] == "offline" and "reason" not in result
    assert "SENSITIVE" not in json.dumps(room.observations)
