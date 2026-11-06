"""Saved hosted authority at normal startup; only disposable existing FakeNative."""
import json
from pathlib import Path
import sys
import threading

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import hosted as hosted_module
from bridge import BusStore
from server import Runtime, Server
import sidecar_registry as registry
import test_hosted as hosted_fixture
import test_service_startup as startup_fixture

OBSERVATIONS = []


def authority_snapshot(runtime, store, saved):
    goal_id = saved["goal"]["id"]
    goal = runtime.store.get_goal(goal_id)
    receipt = runtime.bridge.dispatches()[goal_id]
    with store.connection() as db:
        root = dict(db.execute("SELECT * FROM roots WHERE id=?", (receipt["root_id"],)).fetchone())
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE root_id=?", (root["id"],))]
        attempts = [dict(row) for row in db.execute("SELECT * FROM avalokita_hosted_attempts WHERE root_id=?", (root["id"],))]
        owners = [dict(row) for row in db.execute("SELECT * FROM avalokita_hosted_owners")]
        event = db.execute("SELECT metadata FROM events WHERE id=?", (receipt["event_id"],)).fetchone()
        metadata = json.loads(event[0])
        agents = [{key: row[key] for key in ("id", "provider", "native_address", "native_session_id", "state", "generation", "active_root")}
                  for row in db.execute("SELECT * FROM agents")]
        bus_paused = db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0]
    return {"goal": {key: goal[key] for key in ("id", "state", "budget", "used", "deadline")},
            "root": root, "jobs": jobs, "attempts": attempts, "owners": owners, "agents": agents,
            "bindings": metadata["bindings"], "original_human_goal": metadata["avalokita_goal_id"],
            "parent_job_id": metadata.get("parent_job_id"), "parent_event_id": metadata.get("parent_event_id"),
            "parent_root_id": metadata.get("parent_root_id"),
            "controls": {"local_paused": runtime.bridge.paused(), "bus_paused": bus_paused},
            "fake_provider_starts": sum(len(item.start_ids) for item in hosted_fixture.FakeNative.instances),
            "fake_provider_inputs": sum(len(item.prompts) for item in hosted_fixture.FakeNative.instances)}


def test_saved_human_root_stopped_never_attempted_job_cannot_run_on_normal_service_start(tmp_path, monkeypatch):
    """Persisted queued fixture from test_hosted:410 plus native-dispatch root stop."""
    record = {"case": "saved_hosted_queued_human_root_stopped", "stages": {}}
    OBSERVATIONS.append(record)
    before_threads = set(threading.enumerate())
    fixture = hosted_fixture.HostedTests("test_standalone_live_dispatcher_cannot_claim_persisted_hosted_work")
    monkeypatch.setattr(registry, "REGISTRY", tmp_path / "registry")
    register = registry.register_workspace

    def register_temp(root, *, runtime, workspace_guard):
        return register(root, runtime=runtime, registry_path=tmp_path / "registry", workspace_guard=workspace_guard)

    monkeypatch.setattr(hosted_module, "register_workspace", register_temp)
    runtimes, servers, buses = [], [], []
    fixture.setUp()
    runtimes.append(fixture.runtime)
    try:
        actor = fixture.configure("codex")["sessions"][0]["agent_id"]
        original_bus = fixture.bus
        buses.append(original_bus)
        original_bus.supervisor._stop.set()
        original_bus.supervisor.wake()
        original_bus.supervisor._dispatcher.join(timeout=3)
        assert not original_bus.supervisor._dispatcher.is_alive()
        saved = fixture.prompt("saved-root-stop", recipient_ids=[actor], budget=1, ttl=30)
        fixture.runtime.store.set_goal_state(saved["goal"]["id"], "stopped")
        before = authority_snapshot(fixture.runtime, original_bus.store, saved)
        record["stages"]["before_close"] = before
        assert before["goal"]["state"] == "stopped"
        assert before["root"]["used"] == 0 and before["root"]["budget"] == 1
        assert len(before["jobs"]) == 1 and before["jobs"][0]["state"] == "queued"
        assert before["attempts"] == [] and hosted_fixture.FakeNative.instances == []
        fixture.runtime.hosted.close()
        assert not original_bus.supervisor._dispatcher.is_alive()

        # Observe real claims without changing their result or authority checks.
        claim = BusStore.claim
        probes, two_probes = [], threading.Event()

        def observed_claim(store, agent_id, **kwargs):
            result = claim(store, agent_id, **kwargs)
            if store.workspace == fixture.workspace and agent_id == actor:
                probes.append({"job_id": result["id"] if result else None})
                if len(probes) >= 2:
                    two_probes.set()
            return result

        monkeypatch.setattr(BusStore, "claim", observed_claim)
        runtime = Runtime(fixture.workspace, discovery=False, native_ipc=False,
                          hosted_factory=hosted_fixture.FakeNative,
                          hosted_discovery=[{"provider": "codex", "installed": True, "managed_supported": True}])
        runtimes.append(runtime)
        fixture.runtime = runtime
        assert runtime.native_dispatcher is None
        server = Server(fixture.workspace, runtime=runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        servers.append((server, thread))
        thread.start()
        startup_fixture.wait_for(lambda: runtime.hosted.service_snapshot()["state"] == "ready")
        bus = runtime.hosted.server
        buses.append(bus)
        assert two_probes.wait(5), "The real Supervisor did not complete two claim observations"
        after = authority_snapshot(runtime, bus.store, saved)
        record["stages"]["after_normal_startup"] = after
        record["claim_probes"] = probes
        record["service"] = runtime.hosted.service_snapshot()
        assert after["goal"] == before["goal"]
        assert after["bindings"] == before["bindings"] and after["controls"] == before["controls"]
        assert [(job["id"], job["event_id"], job["root_id"], job["agent_id"]) for job in after["jobs"]] == [
            (job["id"], job["event_id"], job["root_id"], job["agent_id"]) for job in before["jobs"]]
        assert after["fake_provider_inputs"] == 0, "Stopped human root reached the existing fake provider input route"
        assert after["attempts"] == [] and after["root"]["used"] == 0
    finally:
        for item in hosted_fixture.FakeNative.instances:
            item.interrupt()
        for server, thread in servers:
            server.server_close()
            thread.join(timeout=4)
            assert not thread.is_alive()
        for runtime in runtimes:
            runtime.begin_closing()
            runtime.stop_peer_reconciler()
            runtime.device_discovery.stop()
            runtime.hosted.close()
        record["cleanup"] = {"active_work": [runtime._active_work for runtime in runtimes],
                             "remaining_workers": [list(bus.supervisor._workers) for bus in buses],
                             "dispatchers_alive": [bus.supervisor._dispatcher.is_alive() for bus in buses],
                             "listener_filenos": [bus.fileno() for bus in buses] + [server.fileno() for server, _ in servers]}
        fixture.tearDown()
        startup_fixture.wait_for(lambda: not [thread for thread in threading.enumerate() if thread not in before_threads], timeout=4)
        assert record["cleanup"]["active_work"] == [0] * len(runtimes)
        assert not any(record["cleanup"]["remaining_workers"] + record["cleanup"]["dispatchers_alive"])
        assert set(record["cleanup"]["listener_filenos"]) == {-1}
