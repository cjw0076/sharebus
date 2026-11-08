from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store as store_module
from store import BusError, BusStore


class SingleConsumerStoreTests(unittest.TestCase):
    def setUp(self):
        expected = Path(__file__).resolve().parents[1] / "store.py"
        self.assertEqual(Path(store_module.__file__).resolve(), expected)
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-single-consumer-")
        self.root = Path(self.directory.name)
        self.store = BusStore(self.root)

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.directory.cleanup()

    def actor(self, mode="external_attached", native="same-native"):
        return self.store.register("claude", mode, mode=mode,
                                   native_session_id=native)[0]

    def task(self, actor, *, budget=3, text="Selected work"):
        sent = self.store.send("user", actor["id"], text, "task", budget=budget)
        return sent["jobs"][0]["id"], sent["event"]["id"], sent["root_id"]

    def row(self, job_id):
        with self.store.connection() as db:
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def reserve(self, actor, ids):
        return self.store.reserve_native_input(actor["id"], *ids,
                                               actor["native_session_id"])

    def test_default_read_is_observation_and_keeps_both_preview_bodies(self):
        actor = self.actor()
        first = self.task(actor, text="A")
        second = self.task(actor, text="B")
        inbox = self.store.external_inbox(actor["id"])
        self.assertEqual(inbox["jobs"], [])
        self.assertEqual([message["body"] for message in inbox["messages"]], ["A", "B"])
        self.assertTrue(all(message["can_execute"] is False for message in inbox["messages"]))
        self.assertEqual(self.row(first[0])["state"], "queued")
        self.assertEqual(self.row(second[0])["state"], "queued")
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT SUM(used) FROM roots").fetchone()[0], 0)

    def test_native_auto_receive_refused_without_claim_or_ack_mutation(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        with self.assertRaises(BusError) as error:
            self.store.external_inbox(actor["id"], claim_work=True)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.row(ids[0])["state"], "queued")

    def test_transport_claim_cannot_be_completed_by_inbox_ack(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        with self.assertRaises(BusError) as error:
            self.store.external_inbox(actor["id"], acknowledgements=[ids[0]], claim_work=False)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.row(ids[0])["state"], "running")

    def test_preparing_is_not_native_input_or_completion_authority(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic preparing receipt")
        with self.assertRaises(BusError) as error:
            self.store.native_result(actor["id"], actor["native_session_id"],
                                     "Premature selected result", ids[0], ids[1])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.row(ids[0])["state"], "awaiting_native_reply")

    def test_manual_receive_only_grants_one_job_even_after_cursor_passed_preview(self):
        actor = self.actor()
        first = self.task(actor, text="A")
        second = self.task(actor, text="B")
        preview = self.store.external_inbox(actor["id"])
        cursor = preview["messages"][-1]["seq"]
        taken = self.store.external_inbox(actor["id"], after=cursor, claim_work=True)
        self.assertEqual([job["id"] for job in taken["jobs"]], [first[0]])
        self.assertIs(taken["jobs"][0]["can_execute"], True)
        self.assertEqual(taken["jobs"][0]["event"]["body"], "A")
        self.assertEqual(taken["messages"], [])
        self.store.external_inbox(actor["id"], acknowledgements=[first[0]])
        replay_ack = self.store.external_inbox(actor["id"], acknowledgements=[first[0]], claim_work=True)
        self.assertEqual(replay_ack["jobs"], [])
        self.assertEqual(self.row(second[0])["state"], "queued")
        next_job = self.store.external_inbox(actor["id"], after=cursor, claim_work=True)["jobs"][0]
        self.assertEqual(next_job["id"], second[0])
        self.assertEqual(next_job["event"]["body"], "B")

    def test_manual_exact_selected_result_preserves_both_channel_states(self):
        for verified in (False, True):
            with self.subTest(channel_verified=verified):
                actor = self.actor(native=f"manual-{verified}")
                self.store.update_agent(actor["id"], channel_verified=verified)
                ids = self.task(actor)
                self.store.external_inbox(actor["id"], claim_work=True)
                result = self.store.native_result(actor["id"], actor["native_session_id"],
                                                 "Selected manual result", ids[0], ids[1])
                self.assertIs(result["completed"], True)
                self.assertEqual(self.store.agent(actor["id"])["channel_verified"], verified)
                self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])

    def test_one_native_reservation_wins_and_lost_response_never_regrants(self):
        actor = self.actor("native_attached")
        ids = self.task(actor, budget=1)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        barrier = threading.Barrier(2)

        def attempt(_):
            barrier.wait(timeout=3)
            try:
                return self.reserve(actor, ids)
            except BusError as error:
                self.assertEqual(error.status, 409)
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        consumed = self.row(ids[0])["native_input_reserved_at"]
        self.assertIsInstance(consumed, float)
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        self.assertEqual(self.row(ids[0])["native_input_reserved_at"], consumed)
        result = self.store.native_result(actor["id"], actor["native_session_id"],
                                         "Verified selected callback", ids[0], ids[1])
        self.assertIs(result["completed"], True)
        self.assertTrue(all(event["can_execute"] is False for event in self.store.events()["events"]))

    def test_reservation_final_fence_rejects_pause_and_binding_change(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.store.set_paused(True)
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        self.store.set_paused(False)
        self.store.update_agent(actor["id"], native_session_id="changed-native")
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])

    def test_known_owner_preinput_failure_and_managed_success_remain_valid(self):
        native = self.actor("native_attached")
        native_ids = self.task(native)
        self.store.claim(native["id"])
        self.store.finish(native_ids[0], "failed", error="Synthetic startup failure",
                          expected_execution_owner="native_dispatch")
        self.assertEqual(self.row(native_ids[0])["state"], "failed")
        self.assertIsNone(self.row(native_ids[0])["native_input_reserved_at"])
        managed = self.actor("managed_new", native="managed-original")
        managed_ids = self.task(managed)
        self.store.claim(managed["id"])
        self.store.finish(managed_ids[0], "completed", expected_execution_owner="managed_runtime")
        self.assertEqual(self.row(managed_ids[0])["state"], "completed")

    def test_started_legacy_null_cannot_be_adopted_even_with_state_override(self):
        actor = self.actor()
        ids = self.task(actor)
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET started_at=?,execution_owner=NULL WHERE id=?", (time.time(), ids[0]))
        restarted = BusStore(self.root)
        self.assertIsNone(restarted.claim(actor["id"]))
        with restarted.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(BusError):
                BusStore._claim_execution_in(db, actor["id"], ids[0], execution_owner="manual_inbox",
                                             allowed_states=("queued", "running"), allow_existing=True)
        self.assertIsNone(self.row(ids[0])["execution_owner"])

    def test_same_writer_helpers_do_not_open_another_store_connection(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with patch.object(BusStore, "connection", side_effect=AssertionError("Nested writer/connection")):
                reserved = BusStore._reserve_native_input_in(db, actor["id"], *ids,
                                                             actor["native_session_id"])
                verified = BusStore._validate_native_input_in(db, actor["id"], *ids,
                                                              actor["native_session_id"], require_reserved=True)
            self.assertEqual(reserved["native_input_reserved_at"], verified["native_input_reserved_at"])
            self.assertIs(db.in_transaction, True)

    def test_pending_never_offers_direct_native_owner_as_manual_fallback(self):
        actor = self.actor()
        ids = self.task(actor)
        self.store.configure_auto(enabled=True)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            BusStore._claim_execution_in(db, actor["id"], ids[0], execution_owner="native_dispatch",
                                         native_completion_domain="avalokita_direct")
        pending = self.store.auto_pending(actor["id"])
        self.assertIs(pending["wake_allowed"], False)
        self.assertEqual(pending["queued"][0]["execution_route"], "native_dispatch")
        self.assertIs(pending["queued"][0]["manual_receive_allowed"], False)
        self.assertEqual(self.store.external_inbox(actor["id"], claim_work=True)["jobs"], [])

    def test_additive_migration_preserves_started_legacy_callback_without_backfill(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET state='awaiting_native_reply',started_at=? WHERE id=?", (time.time(), ids[0]))
            db.execute("UPDATE agents SET state='awaiting_native_reply',active_root=? WHERE id=?", (ids[2], actor["id"]))
            db.execute("UPDATE roots SET used=1 WHERE id=?", (ids[2],))
            original = tuple(db.execute("SELECT id,event_id,state,started_at FROM jobs WHERE id=?", (ids[0],)).fetchone())
            db.execute("DROP TRIGGER jobs_native_completion_insert")
            db.execute("DROP TRIGGER jobs_native_completion_update")
            db.execute("ALTER TABLE jobs DROP COLUMN native_completion_domain")
            db.execute("ALTER TABLE jobs DROP COLUMN execution_owner")
            db.execute("ALTER TABLE jobs DROP COLUMN native_input_reserved_at")
        restarted = BusStore(self.root)
        with restarted.connection() as db:
            migrated = db.execute("SELECT * FROM jobs WHERE id=?", (ids[0],)).fetchone()
            self.assertEqual(tuple(migrated[key] for key in ("id", "event_id", "state", "started_at")), original)
            self.assertIsNone(migrated["execution_owner"])
            self.assertIsNone(migrated["native_input_reserved_at"])
        with self.assertRaises(BusError):
            restarted.native_result(actor["id"], actor["native_session_id"], "Cannot adopt old callback", ids[0], ids[1])
        self.assertEqual(self.row(ids[0])["state"], "awaiting_native_reply")

    def test_unstarted_legacy_dispatch_and_receipt_are_not_new_manual_work(self):
        actor = self.actor()
        ids = self.task(actor)
        with self.store.connection() as db:
            db.execute("CREATE TABLE avalokita_native_dispatch(job_id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO avalokita_native_dispatch VALUES (?)", (ids[0],))
        self.assertIsNone(self.store.claim(actor["id"]))
        pending = self.store.auto_pending(actor["id"])
        self.assertEqual(pending["queued"][0]["execution_route"], "legacy_unknown")
        with self.store.connection() as db:
            db.execute("DELETE FROM avalokita_native_dispatch WHERE job_id=?", (ids[0],))
            self.store._insert_event(db, "notice", "system", "user", "Preserved old receipt", ids[2],
                                     {"job_id": ids[0], "state": "awaiting_native_reply"})
        self.assertIsNone(self.store.claim(actor["id"]))
        self.assertIsNone(self.row(ids[0])["execution_owner"])
        self.assertEqual(self.row(ids[0])["state"], "queued")

    def test_manual_winner_cannot_be_reassigned_by_direct_native_lane(self):
        actor = self.actor()
        ids = self.task(actor)
        manual = self.store.external_inbox(actor["id"], claim_work=True)["jobs"][0]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            with self.assertRaises(BusError):
                BusStore._claim_execution_in(db, actor["id"], ids[0], execution_owner="native_dispatch",
                                             native_completion_domain="avalokita_direct",
                                             allowed_states=("running",), allow_existing=True)
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        self.assertEqual(self.row(ids[0])["execution_owner"], "manual_inbox")
        self.assertIs(manual["can_execute"], True)

    def test_explicit_null_address_is_a_final_fence_at_the_current_generation(self):
        actor = self.actor("native_attached")
        actor = self.store.update_agent(actor["id"], native_address="current-address")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.assertEqual(self.row(ids[0])["generation"], actor["generation"])
        with self.assertRaises(BusError):
            self.store.reserve_native_input(actor["id"], *ids, actor["native_session_id"],
                                             provider="claude", native_address=None, generation=actor["generation"])
        self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])
        self.store.reserve_native_input(actor["id"], *ids, actor["native_session_id"],
                                        provider="claude", native_address="current-address", generation=actor["generation"])

    def test_optional_generation_is_an_additional_final_fence(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.assertEqual(self.row(ids[0])["generation"], self.store.agent(actor["id"])["generation"])
        with self.assertRaises(BusError):
            self.store.reserve_native_input(actor["id"], *ids, actor["native_session_id"], generation=1)
        self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])
        self.store.reserve_native_input(actor["id"], *ids, actor["native_session_id"], generation=0)

    def test_epoch_drift_is_a_final_fence_when_the_optional_generation_is_omitted(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        actor = self.store.update_agent(actor["id"], native_address="later-address")
        self.assertNotEqual(self.row(ids[0])["generation"], actor["generation"])
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])

    def test_suspension_is_a_final_fence_at_the_current_generation(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.assertEqual(self.row(ids[0])["generation"], self.store.agent(actor["id"])["generation"])
        self.store.configure_auto(suspended=True)
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])
        self.store.configure_auto(suspended=False)
        self.reserve(actor, ids)

    def test_expired_deadline_is_a_final_fence_at_the_current_generation(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.store.validate_execution(actor["id"], *ids, execution_owner="native_dispatch")
        self.assertEqual(self.row(ids[0])["generation"], self.store.agent(actor["id"])["generation"])
        with self.store.connection() as db:
            db.execute("UPDATE roots SET deadline=? WHERE id=?", (time.time() - 1, ids[2]))
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])

    def test_captured_null_session_and_mode_change_deny_supervisor_fence(self):
        actor = self.actor("managed_new", native=None)
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.update_agent(actor["id"], native_session_id="new-binding")
        with self.assertRaises(BusError):
            self.store.validate_execution(actor["id"], *ids, execution_owner="managed_runtime", native_session_id=None)
        self.store.update_agent(actor["id"], mode="external_attached")
        with self.assertRaises(BusError):
            self.store.validate_execution(actor["id"], *ids, execution_owner="managed_runtime")

    def test_native_origin_is_already_present_and_never_gets_extra_input(self):
        actor = self.actor("native_attached")
        sent = self.store.send("user", "all", "Original native human turn", "task", native_origin=actor["id"])
        ids = (sent["origin_job_id"], sent["event"]["id"], sent["root_id"])
        row = self.row(ids[0])
        self.assertEqual(row["execution_owner"], "native_dispatch")
        self.assertGreater(row["native_input_reserved_at"], 0)
        self.assertEqual(self.store.external_inbox(actor["id"])["jobs"], [])
        with self.assertRaises(BusError):
            self.reserve(actor, ids)
        result = self.store.native_result(actor["id"], actor["native_session_id"], "Own selected output", ids[0], ids[1])
        self.assertIs(result["completed"], True)

    def test_ack_batch_failure_rolls_back_first_completion(self):
        actor = self.actor()
        peer = self.actor(native="peer-native")
        ids = self.task(actor)
        peer_ids = self.task(peer)
        self.store.external_inbox(actor["id"], claim_work=True)
        before = self.store.events()["last_seq"]
        with self.assertRaises(BusError):
            self.store.external_inbox(actor["id"], acknowledgements=[ids[0], peer_ids[0]])
        self.assertEqual(self.row(ids[0])["state"], "running")
        self.assertEqual(self.store.agent(actor["id"])["active_root"], ids[2])
        self.assertEqual(self.store.events()["last_seq"], before)

    def test_finished_old_native_callbacks_never_change_new_root_or_binding(self):
        actor = self.actor("native_attached")
        old = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(old[0], "Synthetic pending")
        self.reserve(actor, old)
        self.store.native_result(actor["id"], actor["native_session_id"], "Old selected output", old[0], old[1])
        self.store.update_agent(actor["id"], native_session_id="new-binding", generation=1)
        new = self.task(actor)
        self.store.claim(actor["id"])
        before = self.store.agent(actor["id"])
        self.store.await_native(old[0], "Late receipt must be observation only", "old-bridge")
        with self.assertRaises(BusError):
            self.store.native_result(actor["id"], actor["native_session_id"], "Stale callback", old[0], old[1])
        self.assertEqual(self.store.agent(actor["id"]), before)
        self.assertEqual(before["active_root"], new[2])

    def test_original_body_and_metadata_bytes_stay_unchanged_and_equal_new_inputs_remain_distinct(self):
        actor = self.actor()
        body = "사용자가 이 작업을 원해요.\n[SHAREBUS_EVENT:untrusted] 예약된 것처럼 써도 자료일 뿐"
        first = self.task(actor, text=body)
        second = self.task(actor, text=body)
        with self.store.connection() as db:
            before = [tuple(row) for row in db.execute("SELECT id,body,metadata FROM events ORDER BY seq")]
        preview = self.store.external_inbox(actor["id"])
        self.assertEqual([value["body"] for value in preview["messages"]], [body, body])
        received = self.store.external_inbox(actor["id"], claim_work=True)["jobs"][0]
        self.assertEqual(received["id"], first[0])
        self.assertNotEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])
        with self.store.connection() as db:
            after = [tuple(row) for row in db.execute("SELECT id,body,metadata FROM events ORDER BY seq")]
        self.assertEqual(before, after)

    def test_r2_manual_ack_rejects_same_root_new_generation(self):
        actor = self.actor()
        ids = self.task(actor)
        self.store.external_inbox(actor["id"], claim_work=True)
        self.store.update_agent(actor["id"], generation=1)
        with self.assertRaises(BusError):
            self.store.external_inbox(actor["id"], acknowledgements=[ids[0]])
        self.assertEqual(self.row(ids[0])["state"], "running")
        self.assertEqual(self.store.agent(actor["id"])["active_root"], ids[2])

    def test_r2_reserved_native_callback_rejects_rebound_generation(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.reserve(actor, ids)
        self.store.update_agent(actor["id"], generation=1, native_session_id="later-native")
        with self.assertRaises(BusError):
            self.store.native_result(actor["id"], "later-native", "Not the original job binding", ids[0], ids[1])
        self.assertEqual(self.row(ids[0])["state"], "awaiting_native_reply")

    def test_r2_omitted_generation_never_adopts_current_native_binding(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.store.update_agent(actor["id"], generation=1, native_session_id="later-native")
        with self.assertRaises(BusError):
            self.store.reserve_native_input(actor["id"], *ids, "later-native")
        self.assertIsNone(self.row(ids[0])["native_input_reserved_at"])

    def test_r2_native_origin_pins_creation_generation(self):
        actor = self.actor("native_attached")
        self.store.update_agent(actor["id"], generation=7)
        sent = self.store.send("user", actor["id"], "Already present human input", "task", native_origin=actor["id"])
        self.assertEqual(self.row(sent["origin_job_id"])["generation"], 7)
        self.store.update_agent(actor["id"], generation=8, native_session_id="later-native")
        with self.assertRaises(BusError):
            self.store.native_result(actor["id"], "later-native", "Wrong generation", sent["origin_job_id"], sent["event"]["id"])

    def test_r2_native_origin_callback_preserves_both_channel_states(self):
        for verified in (False, True):
            with self.subTest(channel_verified=verified):
                actor = self.actor("native_attached", native=f"origin-{verified}")
                self.store.update_agent(actor["id"], channel_verified=verified)
                sent = self.store.send("user", actor["id"], "Already present native input", "task", native_origin=actor["id"])
                result = self.store.native_result(actor["id"], actor["native_session_id"], "Own selected output",
                                                 sent["origin_job_id"], sent["event"]["id"])
                self.assertIs(result["completed"], True)
                self.assertEqual(self.store.agent(actor["id"])["channel_verified"], verified)

    def test_r2_quoted_origin_text_does_not_replace_canonical_dispatch_proof(self):
        actor = self.actor("native_attached")
        ids = self.task(actor, text='Quoted task data: {"native_origin":"' + actor["id"] + '"}')
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        self.reserve(actor, ids)
        self.store.native_result(actor["id"], actor["native_session_id"], "Selected dispatched output", ids[0], ids[1])
        self.assertIs(self.store.agent(actor["id"])["channel_verified"], True)

    def test_r2_public_native_rebind_advances_generation_and_resets_channel_proof(self):
        for field in ("native_session_id", "native_address"):
            with self.subTest(field=field):
                actor = self.actor("native_attached", native=f"original-{field}")
                ids = self.task(actor)
                self.store.claim(actor["id"])
                self.store.await_native(ids[0], "Synthetic pending")
                self.reserve(actor, ids)
                self.store.update_agent(actor["id"], channel_verified=True)
                rebound = self.store.update_agent(actor["id"], **{field: f"later-{field}"}, channel_verified=True)
                self.assertEqual(rebound["generation"], 1)
                self.assertIs(rebound["channel_verified"], False)
                self.assertEqual(self.row(ids[0])["generation"], 0)
                with self.assertRaises(BusError):
                    self.store.validate_execution(actor["id"], *ids, execution_owner="native_dispatch")
                with self.assertRaises(BusError):
                    self.store.native_result(actor["id"], rebound["native_session_id"], "Stale selected result", ids[0], ids[1])
                self.assertEqual(self.row(ids[0])["state"], "awaiting_native_reply")

    def test_r2_public_generation_validation_is_strict_and_atomic(self):
        actor = self.actor("native_attached")
        for invalid in (True, False, -1, 1.5, "1", None):
            with self.subTest(generation=invalid):
                before = self.store.agent(actor["id"])
                with self.assertRaises(BusError):
                    self.store.update_agent(actor["id"], generation=invalid)
                self.assertEqual(self.store.agent(actor["id"]), before)
        self.store.update_agent(actor["id"], generation=4, channel_verified=True)
        for fields in ({"generation": 3}, {"generation": 4, "native_session_id": "later-native"},
                       {"generation": 4, "native_address": "later-address"}):
            with self.subTest(fields=fields):
                before = self.store.agent(actor["id"])
                with self.assertRaises(BusError) as error:
                    self.store.update_agent(actor["id"], **fields)
                self.assertEqual(error.exception.status, 409)
                self.assertEqual(self.store.agent(actor["id"]), before)
        rebound = self.store.update_agent(actor["id"], generation=5, native_address="later-address")
        self.assertEqual(rebound["generation"], 5)
        self.assertIs(rebound["channel_verified"], False)

    def test_r2_same_binding_updates_and_heartbeats_preserve_generation_and_proof(self):
        actor = self.actor("native_attached")
        self.store.update_agent(actor["id"], generation=3, native_address="same-address", channel_verified=True)
        self.store.update_agent(actor["id"], channel_verified=True)
        unchanged = self.store.update_agent(actor["id"], native_session_id=actor["native_session_id"],
                                           native_address="same-address", generation=3)
        heartbeat = self.store.heartbeat(actor["id"], native_session_id=actor["native_session_id"])
        for current in (unchanged, heartbeat):
            self.assertEqual(current["generation"], 3)
            self.assertIs(current["channel_verified"], True)

    def test_r2_first_heartbeat_binding_advances_once_and_invalidates_old_work(self):
        actor = self.actor(native=None)
        ids = self.task(actor)
        self.store.external_inbox(actor["id"], claim_work=True)
        self.store.update_agent(actor["id"], channel_verified=True)
        bound = self.store.heartbeat(actor["id"], native_session_id="first-native")
        repeated = self.store.heartbeat(actor["id"], native_session_id="first-native")
        self.assertEqual(bound["generation"], 1)
        self.assertEqual(repeated["generation"], 1)
        self.assertIs(bound["channel_verified"], False)
        self.assertEqual(self.row(ids[0])["generation"], 0)
        with self.assertRaises(BusError):
            self.store.external_inbox(actor["id"], acknowledgements=[ids[0]])
        before = self.store.agent(actor["id"])
        with self.assertRaises(BusError) as error:
            self.store.heartbeat(actor["id"], native_session_id="other-native")
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.store.agent(actor["id"]), before)

    def test_r2_concurrent_public_rebinds_each_advance_generation(self):
        actor = self.actor("native_attached")
        self.store.update_agent(actor["id"], channel_verified=True)
        ready = threading.Barrier(2)

        def rebind(address):
            ready.wait(timeout=5)
            return self.store.update_agent(actor["id"], native_address=address)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(rebind, ("first-address", "second-address")))
        current = self.store.agent(actor["id"])
        self.assertEqual(current["generation"], 2)
        self.assertIs(current["channel_verified"], False)

    def test_r2_unknown_job_generation_cannot_be_adopted_for_execution_or_failure(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.await_native(ids[0], "Synthetic pending")
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET generation=NULL WHERE id=?", (ids[0],))
        before = self.row(ids[0])
        with self.assertRaises(BusError):
            self.store.validate_execution(actor["id"], *ids, execution_owner="native_dispatch", generation=0)
        with self.assertRaises(BusError):
            self.store.reserve_native_input(actor["id"], *ids, actor["native_session_id"], generation=0)
        with self.assertRaises(BusError):
            self.store.finish(ids[0], "failed", error="Cannot adopt an unknown generation")
        self.assertEqual(self.row(ids[0]), before)

    def test_r3_stale_receipts_cannot_mutate_job_agent_or_events(self):
        cases = (("native_attached", False, True), ("native_attached", False, False),
                 ("native_attached", True, False), ("managed_new", False, False))
        for index, (mode, already_awaiting, rebind) in enumerate(cases):
            with self.subTest(mode=mode, already_awaiting=already_awaiting, rebind=rebind):
                actor = self.actor(mode, native=f"receipt-original-{index}")
                ids = self.task(actor)
                self.store.claim(actor["id"])
                if already_awaiting:
                    self.store.await_native(ids[0], "Original current-epoch receipt")
                if rebind:
                    self.store.update_agent(actor["id"], native_session_id=f"receipt-later-{index}")
                else:
                    self.store.update_agent(actor["id"], generation=1)
                before = (self.row(ids[0]), self.store.agent(actor["id"]), self.store.events())
                with self.assertRaises(BusError) as error:
                    self.store.await_native(ids[0], "Late old-epoch receipt", "old-bridge")
                self.assertEqual(error.exception.status, 409)
                self.assertEqual((self.row(ids[0]), self.store.agent(actor["id"]), self.store.events()), before)

    def test_r3_unknown_generation_receipt_cannot_be_adopted(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        with self.store.connection() as db:
            db.execute("UPDATE jobs SET generation=NULL WHERE id=?", (ids[0],))
        before = (self.row(ids[0]), self.store.agent(actor["id"]), self.store.events())
        with self.assertRaises(BusError) as error:
            self.store.await_native(ids[0], "Cannot adopt an unknown receipt epoch")
        self.assertEqual(error.exception.status, 409)
        self.assertEqual((self.row(ids[0]), self.store.agent(actor["id"]), self.store.events()), before)

    def test_r3_terminal_receipts_keep_the_existing_noop_across_epochs(self):
        actor = self.actor("native_attached")
        ids = self.task(actor)
        self.store.claim(actor["id"])
        self.store.update_agent(actor["id"], generation=1)
        for terminal in ("completed", "waiting_user", "input_received"):
            with self.subTest(state=terminal):
                with self.store.connection() as db:
                    db.execute("UPDATE jobs SET state=?,generation=NULL WHERE id=?", (terminal, ids[0]))
                before = (self.row(ids[0]), self.store.agent(actor["id"]), self.store.events())
                self.assertIsNone(self.store.await_native(ids[0], "Terminal late receipt"))
                self.assertEqual((self.row(ids[0]), self.store.agent(actor["id"]), self.store.events()), before)

    def test_r3_current_epoch_receipt_preserves_owner_generation_and_input_reservation(self):
        for mode in ("native_attached", "managed_new"):
            with self.subTest(mode=mode):
                actor = self.actor(mode, native=f"current-receipt-{mode}")
                ids = self.task(actor)
                self.store.claim(actor["id"])
                original = self.row(ids[0])
                last_seq = self.store.events()["last_seq"]
                self.store.await_native(ids[0], "Current-epoch receipt", "current-bridge")
                current = self.row(ids[0])
                self.assertEqual(current["state"], "awaiting_native_reply")
                self.assertEqual(self.store.agent(actor["id"])["state"], "awaiting_native_reply")
                for field in ("execution_owner", "generation", "native_input_reserved_at"):
                    self.assertEqual(current[field], original[field])
                self.assertEqual(self.store.events()["last_seq"], last_seq + 1)
                with self.store.connection() as db:
                    self.assertEqual(db.execute("SELECT used FROM roots WHERE id=?", (ids[2],)).fetchone()[0], 1)

    def test_r2_two_legacy_constructor_migrations_are_serialized(self):
        with self.store.connection() as db:
            db.execute("DROP TRIGGER jobs_native_completion_insert")
            db.execute("DROP TRIGGER jobs_native_completion_update")
            db.execute("ALTER TABLE jobs DROP COLUMN native_completion_domain")
            db.execute("ALTER TABLE jobs DROP COLUMN execution_owner")
            db.execute("ALTER TABLE jobs DROP COLUMN native_input_reserved_at")
        schema_ready = threading.Barrier(2)
        old_reads_ready = threading.Barrier(2)
        second_begin = threading.Event()
        lock = threading.Lock()
        records = {"begin_attempts": 0, "missing_reads_without_writer": 0, "writer_reads": 0}
        original_connect = sqlite3.connect

        class MigrationConnection(sqlite3.Connection):
            def __init__(connection, *args, **kwargs):
                super().__init__(*args, **kwargs)
                connection.job_reads = 0
                connection.writer_slot = 0

            def executescript(connection, script):
                result = super().executescript(script)
                schema_ready.wait(timeout=5)
                return result

            def execute(connection, sql, *args, **kwargs):
                if sql.strip().upper() == "BEGIN IMMEDIATE":
                    with lock:
                        records["begin_attempts"] += 1
                        connection.writer_slot = records["begin_attempts"]
                        if connection.writer_slot == 2:
                            second_begin.set()
                result = super().execute(sql, *args, **kwargs)
                if sql == "PRAGMA table_info(jobs)":
                    connection.job_reads += 1
                    if connection.in_transaction:
                        with lock:
                            records["writer_reads"] += 1
                        if connection.writer_slot == 1:
                            if not second_begin.wait(timeout=5):
                                raise AssertionError("Second constructor did not attempt the migration writer")
                    elif connection.job_reads == 2:
                        # On the original source both constructors capture the
                        # missing owner column before either can ALTER it.
                        rows = result.fetchall()
                        with lock:
                            records["missing_reads_without_writer"] += 1
                        old_reads_ready.wait(timeout=5)
                        return iter(rows)
                return result

        def guarded_connect(*args, **kwargs):
            return original_connect(*args, factory=MigrationConnection, **kwargs)

        def first_open(_):
            try:
                BusStore(self.root, timeout=3)
                return None
            except Exception as error:
                return str(error)

        with patch.object(store_module.sqlite3, "connect", guarded_connect):
            with ThreadPoolExecutor(max_workers=2) as pool:
                errors = list(pool.map(first_open, range(2)))
        self.assertEqual(errors, [None, None])
        self.assertEqual(records["begin_attempts"], 2)
        self.assertEqual(records["missing_reads_without_writer"], 0)
        self.assertGreaterEqual(records["writer_reads"], 4)


if __name__ == "__main__":
    unittest.main()
