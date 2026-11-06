"""Real owned-work admission with disposable native objects and exact boundaries."""
import json
import importlib.util
from contextlib import contextmanager
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import hosted as hosted_module
import sidecar_registry as registry
import test_hosted as existing
from bridge import BusError
from server import Runtime, Server
from test_service_startup import wait_for

OBSERVATIONS = []
CANDIDATES = [{"provider": name, "installed": True, "managed_supported": True}
              for name in ("codex", "claude", "antigravity")]


class DispatchBoundary:
    """Run the real loop once per ticket, including when its worker is blocked."""
    def __init__(self, stopped):
        self.stopped = stopped
        self.condition = threading.Condition()
        self.boundaries = 0
        self.tickets = 0

    def wait(self, timeout=None):
        with self.condition:
            self.boundaries += 1
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.tickets or self.stopped.is_set())
            if self.tickets:
                self.tickets -= 1
            return True

    def clear(self):
        pass

    def set(self):
        with self.condition:
            self.condition.notify_all()

    def tick(self):
        with self.condition:
            assert self.condition.wait_for(lambda: self.boundaries, timeout=4)
            before = self.boundaries
            self.tickets += 1
            self.condition.notify_all()
            assert self.condition.wait_for(lambda: self.boundaries > before, timeout=4), "Dispatcher did not reach its next loop boundary"


