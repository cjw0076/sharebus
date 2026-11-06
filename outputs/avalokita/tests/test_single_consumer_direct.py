"""One execution owner across real BusStore and fake direct transports.

The reused fixtures own disposable buses and files. IPC is a fake pipe and
Reason is replaced before its send boundary; no native/model process is used.
"""
from contextlib import contextmanager
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import test_native_dispatch as ipc_fixture
import test_canonical_reason as reason_fixture
import test_native_peers as peer_fixture
from bridge import BridgeError, BusError, BusStore
from native_dispatch import GRANTS, TABLE, NativeDispatcher


class DirectIPCConsumerTests(unittest.TestCase):
    setUp = ipc_fixture.NativeDispatchTests.setUp
    tearDown = ipc_fixture.NativeDispatchTests.tearDown
    reserve = ipc_fixture.NativeDispatchTests.reserve

    def job(self, job_id):
        with self.bus.store.connection() as db:
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def dispatch(self, dispatch_id):
        with self.bus.store.connection() as db:
            return dict(db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone())

    def test_direct_route_preview_is_not_manual_execution_or_ack(self):
        claim = self.reserve()
        job = self.job(claim["job_id"])
        self.assertEqual(job["execution_owner"], "native_dispatch")
        self.assertIsNone(job["native_input_reserved_at"])
        preview = self.bus.store.external_inbox(self.actor["id"])
        self.assertEqual(preview["jobs"], [])
        message = next(value for value in preview["messages"] if value.get("job_id") == job["id"])
        self.assertFalse(message["can_execute"])
        self.assertEqual(message["execution_owner"], "native_dispatch")
        self.assertEqual(self.bus.store.external_inbox(self.actor["id"], claim_work=True)["jobs"], [])
        with self.assertRaises(BusError):
            self.bus.store.external_inbox(self.actor["id"], acknowledgements=[job["id"]])
        self.assertEqual(self.job(job["id"])["state"], "awaiting_native_reply")
        self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "native_accepted")
        self.assertIsNotNone(self.job(job["id"])["native_input_reserved_at"])

    def test_preparing_callback_cannot_complete_or_suppress_valid_delivery(self):
        claim = self.reserve()
        with self.assertRaises(BusError):
            self.bus.store.native_result(self.actor["id"], self.actor["native_session_id"], "Too early",
                                         job_id=claim["job_id"], event_id=claim["event_id"])
        self.assertEqual(self.job(claim["job_id"])["state"], "awaiting_native_reply")
        self.assertEqual(self.system.opens, [])
        self.dispatcher.deliver(claim["dispatch_id"])
        self.assertTrue(self.bus.store.native_result(self.actor["id"], self.actor["native_session_id"], "Selected final",
                                                    job_id=claim["job_id"], event_id=claim["event_id"])["completed"])
        reserved = self.job(claim["job_id"])["native_input_reserved_at"]
        self.assertTrue(self.dispatcher.deliver(claim["dispatch_id"])["duplicate_suppressed"])
        self.assertEqual(self.job(claim["job_id"])["native_input_reserved_at"], reserved)
        self.assertEqual(len(self.system.opens), 1)

    def test_terminal_job_and_changed_generation_block_before_reservation(self):
        claim = self.reserve()
        self.bus.store.finish(claim["job_id"], "failed", error="Known pre-input failure",
                              expected_execution_owner="native_dispatch")
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(self.job(claim["job_id"])["state"], "failed")
        self.assertIsNone(self.job(claim["job_id"])["native_input_reserved_at"])
        self.assertEqual(self.system.opens, [])

    def test_generation_change_does_not_enter_native_transport(self):
        claim = self.reserve()
        with self.bus.store.connection() as db:
            db.execute("UPDATE agents SET generation=generation+1 WHERE id=?", (self.actor["id"],))
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertIsNone(self.job(claim["job_id"])["native_input_reserved_at"])
        self.assertEqual(self.system.opens, [])

    def test_generation_change_before_claim_preserves_the_captured_pending_job(self):
        before = self.bus.store.jobs()[0]
        self.assertEqual(self.job(before["id"])["generation"], self.actor["generation"])
        self.bus.store.update_agent(self.actor["id"], generation=self.actor["generation"] + 1)
        with self.assertRaises(BridgeError):
            self.reserve()
        job = self.job(before["id"])
        self.assertEqual(job["state"], "avalokita_native_pending")
        self.assertEqual(job["generation"], self.actor["generation"])
        self.assertIsNone(job["started_at"])
        self.assertIsNone(job["native_input_reserved_at"])
        self.assertEqual(self.system.opens, [])

    def test_legacy_null_pending_generation_is_never_filled_by_claim(self):
        before = self.bus.store.jobs()[0]
        with self.bus.store.connection() as db:
            db.execute("UPDATE jobs SET generation=NULL WHERE id=?", (before["id"],))
        with self.assertRaises(BridgeError):
            self.reserve()
        job = self.job(before["id"])
        self.assertIsNone(job["generation"])
        self.assertIsNone(job["started_at"])
        self.assertIsNone(job["native_input_reserved_at"])
        self.assertEqual(job["execution_owner"], "native_dispatch")
        self.assertEqual(self.system.opens, [])

    def test_new_direct_pending_job_captures_the_actual_nonzero_generation(self):
        old_job = self.bus.store.jobs()[0]
        self.bus.store.update_agent(self.actor["id"], generation=7)
        goal = self.runtime.store.new_goal("Selected work in the new execution epoch", idempotency_key="new-epoch")
        receipt = self.runtime.bridge.dispatch(goal, goal["prompt"], native_ipc_ids=[self.actor["id"]])
        self.assertEqual(receipt["state"], "queued")
        claim = self.dispatcher.reserve(goal["id"], self.actor["id"])
        self.assertEqual(self.job(claim["job_id"])["generation"], 7)
        self.assertEqual(self.job(old_job["id"])["generation"], self.actor["generation"])
        self.assertIsNone(self.job(old_job["id"])["started_at"])
        self.assertEqual(self.system.opens, [])

    def test_question_reply_cannot_adopt_a_waiting_job_from_an_older_generation(self):
        claim = self.reserve()
        self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "native_accepted")
        question = self.bus.store.send(self.actor["id"], "user", "Which selected version?", "question")
        self.bus.store.update_agent(self.actor["id"], generation=self.actor["generation"] + 1)
        before = self.bus.store.agent(self.actor["id"])
        goal = self.runtime.store.new_goal("The selected answer", idempotency_key="answer-after-epoch-drift")
        result = self.runtime.bridge.dispatch(goal, goal["prompt"], reply_to=question["event"]["id"],
                                              recipient_ids=[self.actor["id"]], native_ipc_ids=[self.actor["id"]])
        self.assertNotEqual(result["state"], "queued")
        self.assertEqual(self.job(claim["job_id"])["state"], "waiting_user")
        self.assertEqual(self.bus.store.agent(self.actor["id"]), before)
        self.assertEqual(len(self.bus.store.jobs()), 1)
        self.assertEqual(len(self.system.opens), 1)

    def test_public_broker_reservation_denies_direct_and_consumed_direct_reservation_prevents_second_input(self):
        claim = self.reserve()
        with self.assertRaises(BusError):
            self.bus.store.reserve_native_input(self.actor["id"], claim["job_id"], claim["event_id"],
                                                claim["root_id"], self.actor["native_session_id"])
        with self.dispatcher.admission() as (db, _, _):
            first = BusStore._reserve_native_input_in(db, self.actor["id"], claim["job_id"], claim["event_id"],
                claim["root_id"], self.actor["native_session_id"], native_completion_domain="avalokita_direct")
        for _ in range(2):
            with self.assertRaises(BridgeError):
                self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(self.dispatch(claim["dispatch_id"])["state"], "claimed")
        self.assertEqual(self.job(claim["job_id"])["native_input_reserved_at"], first["native_input_reserved_at"])
        self.assertEqual(self.system.opens, [])

    def test_consumed_reservation_and_attempt_marker_survive_interruption(self):
        claim = self.reserve()
        original = self.dispatcher.admission
        entered = 0
        @contextmanager
        def interrupted():
            nonlocal entered
            entered += 1
            if entered == 2:
                raise BridgeError("Synthetic interruption after committed reservation", 409)
            with original() as writers:
                yield writers
        with patch.object(self.dispatcher, "admission", interrupted):
            with self.assertRaises(BridgeError):
                self.dispatcher.deliver(claim["dispatch_id"])
        reserved = self.job(claim["job_id"])["native_input_reserved_at"]
        self.assertIsNotNone(reserved)
        self.assertEqual(self.dispatch(claim["dispatch_id"])["state"], "attempting")
        restarted = NativeDispatcher(self.runtime.bridge, adapter_factory=self.dispatcher.adapter_factory)
        self.assertTrue(restarted.deliver(claim["dispatch_id"])["duplicate_suppressed"])
        self.assertEqual(self.job(claim["job_id"])["native_input_reserved_at"], reserved)
        self.assertEqual(self.system.opens, [])

    def test_pause_resume_preserves_consumed_native_ownership(self):
        claim = self.reserve()
        with self.assertRaises(BusError):
            self.bus.store.reserve_native_input(self.actor["id"], claim["job_id"], claim["event_id"],
                                                claim["root_id"], self.actor["native_session_id"])
        with self.dispatcher.admission() as (db, _, _):
            first = BusStore._reserve_native_input_in(db, self.actor["id"], claim["job_id"], claim["event_id"],
                claim["root_id"], self.actor["native_session_id"], native_completion_domain="avalokita_direct")
        self.runtime.control("pause")
        self.runtime.control("resume")
        job = self.job(claim["job_id"])
        self.assertEqual(job["state"], "awaiting_native_reply")
        self.assertEqual(job["execution_owner"], "native_dispatch")
        self.assertEqual(job["native_input_reserved_at"], first["native_input_reserved_at"])
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(self.system.opens, [])

    def test_claim_reserve_and_final_gate_use_the_same_held_bus_writer(self):
        original_admission = self.dispatcher.admission
        held = []
        calls = {"claim": 0, "reserve": 0, "validate": 0}
        @contextmanager
        def admission():
            with original_admission() as writers:
                held.append(writers[0])
                try:
                    yield writers
                finally:
                    held.pop()
        def checked(name, function):
            def invoke(cls, db, *args, **kwargs):
                self.assertIs(db, held[-1])
                self.assertTrue(db.in_transaction)
                calls[name] += 1
                with patch.object(BusStore, "connection", side_effect=AssertionError("Nested Bus connection")):
                    return function(db, *args, **kwargs)
            return classmethod(invoke)
        with patch.object(self.dispatcher, "admission", admission), \
                patch.object(BusStore, "_claim_execution_in", checked("claim", BusStore._claim_execution_in)), \
                patch.object(BusStore, "_reserve_native_input_in", checked("reserve", BusStore._reserve_native_input_in)), \
                patch.object(BusStore, "_validate_native_input_in", checked("validate", BusStore._validate_native_input_in)):
            claim = self.reserve()
            self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "native_accepted")
        self.assertEqual(calls["claim"], 1)
        self.assertEqual(calls["reserve"], 1)
        self.assertGreaterEqual(calls["validate"], 3)
        self.assertEqual(held, [])

    def test_product_imports_are_candidate_local(self):
        candidate = HERE.parents[1]
        for name in ("bridge", "store", "native_dispatch", "server", "daemon", "hosted_runtime"):
            self.assertTrue(Path(importlib.import_module(name).__file__).resolve().is_relative_to(candidate), name)


