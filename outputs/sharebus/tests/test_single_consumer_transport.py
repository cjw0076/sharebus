"""Single-consumer transport races with real local ledgers and fake providers."""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import native_bridge
from adapters import TurnResult
from hosted_runtime import HostedAuthorityDenied, HostedLedger
from native_bridge import NativeDeliveryResult, NeedsNativeAttach, _dispatch_binding, guard_send, reserve_native_dispatch
from store import BusError, BusStore
from supervisor import Supervisor


@pytest.fixture(autouse=True)
def isolated_provider_environment(tmp_path, monkeypatch):
    home = tmp_path / "provider-home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(home))
    for key in ("SHAREBUS_AGENT_ID", "SHAREBUS_WORKSPACE", "SHAREBUS_AGENT_TOKEN_FILE", "SHAREBUS_MANAGED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("A real child was not authorized"))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("A native executable was not authorized"))
    monkeypatch.setattr(socket.socket, "connect", lambda *args, **kwargs: pytest.fail("Network is not part of this fixture"))
    candidate = Path(__file__).resolve().parents[3]
    for module in (native_bridge, sys.modules[BusStore.__module__], sys.modules[Supervisor.__module__]):
        assert Path(module.__file__).resolve().is_relative_to(candidate)


@pytest.fixture
def room(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    store = BusStore(root)
    native = str(uuid.uuid4())
    agent, _ = store.register("claude", "Fixture original peer", mode="native_attached",
                              native_session_id=native, native_address="Fixture [a1b2c3]")
    store.send("user", agent["id"], "Review this selected artifact", "task", budget=1)
    job = store.claim(agent["id"], execution_owner="native_dispatch")
    assert job is not None and job["remaining_turns"] == 0
    return SimpleNamespace(root=root, store=store, agent=agent, native=native, job=job)


def _force_terminal(room):
    with room.store.connection() as db:
        db.execute("UPDATE jobs SET state='completed' WHERE id=?", (room.job["id"],))
        db.execute("UPDATE agents SET state='idle',active_root=NULL WHERE id=?", (room.agent["id"],))


def _binding(room):
    return _dispatch_binding({"workspace_id": room.store.workspace_id, "nonce": "fixture-nonce"},
        room.store.agent(room.agent["id"]), job_id=room.job["id"], event_id=room.job["event_id"], root_id=room.job["root_id"])


def _client_for_store(room, monkeypatch, *, before_reserve=None):
    import mcp_server
    calls = []

    class Client:
        def __init__(self, provider, root):
            assert root == room.root and provider == "claude"

        def runtime(self, root):
            return {"workspace_id": room.store.workspace_id, "nonce": "fixture-nonce"}

        def request(self, root, path, token, payload):
            assert path == "/api/native/dispatch"
            calls.append(dict(payload))
            if before_reserve:
                before_reserve()
            return {"dispatch": room.store.reserve_native_input(room.agent["id"], payload["job_id"], payload["event_id"],
                payload["root_id"], payload["native_session_id"], generation=payload["generation"],
                provider=payload["provider"], native_address=payload["native_address"])}

    monkeypatch.setattr(mcp_server, "BusClient", Client)
    return calls


def test_inbox_cannot_finish_native_claim_and_stale_terminal_worker_never_turns(room):
    before = room.store.jobs()
    with pytest.raises(BusError):
        room.store.external_inbox(room.agent["id"], acknowledgements=[room.job["id"]])
    assert room.store.jobs() == before
    _force_terminal(room)
    starts = []
    supervisor = Supervisor(room.store, [], adapter_factory=lambda *a, **k: starts.append(1))
    supervisor._run(room.agent["id"], room.job)
    assert starts == [] and room.store.jobs()[0]["state"] == "completed"
    supervisor.close()


@pytest.mark.parametrize("change", ["terminal", "new-root", "rebound", "paused"])
def test_slow_start_cannot_update_or_turn_after_exact_authority_changes(room, change):
    entered, release = threading.Event(), threading.Event()
    calls, adapters = [], []

    class Adapter:
        pid = None
        def __init__(self, *args, **kwargs):
            adapters.append(self)
            self.closed = False
        def start(self, resume_id=None):
            entered.set()
            assert release.wait(5)
            return resume_id
        def turn(self, *args, **kwargs):
            calls.append(1)
            return NativeDeliveryResult("pending", room.native, True)
        def close(self):
            self.closed = True
        def interrupt(self):
            pass

    supervisor = Supervisor(room.store, [], adapter_factory=Adapter)
    worker = threading.Thread(target=supervisor._run, args=(room.agent["id"], room.job))
    worker.start()
    try:
        assert entered.wait(5)
        if change in {"terminal", "new-root"}:
            _force_terminal(room)
        if change == "new-root":
            room.store.send("user", room.agent["id"], "An independently authorized new task", "task")
            next_job = room.store.claim(room.agent["id"], execution_owner="native_dispatch")
            assert next_job is not None
        elif change == "rebound":
            room.store.update_agent(room.agent["id"], native_session_id=str(uuid.uuid4()), generation=7)
        elif change == "paused":
            room.store.set_paused(True)
        expected = room.store.agent(room.agent["id"])
    finally:
        release.set()
        worker.join(6)
    assert not worker.is_alive() and calls == []
    current = room.store.agent(room.agent["id"])
    for key in (("generation", "native_session_id", "channel_verified", "pid") if change == "paused"
                else ("active_root", "generation", "native_session_id", "channel_verified", "pid")):
        assert current[key] == expected[key]
    if change in {"terminal", "new-root", "rebound"}:
        assert current["state"] == expected["state"]
    assert adapters[0].closed
    supervisor.close()


def test_late_receipt_after_callback_and_new_root_does_not_overwrite_new_binding(room):
    calls = []
    newer = {}

    class Adapter:
        pid = None
        def __init__(self, *args, **kwargs):
            self.closed = False
        def start(self, resume_id=None):
            return resume_id
        def turn(self, *args, **kwargs):
            calls.append(1)
            own = room.store.agent(room.agent["id"])
            room.store.reserve_native_input(own["id"], room.job["id"], room.job["event_id"], room.job["root_id"],
                own["native_session_id"], generation=own["generation"], provider=own["provider"], native_address=own["native_address"])
            assert room.store.native_result(own["id"], room.native, "The selected old result", room.job["id"], room.job["event_id"])["completed"]
            room.store.send("user", own["id"], "The next user task", "task")
            newer["job"] = room.store.claim(own["id"], execution_owner="native_dispatch")
            newer["agent"] = room.store.agent(own["id"])
            return NativeDeliveryResult("Old receipt arrived late", room.native, True)
        def close(self):
            self.closed = True
        def interrupt(self):
            pass

    supervisor = Supervisor(room.store, [], adapter_factory=Adapter)
    supervisor._run(room.agent["id"], room.job)
    assert calls == [1]
    current = room.store.agent(room.agent["id"])
    for key in ("active_root", "generation", "native_session_id", "channel_verified", "state"):
        assert current[key] == newer["agent"][key]
    jobs = {job["id"]: job for job in room.store.jobs()}
    assert jobs[room.job["id"]]["state"] == "completed"
    assert jobs[newer["job"]["id"]]["state"] == "running"
    supervisor.close()


@pytest.mark.parametrize("change", ["native-id", "address", "generation"])
def test_active_late_receipt_after_revocation_preserves_job_binding_and_history(room, change):
    expected = {}

    class Adapter:
        pid = None
        def __init__(self, *args, **kwargs):
            pass
        def start(self, resume_id=None):
            return resume_id
        def turn(self, *args, **kwargs):
            own = room.store.agent(room.agent["id"])
            room.store.reserve_native_input(own["id"], room.job["id"], room.job["event_id"], room.job["root_id"],
                own["native_session_id"], generation=own["generation"], provider=own["provider"], native_address=own["native_address"])
            updates = ({"native_session_id": "replacement-session"} if change == "native-id" else
                       {"native_address": "Replacement [ffffff]"} if change == "address" else {"generation": 7})
            room.store.update_agent(own["id"], **updates)
            expected.update(agent=room.store.agent(own["id"]), jobs=room.store.jobs(), events=room.store.events())
            return NativeDeliveryResult("Late receipt from the revoked original attempt", room.native, True)
        def close(self):
            pass
        def interrupt(self):
            pass

    supervisor = Supervisor(room.store, [], adapter_factory=Adapter)
    try:
        supervisor._run(room.agent["id"], room.job)
        assert room.store.agent(room.agent["id"]) == expected["agent"]
        assert room.store.jobs() == expected["jobs"]
        assert room.store.events() == expected["events"]
    finally:
        supervisor.close()


def test_last_lawful_budget_turn_can_reserve_once_and_unknown_cannot_retry(room, monkeypatch):
    calls = _client_for_store(room, monkeypatch)
    binding = _binding(room)
    first = reserve_native_dispatch(room.root, "claude", binding)
    assert first["native_input_reserved_at"] > 0
    with pytest.raises(NeedsNativeAttach):
        reserve_native_dispatch(room.root, "claude", binding)
    assert len(calls) == 2
    current = room.store.jobs()[0]
    assert current["state"] == "running" and current["native_input_reserved_at"] == first["native_input_reserved_at"]


@pytest.mark.parametrize("change", ["terminal", "pause", "generation", "null-address"])
def test_final_native_guard_denies_changed_binding_without_consuming_grant(room, monkeypatch, change):
    if change == "null-address":
        with room.store.connection() as db:
            db.execute("UPDATE agents SET native_address=NULL WHERE id=?", (room.agent["id"],))
    binding = _binding(room)
    def changed():
        if change == "terminal":
            _force_terminal(room)
        elif change == "pause":
            room.store.set_paused(True)
        elif change == "generation":
            room.store.update_agent(room.agent["id"], generation=5)
        else:
            room.store.update_agent(room.agent["id"], native_address="Different [ffffff]")
    _client_for_store(room, monkeypatch, before_reserve=changed)
    with pytest.raises(NeedsNativeAttach):
        reserve_native_dispatch(room.root, "claude", binding)
    assert room.store.jobs()[0]["native_input_reserved_at"] is None


def test_claude_final_tool_guard_fails_after_slow_proxy_and_never_reopens_claim(room, monkeypatch):
    calls = _client_for_store(room, monkeypatch)
    request = room.root / "delivery.json"
    request.write_text(json.dumps({"workspace": str(room.root), "native_session_id": room.native,
        "native_address": room.agent["native_address"], "native_listing_confirmed": True,
        "body": "Selected exact public message", "claimed_path": str(room.root / "one.claim"),
        "dispatch_binding": _binding(room)}), encoding="utf-8")
    payload = {"tool_name": "SendMessage", "tool_input": {"to": room.agent["native_address"], "message": "Selected exact public message"}}
    _force_terminal(room)
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(calls) == 1
    assert (room.root / "one.claim").exists()
    assert room.store.jobs()[0]["native_input_reserved_at"] is None


def test_hosted_result_rejects_other_owner_without_touching_managed_attempt(room):
    ledger = HostedLedger(room.store, execution_ready=lambda: True)
    with room.store.connection() as db:
        db.execute("CREATE TABLE avalokita_hosted_attempts(job_id TEXT PRIMARY KEY,state TEXT,updated_at REAL)")
        db.execute("CREATE TABLE avalokita_hosted_approvals(job_id TEXT,state TEXT)")
        db.execute("INSERT INTO avalokita_hosted_attempts VALUES (?,'not_sent',0)", (room.job["id"],))
    with pytest.raises(BusError, match="managed execution owner"):
        ledger.finish(room.job["id"], "completed")
    with room.store.connection() as db:
        assert db.execute("SELECT state FROM avalokita_hosted_attempts").fetchone()[0] == "not_sent"
    assert room.store.jobs()[0]["state"] == "running"


def test_two_final_claude_guards_share_one_durable_input_reservation(room, monkeypatch):
    ready = threading.Barrier(2)
    calls = _client_for_store(room, monkeypatch, before_reserve=lambda: ready.wait(timeout=5))
    binding, decisions = _binding(room), []
    requests = []
    for number in range(2):
        request = room.root / f"delivery-{number}.json"
        request.write_text(json.dumps({"workspace": str(room.root), "native_session_id": room.native,
            "native_address": room.agent["native_address"], "native_listing_confirmed": True,
            "body": "Exact selected body", "claimed_path": str(room.root / f"claim-{number}"),
            "dispatch_binding": binding}), encoding="utf-8")
        requests.append(request)
    payload = {"tool_name": "SendMessage", "tool_input": {"to": room.agent["native_address"], "message": "Exact selected body"}}
    workers = [threading.Thread(target=lambda path=path: decisions.append(guard_send(path, payload)["hookSpecificOutput"]["permissionDecision"]))
               for path in requests]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(7)
    assert all(not worker.is_alive() for worker in workers)
    assert sorted(decisions) == ["allow", "deny"] and len(calls) == 2
    assert room.store.jobs()[0]["native_input_reserved_at"] is not None


def test_managed_runtime_completes_with_its_own_attempt_and_no_native_dispatch_grant(tmp_path):
    root = tmp_path / "owned-workspace"
    root.mkdir()
    store = BusStore(root)
    ledger = HostedLedger(store, execution_ready=lambda: True)
    configured = ledger.configure(["claude"], [{"provider": "claude", "installed": True, "managed_supported": True}])
    actor = configured["sessions"][0]["agent_id"]
    store.send("user", actor, "Complete one bounded managed input", "task", budget=1)
    job = store.claim(actor, execution_owner="managed_runtime")
    agent = store.agent(actor)
    ledger.begin(agent, job, 1)
    ledger.event(actor, job, 1, {"type": "session", "session_id": "owned-fixture-session"})
    ledger.event(actor, job, 1, {"type": "input_attempt", "session_id": "owned-fixture-session"})
    ledger.event(actor, job, 1, {"type": "turn", "status": "accepted", "session_id": "owned-fixture-session", "turn_id": "owned-turn"})
    ledger.complete(job, 1, TurnResult("Selected managed output", "owned-fixture-session", True, status="completed"))
    completed = store.jobs()[0]
    assert completed["state"] == "completed" and completed["execution_owner"] == "managed_runtime"
    assert completed["native_input_reserved_at"] is None
    assert store.agent(actor)["channel_verified"]


def test_managed_required_input_observer_rejects_terminal_job_before_fake_stdin(tmp_path):
    root = tmp_path / "managed-workspace"
    root.mkdir()
    store = BusStore(root)
    agent, _ = store.register("claude", "Managed fixture", mode="managed_resume", native_session_id="owned-session")
    store.send("user", agent["id"], "One owned task", "task")
    job = store.claim(agent["id"], execution_owner="managed_runtime")
    writes = []
    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            return resume_id
        def turn(self, *args, **kwargs):
            with store.connection() as db:
                db.execute("UPDATE jobs SET state='completed' WHERE id=?", (job["id"],))
                db.execute("UPDATE agents SET state='idle',active_root=NULL WHERE id=?", (agent["id"],))
            self.on_event({"type": "input_attempt", "session_id": "owned-session"})
            writes.append("stdin")
            return TurnResult("Unreachable", "owned-session", True)
        def close(self):
            pass
        def interrupt(self):
            pass
    supervisor = Supervisor(store, [], adapter_factory=Adapter)
    supervisor._run(agent["id"], job)
    assert writes == [] and store.jobs()[0]["state"] == "completed"
    supervisor.close()


def test_native_first_startup_advances_only_its_exact_job_generation(room):
    turns = []
    class Adapter:
        pid = None
        def __init__(self, *args, **kwargs):
            pass
        def start(self, resume_id=None):
            return resume_id
        def turn(self, *args, **kwargs):
            own = room.store.agent(room.agent["id"])
            with room.store.connection() as db:
                generation = db.execute("SELECT generation FROM jobs WHERE id=?", (room.job["id"],)).fetchone()[0]
            assert generation == own["generation"] == room.job["generation"] == 1
            room.store.reserve_native_input(own["id"], room.job["id"], room.job["event_id"], room.job["root_id"],
                own["native_session_id"], generation=own["generation"], provider=own["provider"], native_address=own["native_address"])
            turns.append(1)
            return NativeDeliveryResult("Exact synthetic delivery receipt", room.native, True)
        def close(self):
            pass
        def interrupt(self):
            pass
    supervisor = Supervisor(room.store, [], adapter_factory=Adapter)
    supervisor._run(room.agent["id"], room.job)
    assert turns == [1] and room.store.jobs()[0]["state"] == "awaiting_native_reply"
    assert room.store.agent(room.agent["id"])["channel_verified"]
    supervisor.close()


@pytest.mark.parametrize("generation", [None, 8])
def test_changed_canonical_job_generation_is_not_adopted_from_agent_state(room, generation):
    with room.store.connection() as db:
        db.execute("UPDATE jobs SET generation=? WHERE id=?", (generation, room.job["id"]))
    starts = []
    supervisor = Supervisor(room.store, [], adapter_factory=lambda *args, **kwargs: starts.append(1))
    supervisor._run(room.agent["id"], room.job)
    assert starts == [] and room.store.jobs()[0]["state"] == "running"
    assert room.store.agent(room.agent["id"])["generation"] == 0
    supervisor.close()


def test_supervisor_cannot_execute_even_an_active_manual_inbox_job(tmp_path):
    root = tmp_path / "manual-workspace"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Manual native participant", native_session_id="manual-session")
    store.send("user", actor["id"], "Only the explicit receiver owns this", "task")
    job = store.external_inbox(actor["id"], claim_work=True)["jobs"][0]
    starts = []
    supervisor = Supervisor(store, [], adapter_factory=lambda *args, **kwargs: starts.append(1))
    supervisor._run(actor["id"], job)
    assert starts == []
    assert store.jobs()[0]["execution_owner"] == "manual_inbox" and store.jobs()[0]["state"] == "running"
    supervisor.close()


@pytest.fixture
def hosted_room(tmp_path):
    root = tmp_path / "hosted-workspace"
    root.mkdir()
    store = BusStore(root)
    supervisor = Supervisor(store, [], hosted_execution_ready=lambda: True)
    configured = supervisor.hosted.configure(["claude"],
        [{"provider": "claude", "installed": True, "managed_supported": True}])
    actor = configured["sessions"][0]["agent_id"]
    try:
        yield SimpleNamespace(root=root, store=store, supervisor=supervisor, actor=actor)
    finally:
        supervisor.close()


def test_hosted_start_resident_turn_and_restart_preserve_exact_generation(hosted_room):
    room = hosted_room
    starts, turns = [], []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            self.pid = 424242  # Synthetic resident transport; no OS process exists.
            starts.append(resume_id)
            self.on_event({"type": "session", "session_id": "original-hosted-session"})
            return "original-hosted-session"
        def turn(self, prompt, **kwargs):
            current = room.store.agent(room.actor)
            with room.store.connection() as db:
                job = db.execute("SELECT * FROM jobs WHERE agent_id=? AND state='running'", (room.actor,)).fetchone()
                owner = db.execute("SELECT * FROM avalokita_hosted_owners WHERE actor=?", (room.actor,)).fetchone()
                attempt = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()
                assert job["generation"] == current["generation"] == owner["generation"] == attempt["generation"]
                assert job["native_input_reserved_at"] is None
            assert "original-hosted-session" == current["native_session_id"]
            self.on_event({"type": "input_attempt", "session_id": "original-hosted-session"})
            turns.append(current["generation"])
            self.on_event({"type": "turn", "status": "accepted", "session_id": "original-hosted-session",
                           "turn_id": f"turn-{len(turns)}"})
            return TurnResult("Selected owned result", "original-hosted-session", True, status="completed")
        def close(self):
            self.pid = None
        def interrupt(self):
            pass

    room.supervisor.adapter_factory = Adapter
    for number in range(3):
        room.store.send("user", room.actor, f"Independently authorized turn {number}", "task", budget=1)
        job = room.store.claim(room.actor, execution_owner="managed_runtime")
        room.supervisor._run(room.actor, job)
        assert next(item for item in room.store.jobs() if item["id"] == job["id"])["state"] == "completed"
        if number == 1:
            room.supervisor.close()
            room.supervisor = Supervisor(room.store, [], adapter_factory=Adapter, hosted_execution_ready=lambda: True)
    assert starts == [None, "original-hosted-session"]
    assert turns == [1, 1, 2]
    assert room.store.agent(room.actor)["channel_verified"]


@pytest.mark.parametrize("change", ["terminal", "new-root", "native-id", "address", "generation"])
def test_hosted_slow_start_cannot_adopt_a_later_binding_or_root(hosted_room, change):
    room = hosted_room
    entered, release = threading.Event(), threading.Event()
    turns, closed = [], []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            self.on_event({"type": "session", "session_id": "original-hosted-session"})
            entered.set()
            assert release.wait(5)
            return "original-hosted-session"
        def turn(self, *args, **kwargs):
            turns.append(1)
            return TurnResult("Unreachable", "original-hosted-session", True)
        def close(self):
            closed.append(1)
        def interrupt(self):
            pass

    room.supervisor.adapter_factory = Adapter
    room.store.send("user", room.actor, "Original hosted task", "task", budget=1)
    job = room.store.claim(room.actor, execution_owner="managed_runtime")
    worker = threading.Thread(target=room.supervisor._run, args=(room.actor, job))
    worker.start()
    try:
        assert entered.wait(5)
        if change in {"terminal", "new-root"}:
            with room.store.connection() as db:
                db.execute("UPDATE jobs SET state='completed' WHERE id=?", (job["id"],))
                db.execute("UPDATE agents SET state='idle',active_root=NULL WHERE id=?", (room.actor,))
            if change == "new-root":
                room.store.send("user", room.actor, "Later human task", "task")
                assert room.store.claim(room.actor, execution_owner="managed_runtime") is not None
        elif change == "native-id":
            room.store.update_agent(room.actor, native_session_id="replacement-session")
        elif change == "address":
            room.store.update_agent(room.actor, native_address="avalokita-hosted/1:replacement-owner")
        else:
            room.store.update_agent(room.actor, generation=7)
        expected_agent, expected_jobs = room.store.agent(room.actor), room.store.jobs()
    finally:
        release.set()
        worker.join(6)
    assert not worker.is_alive() and turns == [] and closed == [1]
    assert room.store.agent(room.actor) == expected_agent
    assert room.store.jobs() == expected_jobs


def test_hosted_known_not_sent_hold_resumes_original_reservation_once(hosted_room):
    room = hosted_room
    room.store.send("user", room.actor, "One held human task", "task", budget=1)
    job = room.store.claim(room.actor, execution_owner="managed_runtime")
    ledger = room.supervisor.hosted
    original = room.store.agent(room.actor)
    ledger.begin(original, job, 1)
    assert ledger.refuse(job, 1, HostedAuthorityDenied("Temporarily paused", state="avalokita_paused"), original)
    ledger._admission = lambda *args, **kwargs: nullcontext(
        {"job_id": job["id"], "validate": lambda db: True})
    resumed = ledger.resume_reserved(room.actor)
    assert resumed["id"] == job["id"] and resumed["remaining_turns"] == 0
    ledger.begin(room.store.agent(room.actor), resumed, 2)
    with room.store.connection() as db:
        assert db.execute("SELECT used FROM roots WHERE id=?", (job["root_id"],)).fetchone()[0] == 1
        assert db.execute("SELECT generation FROM jobs WHERE id=?", (job["id"],)).fetchone()[0] == 2
    assert room.store.agent(room.actor)["generation"] == 2
    assert ledger.resume_reserved(room.actor) is None


@pytest.mark.parametrize("phase", ["startup", "input"])
def test_hosted_failure_preserves_attempt_and_never_restarts_same_job(hosted_room, phase):
    room = hosted_room
    starts, writes = [], []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            starts.append(1)
            if phase == "startup":
                raise RuntimeError("Owned fixture startup failed before input")
            self.on_event({"type": "session", "session_id": "original-hosted-session"})
            return "original-hosted-session"
        def turn(self, *args, **kwargs):
            self.on_event({"type": "input_attempt", "session_id": "original-hosted-session"})
            writes.append(1)
            return TurnResult("", "original-hosted-session", False, delivery_unknown=True, status="delivery_unknown")
        def close(self):
            pass
        def interrupt(self):
            pass

    room.supervisor.adapter_factory = Adapter
    room.store.send("user", room.actor, "One bounded failure", "task", budget=1)
    job = room.store.claim(room.actor, execution_owner="managed_runtime")
    room.supervisor._run(room.actor, job)
    expected_jobs = room.store.jobs()
    room.supervisor._run(room.actor, job)
    assert room.store.jobs() == expected_jobs and starts == [1]
    assert writes == ([1] if phase == "input" else [])
    assert expected_jobs[0]["state"] == ("awaiting_native_reply" if phase == "input" else "needs_human")
    assert expected_jobs[0]["native_input_reserved_at"] is None
    with room.store.connection() as db:
        attempt = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()
        assert attempt["state"] == ("delivery_unknown" if phase == "input" else "not_sent")
        assert attempt["input_attempted"] == int(phase == "input")
        assert db.execute("SELECT used FROM roots WHERE id=?", (job["root_id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["managed_new", "managed_resume"])
def test_generic_managed_first_start_completes_with_canonical_generation(tmp_path, mode):
    root = tmp_path / "generic-managed-workspace"
    root.mkdir()
    store = BusStore(root)
    actor, _ = store.register("claude", "Owned managed fixture", mode=mode,
        native_session_id="managed-session" if mode == "managed_resume" else None)
    store.send("user", actor["id"], "One normal managed result", "task", budget=1)
    job = store.claim(actor["id"], execution_owner="managed_runtime")
    turns = []

    class Adapter:
        pid = None
        def __init__(self, *args, on_event=None, **kwargs):
            self.on_event = on_event
        def start(self, resume_id=None):
            assert resume_id == actor["native_session_id"]
            self.on_event({"type": "session", "session_id": "managed-session"})
            return "managed-session"
        def turn(self, *args, **kwargs):
            self.on_event({"type": "input_attempt", "session_id": "managed-session"})
            turns.append(1)
            return TurnResult("Selected managed result", "managed-session", True)
        def close(self):
            pass
        def interrupt(self):
            pass

    supervisor = Supervisor(store, [], adapter_factory=Adapter)
    try:
        supervisor._run(actor["id"], job)
        assert turns == [1] and store.jobs()[0]["state"] == "completed"
        assert store.agent(actor["id"])["native_session_id"] == "managed-session"
        assert store.agent(actor["id"])["generation"] == job["generation"] == 1
        assert store.agent(actor["id"])["channel_verified"]
        assert store.jobs()[0]["native_input_reserved_at"] is None
    finally:
        supervisor.close()