class AuthorityRoom:
    def __init__(self, fixture):
        self.fixture = fixture
        self.workspace = fixture.workspace
        self.runtimes = [fixture.runtime]
        self.buses, self.servers, self.releases = [], [], []
        self.boundaries = {}
        self.actor = None

    @property
    def runtime(self):
        return self.fixture.runtime

    @property
    def bus(self):
        return self.fixture.bus

    def configure(self, *providers):
        sessions = self.fixture.configure(*providers)["sessions"]
        if self.bus not in self.buses:
            self.buses.append(self.bus)
        self.stop_dispatcher()
        self.actor = next(item["agent_id"] for item in sessions if item["provider"] == "codex")
        return {item["provider"]: item["agent_id"] for item in sessions}

    def stop_dispatcher(self):
        supervisor = self.bus.supervisor
        supervisor._stop.set()
        supervisor.wake()
        supervisor._dispatcher.join(timeout=3)
        assert not supervisor._dispatcher.is_alive()

    def prompt(self, key, **values):
        return self.fixture.prompt(key, **{"recipient_ids": [self.actor], "budget": 1, "ttl": 60, **values})

    def receipt(self, saved):
        return self.runtime.bridge.dispatches()[saved["goal"]["id"]]

    def job(self, saved):
        root_id = self.receipt(saved)["root_id"]
        return next(job for job in self.bus.store.jobs() if job["root_id"] == root_id)

    def root(self, saved):
        with self.bus.store.connection() as db:
            return dict(db.execute("SELECT * FROM roots WHERE id=?", (self.receipt(saved)["root_id"],)).fetchone())

    def attempts(self, saved):
        with self.bus.store.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM avalokita_hosted_attempts WHERE root_id=?",
                                                   (self.receipt(saved)["root_id"],))]

    def tick(self):
        supervisor = self.bus.supervisor
        if supervisor not in self.boundaries:
            assert not supervisor._dispatcher.is_alive()
            supervisor._stop.clear()
            boundary = DispatchBoundary(supervisor._stop)
            self.boundaries[supervisor] = boundary
            supervisor._wake = boundary
            supervisor._dispatcher = threading.Thread(target=supervisor._dispatch, name="authority-test-dispatch", daemon=True)
            supervisor._dispatcher.start()
        self.boundaries[supervisor].tick()

    def settle(self):
        for worker in list(self.bus.supervisor._workers.values()):
            worker.join(timeout=4)
            assert not worker.is_alive()
        assert not self.bus.supervisor._workers

    def pump(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.tick()
            if predicate() and not self.bus.supervisor._workers:
                return
        assert predicate(), "Owned authority fixture did not reach the selected state"
        self.settle()

    def complete(self, saved):
        self.pump(lambda: self.job(saved)["state"] == "completed")

    def inputs(self):
        return sum(len(item.prompts) for item in existing.FakeNative.instances)

    def restart_normally(self):
        self.runtime.hosted.close()
        runtime = Runtime(self.workspace, discovery=False, native_ipc=False,
                          hosted_factory=existing.FakeNative, hosted_discovery=CANDIDATES)
        self.runtimes.append(runtime)
        self.fixture.runtime = runtime
        server = Server(self.workspace, runtime=runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.servers.append((server, thread))
        thread.start()
        wait_for(lambda: runtime.hosted.service_snapshot()["state"] == "ready")
        self.fixture.bus = runtime.hosted.server
        self.buses.append(self.bus)

    def claim_prepared(self):
        with self.runtime._hosted_admission(self.bus.store, self.actor) as expected:
            return self.bus.store.claim(self.actor, expected=expected)

    def hold_before_input(self, monkeypatch, boundary):
        reached, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def wait_once():
            if not reached.is_set():
                reached.set()
                assert release.wait(4), "The test did not release its selected input boundary"

        if boundary == "before_begin":
            original = self.bus.supervisor._run_hosted

            def run(agent, job):
                wait_once()
                return original(agent, job)

            monkeypatch.setattr(self.bus.supervisor, "_run_hosted", run)
        elif boundary == "after_start":
            original = existing.FakeNative.start

            def start(native, resume_id=None):
                value = original(native, resume_id)
                wait_once()
                return value

            monkeypatch.setattr(existing.FakeNative, "start", start)
        else:
            def turn(native, prompt, timeout=240):
                wait_once()
                native.on_event({"type": "input_attempt", "session_id": native.session_id})
                native.prompts.append(prompt)
                native.on_event({"type": "turn", "status": "accepted", "session_id": native.session_id,
                                 "turn_id": "selected-test-turn"})
                return SimpleNamespace(text="Selected result", session_id=native.session_id, success=True,
                                       status="completed", input_attempted=True, delivery_only=False)

            monkeypatch.setattr(existing.FakeNative, "turn", turn)
        return reached, release

    def close(self):
        for release in self.releases:
            release.set()
        for item in existing.FakeNative.instances:
            item.interrupt()
        for server, thread in self.servers:
            server.server_close()
            thread.join(timeout=4)
            assert not thread.is_alive()
        for runtime in self.runtimes:
            runtime.begin_closing()
            runtime.stop_peer_reconciler()
            runtime.device_discovery.stop()
            runtime.hosted.close()
        observation = {"active_work": [item._active_work for item in self.runtimes],
                       "workers": [list(bus.supervisor._workers) for bus in self.buses],
                       "dispatchers_alive": [bus.supervisor._dispatcher.is_alive() for bus in self.buses],
                       "listeners": [bus.fileno() for bus in self.buses] +
                                    [server.fileno() for server, _ in self.servers]}
        OBSERVATIONS.append(observation)
        assert observation["active_work"] == [0] * len(self.runtimes)
        assert not any(observation["workers"] + observation["dispatchers_alive"])
        assert set(observation["listeners"]) == {-1}
        self.fixture.tearDown()


@pytest.fixture
def room(tmp_path, monkeypatch):
    before_threads = set(threading.enumerate())
    directory = tmp_path / "registry"
    directory.mkdir(parents=True)
    register = registry.register_workspace
    monkeypatch.setattr(registry, "REGISTRY", directory)

    def register_temp(root, *, runtime, workspace_guard):
        return register(root, runtime=runtime, registry_path=directory, workspace_guard=workspace_guard)

    monkeypatch.setattr(hosted_module, "register_workspace", register_temp)
    existing.FakeNative.controlled_gates = set()
    fixture = existing.HostedTests("test_standalone_live_dispatcher_cannot_claim_persisted_hosted_work")
    fixture.setUp()
    value = AuthorityRoom(fixture)
    try:
        value.configure("codex")
        yield value
    finally:
        value.close()
        wait_for(lambda: not [thread for thread in threading.enumerate() if thread not in before_threads], timeout=4)


@pytest.fixture
def native_wire(room, monkeypatch):
    """Use test_adapters.py's Codex wire frames in RAM; never create an OS child."""
    name = "sharebus_authority_native_adapter"
    spec = importlib.util.spec_from_file_location(name, HERE.parent / "sharebus" / "adapters.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    wires, results = [], []

    def make(provider, workspace, actor, token, **options):
        native = module.NativeAdapter(provider, workspace, actor, token, **options)
        frames, prompts = [], []
        process = SimpleNamespace(pid=1, closed=False)
        process.poll = lambda: 0 if process.closed else None
        process.wait = lambda timeout=None: 0

        def close():
            process.closed = True

        def write(encoded):
            value = json.loads(encoded)
            frames.append(value)
            identifier = value["id"]
            if value["method"] == "account/read":
                native._messages.put({"id": identifier, "result": {"account": {"type": "chatgpt"}}})
            elif value["method"] == "turn/start":
                prompts.append(value["params"]["input"][0]["text"])
                turn = str(len(prompts))
                session = native.session_id
                native._messages.put({"id": identifier, "result": {"turn": {"id": turn, "status": "inProgress"}}})
                native._messages.put({"method": "item/completed", "params": {"threadId": session, "turnId": turn,
                    "item": {"id": "answer", "type": "agentMessage", "text": "ready"}}})
                native._messages.put({"method": "turn/completed", "params": {"threadId": session,
                    "turn": {"id": turn, "status": "completed"}}})
            else:
                raise AssertionError("Unexpected in-process test wire method")

        process.stdin = SimpleNamespace(write=write, flush=lambda: None, close=close)

        def start(resume_id=None):
            native._process = process
            native._remember_session(resume_id or "native-session-001")
            return native.session_id

        original = native.turn

        def turn(prompt, timeout=240):
            result = original(prompt, timeout=timeout)
            results.append(result)
            return result

        monkeypatch.setattr(native, "start", start)
        monkeypatch.setattr(native, "turn", turn)
        wires.append(SimpleNamespace(native=native, process=process, frames=frames, prompts=prompts))
        return native

    monkeypatch.setattr(room.bus.supervisor, "adapter_factory", make)
    return SimpleNamespace(module=module, wires=wires, results=results)


@pytest.fixture
def native_grant(native_wire, monkeypatch):
    active = threading.Event()
    emit = native_wire.module.NativeAdapter._emit

    def observe(native, event, *, required=False):
        if event.get("type") == "input_attempt":
            active.set()
        try:
            return emit(native, event, required=required)
        finally:
            if event.get("type") == "input_attempt":
                active.clear()

    monkeypatch.setattr(native_wire.module.NativeAdapter, "_emit", observe)
    return active


@pytest.mark.parametrize("deadline", ["bus", "harmony"])
def test_durable_input_time_crossing_is_typed_and_never_writes_native_input(room, monkeypatch, native_wire, native_grant, deadline):
    saved = room.prompt("durable-time-crossing-" + deadline)
    before_job = room.job(saved)
    with room.runtime.store._connection() as db:
        harmony_deadline = db.execute("SELECT deadline FROM goals WHERE id=?", (saved["goal"]["id"],)).fetchone()[0]
    if deadline == "harmony":
        # Keep the Bus valid beyond Harmony expiry, without changing its reservation.
        with room.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=? WHERE id=?", (harmony_deadline + 60, before_job["root_id"]))
    before_root = room.root(saved)
    expires = before_root["deadline"] if deadline == "bus" else harmony_deadline
    ledger = room.bus.supervisor.hosted
    module = sys.modules[type(ledger).__module__]
    clock, crossed = [None], []
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: time.time() if clock[0] is None else clock[0]))
    admitted = ledger._admitted_write

    class BeforeCommit:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def execute(self, sql, *args):
            result = self.db.execute(sql, *args)
            if sql.startswith("UPDATE avalokita_hosted_attempts SET input_attempted=1"):
                clock[0] = expires + 0.1
                crossed.append("after_update_before_commit")
            return result

    @contextmanager
    def cross(actor, job, generation, *, phase=None):
        try:
            with admitted(actor, job, generation, phase=phase) as db:
                if phase == "input" and native_grant.is_set() and not crossed:
                    if deadline == "bus":
                        clock[0] = expires + 0.1
                        crossed.append("after_admission_before_grant")
                        yield db
                    else:
                        yield BeforeCommit(db)
                else:
                    yield db
        finally:
            clock[0] = None

    monkeypatch.setattr(ledger, "_admitted_write", cross)
    room.tick()
    room.settle()
    assert crossed == (["after_admission_before_grant"] if deadline == "bus" else ["after_update_before_commit"])
    assert native_wire.results[0].status == "not_sent" and not native_wire.results[0].input_attempted
    assert not native_wire.wires[0].prompts
    attempt = room.attempts(saved)[0]
    assert attempt["state"] == "refused" and not attempt["input_attempted"] and attempt["native_turn_id"] is None
    assert room.job(saved)["state"] == "needs_human"
    assert room.bus.store.agent(room.actor)["state"] == "idle"
    assert room.root(saved)["used"] == 1 and room.root(saved)["deadline"] == before_root["deadline"]
    other = room.prompt("valid-after-durable-time-crossing-" + deadline)
    room.complete(other)
    assert sum(len(wire.prompts) for wire in native_wire.wires) == 1


def test_benign_progress_snapshot_race_defers_then_resumes_same_reservation_once(room, monkeypatch, native_wire, native_grant):
    saved = room.prompt("benign-progress-between-admission-and-grant")
    before_job, before_root = room.job(saved), room.root(saved)
    ledger = room.bus.supervisor.hosted
    admission, inserted = ledger._admission, []

    @contextmanager
    def progress(store, actor, job=None, generation=None, *, phase="claim"):
        with admission(store, actor, job, generation, phase=phase) as expected:
            if phase == "input" and native_grant.is_set() and not inserted:
                with store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    inserted.append(store._insert_event(db, "progress", actor, "user", "Selected peer progress update", job["root_id"],
                        {"job_id": job["id"], "event_id": job["event_id"]}))
            yield expected

    monkeypatch.setattr(ledger, "_admission", progress)
    room.tick()
    room.settle()
    assert len(inserted) == 1
    assert native_wire.results[0].status == "not_sent" and not native_wire.results[0].input_attempted
    assert not native_wire.wires[0].prompts
    assert room.job(saved)["state"] == "avalokita_paused"
    assert room.attempts(saved)[0]["state"] == "paused" and not room.attempts(saved)[0]["input_attempted"]
    assert room.root(saved)["used"] == 1 and room.bus.store.agent(room.actor)["state"] == "idle"
    room.complete(saved)
    assert tuple(room.job(saved)[key] for key in ("id", "event_id", "root_id", "agent_id")) == tuple(
        before_job[key] for key in ("id", "event_id", "root_id", "agent_id"))
    assert room.root(saved)["used"] == 1 and room.root(saved)["deadline"] == before_root["deadline"]
    assert len(room.attempts(saved)) == 1
    assert sum(len(wire.prompts) for wire in native_wire.wires) == 1
    room.tick()
    room.settle()
    assert sum(len(wire.prompts) for wire in native_wire.wires) == 1


@pytest.mark.parametrize("control", ["stopped", "paused"])
def test_real_native_adapter_wrapped_input_refusal_is_proven_unsent_per_job(room, monkeypatch, native_wire, control):
    saved = room.prompt("real-native-refusal-" + control)
    reached, release = threading.Event(), threading.Event()
    room.releases.append(release)
    emit = native_wire.module.NativeAdapter._emit

    def hold(native, event, *, required=False):
        if event.get("type") == "input_attempt" and not reached.is_set():
            reached.set()
            assert release.wait(4)
        return emit(native, event, required=required)

    monkeypatch.setattr(native_wire.module.NativeAdapter, "_emit", hold)
    room.tick()
    assert reached.wait(4)
    room.runtime.store.set_goal_state(saved["goal"]["id"], control)
    release.set()
    room.settle()
    assert native_wire.results[0].status == "not_sent" and not native_wire.results[0].input_attempted
    assert "lifecycle receipt" in native_wire.results[0].error
    assert not native_wire.wires[0].prompts
    assert all(frame["method"] != "turn/start" for frame in native_wire.wires[0].frames)
    assert room.root(saved)["used"] == 1
    attempt = room.attempts(saved)[0]
    assert not attempt["input_attempted"] and attempt["native_turn_id"] is None
    assert attempt["state"] == ("paused" if control == "paused" else "refused")
    assert room.bus.store.agent(room.actor)["state"] == "idle"
    other = room.prompt("real-native-valid-after-" + control)
    room.complete(other)
    assert sum(len(wire.prompts) for wire in native_wire.wires) == 1
    if control == "paused":
        room.runtime.store.set_goal_state(saved["goal"]["id"], "active")
        room.runtime.control("resume")
        room.complete(saved)
        assert room.root(saved)["used"] == 1
        assert room.attempts(saved)[0]["owner_id"] == attempt["owner_id"]
        assert room.attempts(saved)[0]["native_session_id"] == attempt["native_session_id"]
        assert sum(len(wire.prompts) for wire in native_wire.wires) == 2


@pytest.mark.parametrize("cause", ["stopped", "paused", "harmony_expired", "harmony_exhausted",
                                  "bus_expired", "bus_exhausted"])
def test_ineligible_goal_never_reserves_or_inputs_and_does_not_block_another_goal(room, cause):
    invalid = room.prompt("invalid-" + cause)
    goal_id = invalid["goal"]["id"]
    if cause in {"stopped", "paused"}:
        room.runtime.store.set_goal_state(goal_id, cause)
    elif cause.startswith("harmony"):
        with room.runtime.store._connection(write=True) as db:
            if cause.endswith("expired"):
                db.execute("UPDATE goals SET deadline=? WHERE id=?", (time.time() - 1, goal_id))
            else:
                db.execute("UPDATE goals SET used=budget WHERE id=?", (goal_id,))
    else:
        with room.bus.store.connection() as db:
            if cause.endswith("expired"):
                db.execute("UPDATE roots SET deadline=? WHERE id=?", (time.time() - 1, room.receipt(invalid)["root_id"]))
            else:
                db.execute("UPDATE roots SET used=budget WHERE id=?", (room.receipt(invalid)["root_id"],))
    before = room.root(invalid)
    valid = room.prompt("valid-after-" + cause)
    room.complete(valid)
    assert room.inputs() == 1
    assert room.root(invalid)["used"] == before["used"]
    assert room.attempts(invalid) == []
    assert room.job(invalid)["state"] != "completed"
    assert room.bus.store.agent(room.actor)["state"] == "idle"


def test_paused_goal_resumes_only_after_explicit_human_resume_with_saved_ids(room):
    saved = room.prompt("goal-pause-resume")
    before = room.job(saved), room.root(saved)
    room.runtime.store.set_goal_state(saved["goal"]["id"], "paused")
    room.tick()
    room.settle()
    assert room.inputs() == 0 and room.root(saved)["used"] == 0
    room.runtime.store.set_goal_state(saved["goal"]["id"], "active")
    room.runtime.control("resume")
    room.complete(saved)
    assert room.job(saved)["id"] == before[0]["id"]
    assert room.root(saved)["deadline"] == before[1]["deadline"]
    assert room.root(saved)["used"] == room.inputs() == 1


def test_workspace_pause_resumes_the_exact_saved_work_after_human_resume(room):
    saved = room.prompt("workspace-pause-resume")
    before = room.job(saved), room.root(saved)
    room.runtime.control("pause")
    room.tick()
    room.settle()
    assert room.inputs() == 0 and room.root(saved)["used"] == 0
    room.runtime.control("resume")
    room.complete(saved)
    assert room.job(saved)["id"] == before[0]["id"]
    assert room.root(saved)["deadline"] == before[1]["deadline"]
    assert room.root(saved)["used"] == room.inputs() == 1


def test_normal_service_restart_resumes_observed_native_session_once_for_saved_valid_job(room):
    first = room.prompt("establish-native")
    room.complete(first)
    room.bus.supervisor._stop.set()
    native_id = room.bus.store.agent(room.actor)["native_session_id"]
    saved = room.prompt("saved-valid-native")
    before = room.job(saved), room.root(saved), room.receipt(saved)
    old_instances = len(existing.FakeNative.instances)
    room.restart_normally()
    wait_for(lambda: room.job(saved)["state"] == "completed")
    assert len(existing.FakeNative.instances) == old_instances + 1
    assert existing.FakeNative.instances[-1].start_ids == [native_id]
    assert existing.FakeNative.instances[-1].session_id == native_id
    assert len(existing.FakeNative.instances[-1].prompts) == 1
    assert tuple(room.job(saved)[key] for key in ("id", "event_id", "root_id", "agent_id")) == tuple(
        before[0][key] for key in ("id", "event_id", "root_id", "agent_id"))
    assert room.root(saved)["used"] == 1
    assert room.root(saved)["deadline"] == before[1]["deadline"]
    assert room.receipt(saved)["bindings"] == before[2]["bindings"]


@pytest.mark.parametrize("boundary", ["before_begin", "after_start", "before_input"])
def test_stop_before_input_keeps_reservation_and_allows_another_goal(room, monkeypatch, boundary):
    saved = room.prompt("stop-" + boundary)
    reached, release = room.hold_before_input(monkeypatch, boundary)
    room.tick()
    assert reached.wait(4)
    assert room.root(saved)["used"] == 1
    room.runtime.store.set_goal_state(saved["goal"]["id"], "stopped")
    release.set()
    room.settle()
    assert room.inputs() == 0
    assert room.root(saved)["used"] == 1
    assert all(not item["input_attempted"] for item in room.attempts(saved))
    assert room.bus.store.agent(room.actor)["active_root"] is None
    assert room.bus.store.agent(room.actor)["state"] == "idle"
    valid = room.prompt("valid-after-stop-" + boundary)
    room.complete(valid)
    assert room.inputs() == 1


@pytest.mark.parametrize("boundary", ["before_begin", "after_start", "before_input"])
@pytest.mark.parametrize("budget", [1, 2])
def test_reserved_pause_resumes_same_saved_input_after_another_goal(room, monkeypatch, boundary, budget):
    # Budget 1 is the last reservation; 2 also exercises Bridge's paused -> queued path.
    saved = room.prompt("reserved-pause-" + boundary, budget=budget)
    original_job, original_root = room.job(saved), room.root(saved)
    reached, release = room.hold_before_input(monkeypatch, boundary)
    room.tick()
    assert reached.wait(4)
    room.runtime.store.set_goal_state(saved["goal"]["id"], "paused")
    release.set()
    room.settle()
    held = room.attempts(saved)
    assert room.job(saved)["state"] == "avalokita_paused"
    assert room.root(saved)["used"] == 1 and room.inputs() == 0
    assert all(item["state"] == "paused" and not item["input_attempted"] for item in held)
    assert room.bus.store.agent(room.actor)["active_root"] is None
    other = room.prompt("valid-during-reserved-pause-" + boundary)
    room.complete(other)
    assert room.inputs() == 1 and room.root(saved)["used"] == 1
    assert room.job(saved)["state"] == "avalokita_paused"
    room.runtime.store.set_goal_state(saved["goal"]["id"], "active")
    room.runtime.control("resume")
    room.complete(saved)
    final_job, final_root, final_attempt = room.job(saved), room.root(saved), room.attempts(saved)
    assert tuple(final_job[key] for key in ("id", "event_id", "root_id", "agent_id")) == tuple(
        original_job[key] for key in ("id", "event_id", "root_id", "agent_id"))
    assert final_root["used"] == 1 and final_root["deadline"] == original_root["deadline"]
    assert room.inputs() == 2 and len(final_attempt) == 1 and final_attempt[0]["input_attempted"] == 1
    assert len(room.bus.store.jobs()) == 2
    assert sum("JOB:" + original_job["id"] in prompt for native in existing.FakeNative.instances for prompt in native.prompts) == 1
    if held:
        assert final_attempt[0]["owner_id"] == held[0]["owner_id"]
        assert held[0]["native_session_id"] in {None, final_attempt[0]["native_session_id"]}
        if held[0]["generation"] != final_attempt[0]["generation"]:
            with pytest.raises(BusError):
                room.bus.supervisor.hosted.event(room.actor, final_job, held[0]["generation"],
                    {"type": "turn", "status": "accepted", "session_id": final_attempt[0]["native_session_id"], "turn_id": "stale-held-generation"})
            assert room.attempts(saved) == final_attempt
    room.tick()
    room.settle()
    assert room.inputs() == 2


def test_reserved_pause_survives_normal_service_restart_and_human_resume(room, monkeypatch):
    first = room.prompt("observed-before-held-restart")
    room.complete(first)
    native_id = room.bus.store.agent(room.actor)["native_session_id"]
    saved = room.prompt("held-normal-service-restart")
    before_job, before_root = room.job(saved), room.root(saved)
    reached, release = room.hold_before_input(monkeypatch, "before_input")
    room.tick()
    assert reached.wait(4)
    room.runtime.store.set_goal_state(saved["goal"]["id"], "paused")
    release.set()
    room.settle()
    before_attempt = room.attempts(saved)[0]
    room.stop_dispatcher()
    room.restart_normally()
    room.stop_dispatcher()
    assert room.inputs() == 1 and room.root(saved)["used"] == 1
    assert room.job(saved)["state"] == "avalokita_paused"
    room.runtime.store.set_goal_state(saved["goal"]["id"], "active")
    room.runtime.control("resume")
    room.complete(saved)
    assert existing.FakeNative.instances[-1].start_ids == [native_id]
    assert room.job(saved)["id"] == before_job["id"]
    assert room.root(saved)["used"] == 1 and room.root(saved)["deadline"] == before_root["deadline"]
    assert len(room.attempts(saved)) == 1
    assert room.attempts(saved)[0]["owner_id"] == before_attempt["owner_id"]
    assert room.attempts(saved)[0]["native_session_id"] == native_id
    assert room.inputs() == 2


@pytest.mark.parametrize("target", ["all", "actor"])
@pytest.mark.parametrize("boundary", ["after_start", "before_input"])
def test_operator_pause_fast_path_retains_the_last_reservation_until_human_resume(room, monkeypatch, target, boundary):
    saved = room.prompt("operator-pause-" + target)
    before_job, before_root = room.job(saved), room.root(saved)
    reached, release = room.hold_before_input(monkeypatch, boundary)
    room.tick()
    assert reached.wait(4)
    actor = room.actor if target == "actor" else "all"
    room.runtime.bridge.request("/api/control", {"action": "pause", "agent": actor})
    release.set()
    room.settle()
    assert room.inputs() == 0 and room.root(saved)["used"] == 1
    assert room.job(saved)["state"] == "avalokita_paused"
    assert room.attempts(saved)[0]["state"] == "paused"
    assert not room.attempts(saved)[0]["input_attempted"]
    room.tick()
    room.settle()
    assert room.inputs() == 0
    room.runtime.bridge.request("/api/control", {"action": "resume", "agent": actor})
    room.complete(saved)
    assert room.job(saved)["id"] == before_job["id"]
    assert room.root(saved)["used"] == room.inputs() == 1
    assert room.root(saved)["deadline"] == before_root["deadline"]
    assert len(room.attempts(saved)) == 1
    other = room.prompt("valid-after-operator-resume-" + target)
    room.complete(other)
    assert room.inputs() == 2


def test_operator_stop_at_input_observer_is_terminal_without_refund_or_replay(room, monkeypatch):
    saved = room.prompt("operator-stop-before-input")
    before_job, before_root = room.job(saved), room.root(saved)
    reached, release = room.hold_before_input(monkeypatch, "before_input")
    room.tick()
    assert reached.wait(4)
    room.runtime.bridge.request("/api/control", {"action": "stop", "agent": room.actor})
    release.set()
    room.settle()
    assert room.job(saved)["state"] == "avalokita_stopped"
    assert room.attempts(saved)[0]["state"] == "refused" and not room.attempts(saved)[0]["input_attempted"]
    assert room.root(saved)["used"] == 1 and room.inputs() == 0
    room.runtime.bridge.request("/api/control", {"action": "resume", "agent": room.actor})
    room.tick()
    room.settle()
    assert room.job(saved)["id"] == before_job["id"] and room.job(saved)["state"] == "avalokita_stopped"
    assert room.root(saved)["used"] == 1 and room.root(saved)["deadline"] == before_root["deadline"]
    assert room.inputs() == 0
    other = room.prompt("valid-after-operator-stopped-input")
    room.complete(other)
    assert room.inputs() == 1


@pytest.mark.parametrize("boundary", ["before_begin", "after_start"])
def test_expiry_after_claim_releases_only_the_unattempted_job(room, monkeypatch, boundary):
    saved = room.prompt("reserved-expiry-" + boundary)
    reached, release = room.hold_before_input(monkeypatch, boundary)
    room.tick()
    assert reached.wait(4)
    with room.bus.store.connection() as db:
        db.execute("UPDATE roots SET deadline=? WHERE id=?", (time.time() - 1, room.receipt(saved)["root_id"]))
    release.set()
    room.settle()
    assert room.inputs() == 0 and room.root(saved)["used"] == 1
    assert room.bus.store.agent(room.actor)["state"] == "idle"
    other = room.prompt("valid-after-reserved-expiry-" + boundary)
    room.complete(other)
    assert room.inputs() == 1


def test_last_reserved_turn_is_not_recharged_when_harmony_reaches_its_budget(room):
    saved = room.prompt("last-reserved")
    job = room.claim_prepared()
    assert job and room.root(saved)["used"] == 1
    with room.runtime.store._connection(write=True) as db:
        db.execute("UPDATE goals SET used=budget WHERE id=?", (saved["goal"]["id"],))
    room.bus.supervisor._stop.clear()
    room.bus.supervisor._run_hosted(room.bus.store.agent(room.actor), job)
    assert room.job(saved)["state"] == "completed"
    assert room.inputs() == room.root(saved)["used"] == 1


def test_result_settles_after_stop_when_the_exact_input_was_already_accepted(room, monkeypatch):
    gate = threading.Event()
    room.releases.append(gate)
    monkeypatch.setattr(existing.FakeNative, "gates", {"codex": gate})
    monkeypatch.setattr(existing.FakeNative, "controlled_gates", {"codex"})
    saved = room.prompt("stop-after-grant")
    room.tick()
    wait_for(lambda: existing.FakeNative.instances and existing.FakeNative.instances[0].entered.is_set())
    assert room.attempts(saved)[0]["input_attempted"] == 1
    room.runtime.store.set_goal_state(saved["goal"]["id"], "stopped")
    gate.set()
    room.settle()
    assert room.job(saved)["state"] == room.attempts(saved)[0]["state"] == "completed"
    assert room.root(saved)["used"] == room.inputs() == 1


def test_normal_restart_does_not_replay_an_attempted_unknown_input(room, monkeypatch):
    monkeypatch.setattr(existing.FakeNative, "unknown", {"codex"})
    saved = room.prompt("unknown-before-restart")
    room.pump(lambda: bool(room.attempts(saved)) and room.attempts(saved)[0]["state"] == "delivery_unknown")
    before = room.job(saved), room.root(saved), room.inputs(), len(existing.FakeNative.instances)
    room.restart_normally()
    room.bus.supervisor.wake()
    room.stop_dispatcher()
    assert room.inputs() == before[2] == 1
    assert len(existing.FakeNative.instances) == before[3]
    assert room.job(saved)["id"] == before[0]["id"]
    assert room.root(saved)["used"] == before[1]["used"] == 1
    assert room.attempts(saved)[0]["state"] == "delivery_unknown"


@pytest.mark.parametrize("binding", ["native", "owner", "generation"])
def test_rebound_owner_identity_cannot_admit_a_saved_job(room, binding):
    saved = room.prompt("rebound-" + binding)
    with room.bus.store.connection() as db:
        if binding == "native":
            db.execute("UPDATE agents SET native_session_id='foreign-native' WHERE id=?", (room.actor,))
        elif binding == "owner":
            db.execute("UPDATE avalokita_hosted_owners SET owner_id='avalokita-hosted/1:foreign' WHERE actor=?", (room.actor,))
        else:
            db.execute("UPDATE avalokita_hosted_owners SET generation=generation+1 WHERE actor=?", (room.actor,))
    room.tick()
    room.settle()
    assert room.inputs() == 0 and room.root(saved)["used"] == 0
    assert room.attempts(saved) == []


def test_prepared_claim_never_switches_to_a_new_higher_priority_candidate(room):
    selected = room.prompt("selected-before-priority-change")
    other = room.prompt("other-before-priority-change")
    selected_job, other_job = room.job(selected), room.job(other)
    with room.runtime._hosted_admission(room.bus.store, room.actor) as expected:
        with room.bus.store.connection() as db:
            db.execute("UPDATE jobs SET priority=1000 WHERE id=?", (other_job["id"],))
        result = room.bus.store.claim(room.actor, expected=expected)
    assert result is None or result["id"] == selected_job["id"]
    assert room.root(other)["used"] == 0
    assert room.root(selected)["used"] == int(result is not None)


def test_prepared_claim_rejects_selected_event_mutated_before_the_writer(room):
    saved = room.prompt("event-mutated-after-snapshot")
    job = room.job(saved)
    with room.runtime._hosted_admission(room.bus.store, room.actor) as expected:
        with room.bus.store.connection() as db:
            metadata = json.loads(db.execute("SELECT metadata FROM events WHERE id=?", (job["event_id"],)).fetchone()[0])
            metadata["bindings"][room.actor] = ["codex", "avalokita-hosted/1:foreign"]
            db.execute("UPDATE events SET metadata=? WHERE id=?", (json.dumps(metadata), job["event_id"]))
        result = room.bus.store.claim(room.actor, expected=expected)
    assert result is None
    assert room.root(saved)["used"] == room.inputs() == 0


def test_prepared_child_claim_rejects_an_ancestor_attempt_changed_before_the_writer(room, monkeypatch):
    actors = room.configure("codex", "claude")
    gate = threading.Event()
    room.releases.append(gate)
    monkeypatch.setattr(existing.FakeNative, "gates", {"codex": gate})
    monkeypatch.setattr(existing.FakeNative, "controlled_gates", {"codex"})
    saved = room.prompt("ancestor-proof", budget=3, collaborator_ids=list(actors.values()))
    room.tick()
    wait_for(lambda: existing.FakeNative.instances and existing.FakeNative.instances[0].entered.is_set())
    token = (room.bus.store.credentials / (room.actor + ".token")).read_text(encoding="utf-8")
    context = room.runtime.bridge.request("/api/avalokita/context", token=token)
    result = room.runtime.peer(token, {**context, "operation": "send-keys", "recipient": actors["claude"],
        "body": "Review this selected shared work", "kind": "critique", "idempotency_key": "ancestor-child"})
    child = next(job for job in room.bus.store.jobs() if job["event_id"] == result["event"]["id"])
    parent_id = context["parent_job_id"]
    with room.runtime._hosted_admission(room.bus.store, actors["claude"]) as expected:
        with room.bus.store.connection() as db:
            generation = db.execute("SELECT generation FROM avalokita_hosted_attempts WHERE job_id=?", (parent_id,)).fetchone()[0]
            db.execute("UPDATE avalokita_hosted_attempts SET generation=generation+1 WHERE job_id=?", (parent_id,))
        try:
            claimed = room.bus.store.claim(actors["claude"], expected=expected)
        finally:
            with room.bus.store.connection() as db:
                db.execute("UPDATE avalokita_hosted_attempts SET generation=? WHERE job_id=?", (generation, parent_id))
    assert claimed is None
    assert next(job for job in room.bus.store.jobs() if job["id"] == child["id"])["state"] == "queued"
    assert room.root(saved)["used"] == room.inputs() == 1
    gate.set()
    room.settle()


def test_two_claimers_reserve_one_prepared_input_only_once(room):
    saved = room.prompt("concurrent-exact-claim")
    results, errors = [], []
    barrier = threading.Barrier(3)
    with room.runtime._hosted_admission(room.bus.store, room.actor) as expected:
        def claim():
            try:
                barrier.wait(timeout=3)
                results.append(room.bus.store.claim(room.actor, expected=expected))
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=3)
        for worker in workers:
            worker.join(timeout=4)
            assert not worker.is_alive()
    assert not errors
    assert len([result for result in results if result]) == 1
    assert {result["id"] for result in results if result} == {room.job(saved)["id"]}
    assert room.root(saved)["used"] == 1


def test_dispatching_receipt_defers_then_reconciles_the_same_saved_input(room):
    saved = room.prompt("dispatching-reconcile")
    before = room.job(saved), room.root(saved)
    with room.runtime.bridge._db() as db:
        db.execute("UPDATE dispatch SET state='dispatching' WHERE goal_id=?", (saved["goal"]["id"],))
    room.tick()
    room.settle()
    assert room.inputs() == 0 and room.root(saved)["used"] == 0
    again = room.prompt("dispatching-reconcile")
    assert again["goal"]["id"] == saved["goal"]["id"]
    room.complete(saved)
    assert room.job(saved)["id"] == before[0]["id"]
    assert room.root(saved)["id"] == before[1]["id"]
    assert room.root(saved)["deadline"] == before[1]["deadline"]
    assert room.inputs() == room.root(saved)["used"] == 1