class DirectReasonConsumerTests(unittest.TestCase):
    setUp = reason_fixture.CanonicalReasonTests.setUp
    tearDown = reason_fixture.CanonicalReasonTests.tearDown
    reserve = reason_fixture.CanonicalReasonTests.reserve
    state = reason_fixture.CanonicalReasonTests.state

    def process_factory(self, *_args, **_kwargs):
        raise AssertionError("The direct ownership fixture cannot start a child")

    def test_two_reason_gates_reuse_one_consumed_reservation(self):
        fixture = self
        observations = []
        class FakeReason:
            def __init__(self, **values):
                self.gate = values["authorize_send"]
            def send_authorized(self, *_args, **values):
                for _ in range(2):
                    self.assert_reserved(values["dispatch_id"])
                    observations.append(self.gate(values["authorization_record"]))
                return {"state": "transport_accepted", "native_accepted": False, "native_result_verified": False,
                        "automatic_retry": False, "fallback_attempted": False}
            def assert_reserved(self, dispatch_id):
                with fixture.bus.store.connection() as db:
                    job = db.execute(f"SELECT j.* FROM jobs j JOIN {TABLE} d ON d.job_id=j.id WHERE d.dispatch_id=?", (dispatch_id,)).fetchone()
                    observations.append(job["native_input_reserved_at"])
                    fixture.assertEqual(job["execution_owner"], "native_dispatch")
                    fixture.assertIsNotNone(job["native_input_reserved_at"])
        claim = self.reserve()
        with patch("native_dispatch.ReasonEngineAdapter", FakeReason):
            result = self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(observations[1::2], [True, True])
        self.assertEqual(observations[0], observations[2])
        self.assertTrue(result["commit_granted"])
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {GRANTS}").fetchone()[0], 1)
        # Shared ownership/grant is still not durable native-attempt proof.
        with self.assertRaises(BridgeError):
            NativeDispatcher.complete_reason(self.runtime.bridge, self.token, actor=self.actor["id"],
                native_session_id=self.actor["native_session_id"], job_id=claim["job_id"], event_id=claim["event_id"], text="Unproved")
        self.assertEqual(self.starts, [])

    def test_reason_generation_change_between_gates_denies_commit_without_retry(self):
        fixture = self
        gates = []
        class FakeReason:
            def __init__(self, **values):
                self.gate = values["authorize_send"]
            def send_authorized(self, *_args, **values):
                gates.append(self.gate(values["authorization_record"]))
                fixture.bus.store.update_agent(fixture.actor["id"], generation=fixture.actor["generation"] + 1)
                gates.append(self.gate(values["authorization_record"]))
                return {"state": "delivery_unknown", "native_accepted": False, "native_result_verified": False,
                        "automatic_retry": False, "fallback_attempted": False}
        claim = self.reserve()
        with patch("native_dispatch.ReasonEngineAdapter", FakeReason):
            result = self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(gates, [True, False])
        self.assertFalse(result["commit_granted"])
        self.assertTrue(self.dispatcher.deliver(claim["dispatch_id"])["duplicate_suppressed"])
        with self.bus.store.connection() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (claim["job_id"],)).fetchone()
            self.assertIsNotNone(job["native_input_reserved_at"])
            self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {GRANTS}").fetchone()[0], 0)
        self.assertEqual(self.starts, [])


