"""Synthetic Runtime wiring only; no daemon, native binding, app or model I/O."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest.mock import Mock, call, patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from adapters.canonical_reason import CanonicalReasonTransport
from adapters.reason_engine import ReasonEngineError
from bridge import BridgeError, BusStore, is_hosted
from hosted_runtime import HostedLedger
from native_dispatch import ENVELOPES, TABLE, TRANSPORTS, NativeDispatcher, digest
from server import Runtime


class FakeReasonTransport(CanonicalReasonTransport):
    """Only exact synthetic metadata; never an executable or native binding."""
    def __init__(self, targets):
        self.targets = targets
        self.bindings = dict(targets)
        self.selections = []
        self.process_factory: Mock = Mock(side_effect=AssertionError("Native I/O is forbidden"))

    def selection(self, actor_id: str, native_id: str, workspace: Path, *, check_files: bool = False) -> dict | None:
        self.selections.append((actor_id, native_id, workspace))
        if actor_id not in self.targets:
            return None
        if self.targets[actor_id] != native_id:
            raise ReasonEngineError("Synthetic exact target changed")
        return {"transport": "reason"}


class RuntimeReasonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.actors: list[dict[str, Any]] = [{"id": actor, "provider": "codex", "mode": "native_attached", "state": "idle",
                        "native_session_id": "native-" + actor, "role": "peer", "capabilities": []}
                       for actor in ("synthetic-a", "synthetic-b", "synthetic-c")]
        self.a, self.b, self.c = [actor["id"] for actor in self.actors]
        self.transport = FakeReasonTransport({actor["id"]: actor["native_session_id"]
                                              for actor in (self.actors[0], self.actors[2])})
        self.native_factory: Mock = Mock(side_effect=AssertionError("Original IPC I/O is forbidden"))
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=True,
                               native_factory=self.native_factory, reason_transport=self.transport)
        self.addCleanup(self.runtime.hosted.close)
        dispatcher = self.runtime.native_dispatcher
        assert dispatcher is not None
        self.dispatcher = dispatcher
        self.dispatcher.available = Mock(return_value={"state": "pending_capability"})
        self.dispatcher.drain = Mock(return_value=[])
        self.dispatcher.reconcile_completed = Mock(return_value=[])
        self.dispatcher.reserve = Mock(return_value={"dispatch_id": "synthetic-dispatch", "state": "claimed"})
        self.dispatcher.deliver = Mock(return_value={"state": "native_accepted", "native_result_verified": False})
        self.observed: dict[str, Any] = {"connection": {"state": "connected"}, "state": {"agents": deepcopy(self.actors), "paused": False}}
        self.observe = self.patch(self.runtime.bridge, "observe", return_value=self.observed)
        self.reconcile = self.patch(self.runtime.bridge, "reconcile_dispatches", return_value={})
        self.outputs = self.patch(self.runtime.bridge, "selected_outputs", return_value=[])
        self.dispatch = self.patch(self.runtime.bridge, "dispatch", side_effect=self.queued)

    def tearDown(self):
        self.native_factory.assert_not_called()
        self.transport.process_factory.assert_not_called()
        self.assertFalse((self.workspace / ".sharebus").exists())

    def patch(self, target, name, **kwargs):
        change = patch.object(target, name, **kwargs)
        self.addCleanup(change.stop)
        return change.start()

    @staticmethod
    def queued(goal, text, **kwargs):
        return {"goal_id": goal["id"], "root_id": "synthetic-root", "state": "queued", **kwargs}

    def prompt(self, **kwargs):
        return self.runtime.prompt({"text": "Synthetic selected work", "idempotency_key": "synthetic-human",
                                    "recipient_ids": [self.a], **kwargs})

    def selected_result(self):
        goal = self.runtime.store.new_goal("Synthetic existing human goal", idempotency_key="synthetic-completed")
        event = {"id": "synthetic-result", "sender": self.a, "provider": "codex", "role": "peer",
                 "native_session_id": self.actors[0]["native_session_id"], "capabilities": [],
                 "goal_id": goal["id"], "source_job_id": "synthetic-job", "created_at": time.time(),
                 "kind": "result", "completion_verified": True, "recipient": "user", "body": "Synthetic selected final"}
        self.outputs.return_value = [event]
        return goal, event

    def test_trusted_transport_is_forwarded_without_work_or_native_io(self):
        self.assertIs(self.dispatcher.reason_transport, self.transport)
        self.assertEqual(self.transport.selections, [])
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        disabled = Runtime(self.workspace, discovery=False, native_ipc=False, reason_transport=self.transport)
        self.addCleanup(disabled.hosted.close)
        self.assertIsNone(disabled.native_dispatcher)
        self.assertEqual(self.transport.selections, [])

    def test_state_readiness_checks_each_exact_actor_and_native_id(self):
        available = self.patch(self.dispatcher, "available_for", wraps=self.dispatcher.available_for)
        state = self.runtime.state()
        readiness = {actor["id"]: actor["readiness"]["can_dispatch"] for actor in state["participants"]}
        self.assertEqual(readiness, {self.a: True, self.b: False, self.c: True})
        self.assertEqual(available.call_args_list, [call(actor["id"], actor["native_session_id"]) for actor in self.actors])
        self.dispatcher.available.assert_called_once_with()
        self.assertEqual(state["goals"], [])
        self.assertEqual(state["messages"], [])
        self.dispatcher.deliver.assert_not_called()

    def test_prompt_preserves_initial_recipient_and_explicit_collaborator_roster(self):
        result = self.prompt(collaborator_ids=[self.a, self.c], require_native_ipc=True)
        selected = self.dispatch.call_args.kwargs
        self.assertEqual(selected["recipient_ids"], [self.a])
        self.assertEqual(selected["collaborator_ids"], [self.a, self.c])
        self.assertEqual(selected["native_ipc_ids"], [self.a, self.c])
        self.dispatcher.reserve.assert_called_once_with(result["goal"]["id"], self.a)
        self.dispatcher.deliver.assert_called_once_with("synthetic-dispatch")
        self.dispatcher.available.assert_not_called()
        self.assertEqual(result["native_delivery"], [{"state": "native_accepted", "native_result_verified": False}])
        self.assertEqual([message["actor"] for message in self.runtime.store.snapshot()["messages"]], ["human"])

    def test_unconfigured_or_rebound_target_cannot_create_a_human_goal(self):
        with self.assertRaises(BridgeError) as rejected:
            self.prompt(recipient_ids=[self.b], require_native_ipc=True)
        self.assertEqual(rejected.exception.status, 409)
        self.observed["state"]["agents"][0]["native_session_id"] = "synthetic-rebound"
        self.dispatcher.available.reset_mock()
        with self.assertRaises(BridgeError) as rejected:
            self.prompt(require_native_ipc=True)
        self.assertEqual(rejected.exception.status, 409)
        self.dispatcher.available.assert_not_called()
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.dispatch.assert_not_called()
        self.dispatcher.reserve.assert_not_called()
        self.dispatcher.deliver.assert_not_called()

    def test_selected_result_is_imported_before_bounded_transport_reconciliation(self):
        goal, event = self.selected_result()

        def settled(*, limit):
            self.assertEqual(limit, 8)
            messages = self.runtime.store.snapshot()["messages"]
            self.assertEqual([(message["actor"], message["body"]) for message in messages if message["kind"] == "result"],
                             [(self.a, event["body"])])
            self.reconcile.assert_called_once_with()
            return [{"state": "result_completed", "native_result_verified": True}]

        self.dispatcher.reconcile_completed.side_effect = settled
        state = self.runtime.state()
        self.dispatcher.reconcile_completed.assert_called_once_with(limit=8)
        self.assertEqual([message["root_id"] for message in state["messages"] if message["kind"] == "result"], [goal["id"]])
        self.dispatcher.reserve.assert_not_called()
        self.dispatcher.deliver.assert_not_called()

    def test_transport_ack_alone_does_not_publish_a_result(self):
        self.dispatcher.reconcile_completed.return_value = [{"state": "native_accepted", "native_result_verified": False}]
        self.assertEqual(self.runtime.state()["messages"], [])
        self.assertEqual(self.runtime.store.snapshot()["messages"], [])

    def test_transport_reconciliation_failure_preserves_canonical_selected_result(self):
        _, event = self.selected_result()
        self.dispatcher.reconcile_completed.side_effect = OSError("Synthetic private observation failure")
        state = self.runtime.state()
        self.assertEqual([message["body"] for message in state["messages"] if message["kind"] == "result"], [event["body"]])
        self.dispatcher.deliver.assert_not_called()

    def test_summary_remains_read_only_with_reason_configured(self):
        before = self.runtime.store.snapshot()
        summary = self.runtime.summary()
        self.assertEqual(self.runtime.store.snapshot(), before)
        self.assertEqual(summary["counts"]["goals"], 0)
        self.assertEqual(summary["counts"]["messages"], 0)
        self.assertEqual(self.transport.selections, [])
        self.observe.assert_not_called()
        self.reconcile.assert_not_called()
        self.outputs.assert_not_called()
        self.dispatcher.available.assert_not_called()
        self.dispatcher.drain.assert_not_called()
        self.dispatcher.reconcile_completed.assert_not_called()

    def test_renderer_payload_cannot_supply_transport_or_binding_configuration(self):
        for field in ("reason_transport", "engine_path", "engine_sha256", "bindings"):
            with self.subTest(field=field), self.assertRaises(BridgeError):
                self.prompt(**{field: {"synthetic": "untrusted"}})
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertEqual(self.transport.selections, [])
        self.dispatch.assert_not_called()
        self.dispatcher.reserve.assert_not_called()
        self.dispatcher.deliver.assert_not_called()

    def pause_while_blocked(self, operation, entered, release):
        errors = []
        paused = threading.Event()

        def run():
            try:
                operation()
            except Exception as error:
                errors.append(error)

        def pause():
            try:
                self.runtime.control("pause")
                paused.set()
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        control = threading.Thread(target=pause)
        try:
            worker.start()
            self.assertTrue(entered.wait(10), "Synthetic slow phase was not reached")
            control.start()
            self.assertTrue(paused.wait(3), "Pause waited for the blocked synthetic Reason phase")
            self.assertFalse(release.is_set())
        finally:
            release.set()
            worker.join(timeout=10)
            if control.ident is not None:
                control.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(control.is_alive())
        self.assertEqual(errors, [])

    def block_reason_delivery(self):
        entered, release = threading.Event(), threading.Event()
        self.dispatcher.reserve.return_value = {"dispatch_id": "synthetic-dispatch", "state": "claimed", "transport": "reason"}

        def blocked(_dispatch_id):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Synthetic Reason phase was not released")
            return {"state": "native_accepted", "native_result_verified": False}

        self.dispatcher.deliver.side_effect = blocked
        return entered, release

    def test_pause_can_finish_while_direct_reason_delivery_is_blocked(self):
        entered, release = self.block_reason_delivery()
        self.pause_while_blocked(self.prompt, entered, release)

    def test_recovered_reason_delivery_does_not_keep_an_outer_runtime_lock(self):
        goal = self.runtime.store.new_goal("Synthetic selected work", idempotency_key="synthetic-human")
        self.patch(self.runtime.bridge, "dispatches", return_value={goal["id"]: {
            "state": "waiting", "recipient_ids": [self.a], "request_budget": 24, "request_ttl": 3600}})
        entered, release = self.block_reason_delivery()
        self.pause_while_blocked(self.runtime._recover_waiting, entered, release)

    def test_pause_can_finish_while_reason_settlement_is_blocked(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(*, limit):
            self.assertEqual(limit, 8)
            entered.set()
            if not release.wait(10):
                raise AssertionError("Synthetic settlement was not released")
            return []

        self.dispatcher.reconcile_completed.side_effect = blocked
        self.pause_while_blocked(self.runtime.state, entered, release)


class RuntimeReasonPeerGateTests(unittest.TestCase):
    """Durable synthetic parents and metadata stubs; no daemon or native I/O."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.bus = BusStore(self.workspace)
        self.sender, self.token = self.bus.register("codex", "Synthetic sender", [], "external_attached",
                                                   native_session_id="synthetic-parent-native")
        self.targets = [self.bus.register(provider, "Synthetic " + provider, [], "native_attached",
                                          native_session_id="synthetic-" + provider)[0]
                        for provider in ("claude", "antigravity")]
        hosted = HostedLedger(self.bus, execution_ready=lambda: True)
        hosted.configure(["codex"], [{"provider": "codex", "installed": True, "managed_supported": True}])
        self.targets.append(next(actor for actor in self.bus.agents() if is_hosted(actor)))
        self.bus.configure_auto(enabled=True, suspended=False)
        for actor in self.targets[:2]:
            self.bus.update_agent(actor["id"], channel_verified=True)
            self.bus.observe_auto(actor["id"], "watching", "ready")
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False)
        self.addCleanup(self.runtime.hosted.close)
        self.change(self.runtime.bridge, "_runtime", return_value={"workspace_id": self.bus.workspace_id})
        self.change(self.runtime.bridge, "request", side_effect=self.metadata_request)
        self.transport = FakeReasonTransport({self.sender["id"]: self.sender["native_session_id"]})
        self.native_factory: Mock = Mock(side_effect=AssertionError("Native IPC is forbidden"))
        self.bridge_send = self.change(self.runtime.bridge, "peer_send", wraps=self.runtime.bridge.peer_send)

    def tearDown(self):
        self.native_factory.assert_not_called()
        self.transport.process_factory.assert_not_called()

    def change(self, target, name, **kwargs):
        change = patch.object(target, name, **kwargs)
        self.addCleanup(change.stop)
        return change.start()

    def metadata_request(self, path, payload=None, *, token=None):
        self.assertEqual(path, "/api/state")
        actors = self.bus.agents()
        for actor in actors:
            actor.update(wake_supported=True, wake_channel_available=True)
            if is_hosted(actor):
                actor.update(hosted_enabled=True, hosted_owner_verified=True, hosted_blocked=False)
        with self.bus.connection() as db:
            observations = [dict(row) for row in db.execute("SELECT * FROM auto_observations")]
        state = {"agents": actors, "paused": False, "auto": {"active": True, "suspended": False,
                                                             "observations": observations}}
        if token is not None:
            actor_id = self.bus.authenticate(token)
            state["own_agent"] = next((actor for actor in actors if actor["id"] == actor_id), None)
        return state

    def admit(self, transport="reason"):
        self.dispatcher = NativeDispatcher(self.runtime.bridge, harmony_store=self.runtime.store,
            adapter_factory=self.native_factory, reason_transport=self.transport if transport == "reason" else None)
        self.runtime.native_dispatcher = self.dispatcher
        self.runtime.reason_transport = self.dispatcher.reason_transport
        self.goal = self.runtime.store.new_goal("Synthetic original human scope", budget=6, ttl=600,
                                                idempotency_key="synthetic-peer-root")
        self.receipt = self.runtime.bridge.dispatch(self.goal, self.goal["prompt"],
            recipient_ids=[self.sender["id"]], collaborator_ids=[self.sender["id"], *[actor["id"] for actor in self.targets]],
            native_ipc_ids=[self.sender["id"]], budget=6, ttl=600)
        self.assertEqual(self.receipt["state"], "queued")
        with self.dispatcher.admission() as (db, _, _):
            parent = dict(db.execute("SELECT * FROM jobs WHERE agent_id=?", (self.sender["id"],)).fetchone())
            human = db.execute("SELECT * FROM events WHERE id=?", (parent["event_id"],)).fetchone()
            prompt = self.dispatcher._prompt(goal_id=self.goal["id"], actor=self.sender["id"],
                native_id=self.sender["native_session_id"], root_id=parent["root_id"], job_id=parent["id"],
                event_id=parent["event_id"], human_body=human["body"], roster=[])
            self.parent = {"dispatch_id": "synthetic-parent-dispatch", "job_id": parent["id"],
                           "event_id": parent["event_id"], "root_id": parent["root_id"]}
            now = time.time()
            db.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                self.parent["dispatch_id"], parent["id"], self.goal["id"], self.sender["id"], self.sender["native_session_id"],
                "user" if transport == "reason" else "synthetic-native-source", parent["root_id"], parent["event_id"],
                digest(prompt), self.goal["deadline"], "transport_accepted" if transport == "reason" else "native_accepted", "{}", now))
            db.execute(f"INSERT INTO {TRANSPORTS} VALUES(?,?)", (self.parent["dispatch_id"], json.dumps({"transport": transport})))
            db.execute(f"INSERT INTO {ENVELOPES} VALUES(?,?,?)", (self.parent["dispatch_id"], prompt, self.dispatcher._input_hash(db, human)))
            db.execute("UPDATE jobs SET state='awaiting_native_reply',started_at=? WHERE id=?", (now, parent["id"]))
            db.execute("UPDATE agents SET state='awaiting_native_reply',active_root=? WHERE id=?", (parent["root_id"], self.sender["id"]))
            db.execute("UPDATE roots SET used=1 WHERE id=?", (parent["root_id"],))

    def effects(self):
        with self.bus.connection() as db:
            bus = (tuple(db.execute("SELECT budget,used,deadline FROM roots WHERE id=?", (self.parent["root_id"],)).fetchone()),
                   db.execute("SELECT count(*) FROM jobs").fetchone()[0], db.execute("SELECT count(*) FROM events").fetchone()[0])
        with self.runtime.bridge._db() as db:
            peers = db.execute("SELECT count(*) FROM peer_dispatch").fetchone()[0]
        with self.runtime.store._connection() as db:
            budget = tuple(db.execute("SELECT budget,used,deadline FROM goals WHERE id=?", (self.goal["id"],)).fetchone())
        return bus, peers, budget

    def peer(self, recipient, **overrides):
        return self.runtime.peer(self.token, {"operation": "send-keys", "root_id": self.goal["id"],
            "recipient": recipient, "body": "Synthetic selected review", "kind": "message", "idempotency_key": "synthetic-peer",
            "parent_job_id": self.parent["job_id"], "parent_event_id": self.parent["event_id"], **overrides})

    def test_reason_parent_cannot_fall_back_to_claude_antigravity_or_hosted(self):
        self.admit()
        before = self.effects()
        for recipient in self.targets:
            with self.subTest(recipient=recipient["provider"], mode=recipient["mode"]), self.assertRaises(BridgeError) as rejected:
                self.peer(recipient["id"])
            self.assertEqual(rejected.exception.status, 403)
            self.assertEqual(self.effects(), before)
        self.bridge_send.assert_not_called()

    def test_reason_parent_cannot_omit_forge_or_mismatch_its_parent(self):
        self.admit()
        self.runtime.reason_transport = None
        self.dispatcher.reason_transport = None
        before = self.effects()
        for fields in ({"parent_job_id": None, "parent_event_id": None}, {"parent_job_id": None},
                       {"parent_job_id": "synthetic-missing-parent"}, {"parent_event_id": "synthetic-wrong-event"},
                       {"parent_job_id": 0}):
            with self.subTest(fields=fields), self.assertRaises(BridgeError) as rejected:
                self.peer(self.targets[0]["id"], **fields)
            self.assertEqual(rejected.exception.status, 403)
            self.assertEqual(self.effects(), before)
        self.bridge_send.assert_not_called()

    def test_missing_canonical_parent_admission_fails_closed(self):
        self.admit()
        self.runtime.reason_transport = None
        self.dispatcher.reason_transport = None
        with self.bus.connection() as db:
            db.execute(f"UPDATE {TABLE} SET job_id='synthetic-missing-native-parent' WHERE dispatch_id=?", (self.parent["dispatch_id"],))
        before = self.effects()
        with self.assertRaises(BridgeError) as rejected:
            self.peer(self.targets[0]["id"])
        self.assertEqual(rejected.exception.status, 403)
        self.assertEqual(self.effects(), before)
        self.bridge_send.assert_not_called()

    def test_reason_parent_stays_blocked_when_dispatcher_is_disabled(self):
        self.admit()
        self.runtime.native_dispatcher = None
        self.runtime.reason_transport = None
        before = self.effects()
        with self.assertRaises(BridgeError) as rejected:
            self.peer(self.targets[0]["id"])
        self.assertEqual(rejected.exception.status, 403)
        self.assertEqual(self.effects(), before)
        self.bridge_send.assert_not_called()

    def test_missing_native_parent_transport_lookup_cannot_allow_legacy_fallback(self):
        self.admit()
        self.runtime.native_dispatcher = None
        self.runtime.reason_transport = None
        with self.bus.connection() as db:
            db.execute(f"UPDATE {TABLE} SET actor='synthetic-other-actor' WHERE dispatch_id=?", (self.parent["dispatch_id"],))
        before = self.effects()
        with self.assertRaises(BridgeError) as rejected:
            self.peer(self.targets[0]["id"])
        self.assertEqual(rejected.exception.status, 403)
        self.assertEqual(self.effects(), before)
        self.bridge_send.assert_not_called()

    def test_old_ipc_parent_preserves_legacy_peer_route(self):
        self.admit("codex_ipc")
        before = self.effects()
        result = self.peer(self.targets[0]["id"])
        self.assertEqual(result["state"], "accepted")
        self.bridge_send.assert_called_once()
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["agent_id"], self.targets[0]["id"])
        self.assertEqual(self.effects()[0][1], before[0][1] + 1)

    def test_no_dispatcher_preserves_old_ipc_legacy_peer_route(self):
        self.admit("codex_ipc")
        self.runtime.native_dispatcher = None
        result = self.peer(self.targets[1]["id"])
        self.assertEqual(result["state"], "accepted")
        self.bridge_send.assert_called_once()
        self.assertEqual(result["jobs"][0]["agent_id"], self.targets[1]["id"])

    def test_reason_configured_hosted_target_is_blocked_before_fallback(self):
        self.admit("codex_ipc")
        self.dispatcher.reason_transport = FakeReasonTransport({self.targets[2]["id"]: "synthetic-hosted-target"})
        self.runtime.reason_transport = self.dispatcher.reason_transport
        before = self.effects()
        with self.assertRaises(BridgeError) as rejected:
            self.peer(self.targets[2]["id"])
        self.assertEqual(rejected.exception.status, 403)
        self.assertEqual(self.effects(), before)
        self.bridge_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