class DeferredPeerConsumerTests(unittest.TestCase):
    setUp = peer_fixture.NativePeerTests.setUp
    tearDown = peer_fixture.NativePeerTests.tearDown
    peer = peer_fixture.NativePeerTests.peer
    finish = peer_fixture.NativePeerTests.finish
    child_job = peer_fixture.NativePeerTests.child_job
    used = peer_fixture.NativePeerTests.used
    not_sent_human = peer_fixture.NativePeerTests.not_sent_human

    def test_not_sent_repair_preserves_a_newer_agent_generation(self):
        fresh, dispatch, _ = self.not_sent_human()
        self.bus.store.update_agent(self.a["id"], generation=self.a["generation"] + 1)
        before = self.bus.store.agent(self.a["id"])
        with self.assertRaises(BridgeError):
            self.runtime.native_dispatcher.classify_not_sent(fresh["goal"]["id"], self.a["id"], dispatch["dispatch_id"])
        self.assertEqual(self.bus.store.agent(self.a["id"]), before)
        with self.bus.store.connection() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (dispatch["job_id"],)).fetchone()
            self.assertEqual(job["state"], "awaiting_native_reply")
            self.assertIsNotNone(job["native_input_reserved_at"])
        self.assertEqual(len(self.sends), 3)

    def test_expired_unattempted_claim_does_not_release_a_newer_agent_generation(self):
        value = self.peer(self.a, self.b, "retired-newer-epoch")
        self.finish(self.b, self.original[self.b["id"]])
        with patch.object(self.runtime.native_dispatcher, "deliver", side_effect=OSError("Fixture exit before input")):
            self.runtime.drain_native_peers()
        self.bus.store.update_agent(self.b["id"], generation=self.b["generation"] + 1)
        before = self.bus.store.agent(self.b["id"])
        used = self.used()
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0 WHERE id=?", (self.root,))
        self.assertEqual(self.runtime.drain_native_peers(), [])
        self.assertEqual(self.child_job(value)["state"], "avalokita_expired")
        self.assertEqual(self.bus.store.agent(self.b["id"]), before)
        self.assertEqual(self.used(), used)
        self.assertEqual(len(self.sends), 2)

    def test_deferred_peer_epoch_drift_never_adopts_the_current_generation(self):
        value = self.peer(self.a, self.b, "deferred-epoch-drift")
        self.assertEqual(self.child_job(value)["generation"], self.b["generation"])
        self.finish(self.b, self.original[self.b["id"]])
        self.bus.store.update_agent(self.b["id"], generation=self.b["generation"] + 1)
        before = self.used()
        self.assertEqual(self.runtime.drain_native_peers(), [])
        job = self.child_job(value)
        self.assertEqual(job["state"], "avalokita_native_pending")
        self.assertEqual(job["generation"], self.b["generation"])
        self.assertIsNone(job["started_at"])
        self.assertIsNone(job["native_input_reserved_at"])
        self.assertEqual(self.used(), before)
        self.assertEqual(len(self.sends), 2)

    def test_legacy_null_deferred_generation_is_never_filled_by_drain(self):
        value = self.peer(self.a, self.b, "deferred-null-epoch")
        self.finish(self.b, self.original[self.b["id"]])
        with self.bus.store.connection() as db:
            db.execute("UPDATE jobs SET generation=NULL WHERE id=?", (self.child_job(value)["id"],))
        before = self.used()
        self.assertEqual(self.runtime.drain_native_peers(), [])
        job = self.child_job(value)
        self.assertEqual(job["state"], "avalokita_native_pending")
        self.assertIsNone(job["generation"])
        self.assertIsNone(job["started_at"])
        self.assertIsNone(job["native_input_reserved_at"])
        self.assertEqual(job["execution_owner"], "native_dispatch")
        self.assertEqual(self.used(), before)
        self.assertEqual(len(self.sends), 2)

    def test_deferred_native_peer_has_no_manual_grant_then_drains_once(self):
        value = self.peer(self.a, self.b, "single-consumer-deferred")
        job = self.child_job(value)
        self.assertEqual(job["state"], "avalokita_native_pending")
        self.assertEqual(job["execution_owner"], "native_dispatch")
        self.assertIsNone(job["native_input_reserved_at"])
        before = self.used()
        inbox = self.bus.store.external_inbox(self.b["id"], claim_work=True)
        self.assertEqual(inbox["jobs"], [])
        public = next(item for item in inbox["messages"] if item.get("job_id") == job["id"])
        self.assertFalse(public["can_execute"])
        self.assertEqual(public["execution_owner"], "native_dispatch")
        with self.assertRaises(BusError):
            self.bus.store.external_inbox(self.b["id"], acknowledgements=[job["id"]])
        self.finish(self.b, self.original[self.b["id"]])
        self.assertEqual(len(self.runtime.drain_native_peers()), 1)
        delivered = self.child_job(value)
        self.assertEqual(delivered["execution_owner"], "native_dispatch")
        self.assertIsNotNone(delivered["native_input_reserved_at"])
        self.assertEqual(self.used(), before)
        self.assertEqual(self.runtime.drain_native_peers(), [])
        self.assertEqual(len(self.sends), 3)

    def test_consumed_peer_reservation_does_not_report_actual_input(self):
        value = self.peer(self.a, self.b, "single-consumer-no-input-proof")
        self.finish(self.b, self.original[self.b["id"]])
        dispatcher = self.runtime.native_dispatcher
        original = dispatcher.admission
        entered = 0
        @contextmanager
        def interrupted():
            nonlocal entered
            entered += 1
            if entered == 3:
                raise BridgeError("Synthetic interruption before the native boundary", 409)
            with original() as writers:
                yield writers
        with patch.object(dispatcher, "admission", interrupted):
            self.assertEqual(self.runtime.drain_native_peers(), [])
        observed = self.peer(self.a, self.b, "single-consumer-no-input-proof")
        self.assertTrue(observed["duplicate_suppressed"])
        self.assertIsNone(observed["native_attempted"])
        self.assertIsNotNone(self.child_job(value)["native_input_reserved_at"])
        self.assertEqual(len(self.sends), 2)


if __name__ == "__main__":
    unittest.main()
