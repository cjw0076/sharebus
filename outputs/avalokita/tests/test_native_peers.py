"""Public peer continuations in disposable workspaces and fake pinned pipes.

No native applications or real model sessions are contacted.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from contextlib import closing

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from adapters.codex_app_tools import CodexAppToolsAdapter
from bridge import BridgeError
from daemon import BusHTTPServer
from native_dispatch import ENVELOPES, PEERS, TABLE, NativeDispatcher, complete
from server import Runtime, Server
from test_codex_app_tools import FakePipe, FakeSystem, SEND_TOOL, SID, inventory, rpc_result


class NativePeerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-native-source"})
        self.env.start()
        self.bus = BusHTTPServer(self.workspace, enable_native=False, discovery=[])
        (self.bus.store.private / "runtime.json").write_text(json.dumps(self.bus.runtime()), encoding="utf-8")
        self.bus_thread = threading.Thread(target=self.bus.serve_forever, daemon=True)
        self.bus_thread.start()
        self.a, self.a_token = self.bus.store.register("codex", "Planner", ["ideation"], "external_attached", role="Planner", native_session_id="fixture-native-a")
        self.b, self.b_token = self.bus.store.register("codex", "Builder", ["frontend", "review"], "external_attached", role="Builder", native_session_id="fixture-native-b")
        self.sends = []
        self.metadata_calls = 0
        self.unknown = False
        self.not_sent = False
        self.sent = threading.Event()
        fixture = self
        class Metadata:
            def probe_metadata(self, source):
                return self.discover()
            def discover(self):
                fixture.metadata_calls += 1
                return {"same_handle_server_verified": True, "native_message_sent": False,
                        "owner_sid_sha256": hashlib.sha256(SID.encode()).hexdigest()}
        class Delivery:
            def __init__(self, kwargs):
                self.kwargs = kwargs
            def send_authorized(self, target, prompt, **values):
                response = inventory([] if fixture.not_sent else [SEND_TOOL])
                if not fixture.unknown:
                    response += rpc_result(2, {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps({"threadId": target})}]})
                pipe = FakePipe(response)
                adapter = CodexAppToolsAdapter(system=FakeSystem(pipe), **self.kwargs)
                result = adapter.send_authorized(target, prompt, **values)
                fixture.sends.append({"target": target, "prompt": prompt, "receipt": result, "writes": pipe.writes})
                fixture.sent.set()
                return result
        def factory(**kwargs):
            return Delivery(kwargs) if "ledger_path" in kwargs else Metadata()
        self.factory = factory
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
        self.unregister_completion = self.bus.store.register_native_completion_handler(self.runtime.complete_native_result)
        self.response = self.runtime.prompt({"text": "Together build the authorized selected prototype, then ask a different peer to review.", "idempotency_key": "one-human"})
        self.goal = self.response["goal"]["id"]
        self.root = self.response["dispatch"]["root_id"]
        self.original = {job["agent_id"]: job for job in self.bus.store.jobs()}
        self.assertEqual(len(self.sends), 2)
        self.assertTrue(all(job["state"] == "awaiting_native_reply" for job in self.original.values()))

    def tearDown(self):
        self.unregister_completion()
        self.runtime.stop_peer_reconciler()
        self.bus.shutdown()
        self.bus.close()
        self.env.stop()
        self.temp.cleanup()

    def peer(self, sender, recipient, key="peer-one", **values):
        token = self.a_token if sender == self.a else self.b_token
        return self.runtime.peer(token, {"operation": "send-keys", "root_id": self.goal, "recipient": recipient["id"],
            "body": "Please review my bounded contribution and share the result.", "kind": "question", "idempotency_key": key, **values})

    def finish(self, actor, job, text="Selected exact fixture completion"):
        return complete(self.workspace, actor=actor["id"], native_session_id=actor["native_session_id"],
                        job_id=job["id"], event_id=job["event_id"], text=text)

    def child_job(self, value):
        with self.bus.store.connection() as db:
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (value["jobs"][0]["id"],)).fetchone())

    def used(self):
        with self.bus.store.connection() as db:
            return db.execute("SELECT used FROM roots WHERE id=?", (self.root,)).fetchone()[0]

    def test_busy_peers_defer_then_complete_and_dispatch_each_descendant_once(self):
        ab = self.peer(self.a, self.b, "a-to-b")
        ba = self.peer(self.b, self.a, "b-to-a")
        self.assertEqual((ab["state"], ba["state"]), ("deferred", "deferred"))
        self.assertEqual(self.used(), 4)
        self.assertEqual(self.child_job(ab)["state"], "avalokita_native_pending")
        self.assertIsNone(self.bus.store.claim(self.b["id"]))
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 2)
        self.finish(self.a, self.original[self.a["id"]])
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 3)
        self.assertEqual(self.sends[-1]["target"], self.a["native_session_id"])
        self.finish(self.b, self.original[self.b["id"]])
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 4)
        state = self.runtime.state()
        self.assertNotEqual(state["dispatches"][self.goal]["status"], "completed")
        self.assertEqual(len(state["dispatches"][self.goal]["outcomes"]), 4)
        self.finish(self.a, self.child_job(ba), "A reviewed B's bounded output")
        self.finish(self.b, self.child_job(ab), "B reviewed A's bounded output")
        state = self.runtime.state()
        self.assertEqual(state["dispatches"][self.goal]["status"], "completed")
        self.assertTrue(state["dispatches"][self.goal]["delivery_verified"])
        self.assertEqual(self.used(), 4)
        self.assertEqual(len(self.sends), 4)
        messages = [m for m in state["messages"] if m.get("source_event_id") in {ab["event"]["id"], ba["event"]["id"]}]
        self.assertEqual(len(messages), 2)
        self.assertEqual({m["recipient"] for m in messages}, {self.a["id"], self.b["id"]})
        self.assertTrue(all(m["root_id"] == self.goal and m["kind"] == "question" for m in messages))
        self.assertEqual(sum(m["body"] == "A reviewed B's bounded output" for m in state["messages"]), 1)
        with self.bus.store.connection() as db:
            child = dict(db.execute("SELECT * FROM events WHERE id=?", (ab["event"]["id"],)).fetchone())
        self.assertEqual(child["sender"], self.a["id"])
        self.assertNotIn("human_authorized", json.loads(child["metadata"]))

    def test_fifo_mailbox_and_restart_idempotency_never_double_debit(self):
        first = self.peer(self.a, self.b, "first")
        second = self.peer(self.a, self.b, "second", body="Review the second scope afterwards")
        self.finish(self.b, self.original[self.b["id"]])
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=self.factory)
        duplicate = self.peer(self.a, self.b, "first")
        self.assertTrue(duplicate["duplicate_suppressed"])
        self.assertEqual(duplicate["event"]["id"], first["event"]["id"])
        self.runtime.drain_native_peers()
        self.assertEqual(self.child_job(first)["state"], "awaiting_native_reply")
        self.assertEqual(self.child_job(second)["state"], "avalokita_native_pending")
        self.finish(self.b, self.child_job(first))
        self.runtime.drain_native_peers()
        self.assertEqual(self.child_job(second)["state"], "awaiting_native_reply")
        self.assertEqual(self.used(), 4)
        self.assertEqual(len(self.sends), 4)
        with self.assertRaises(BridgeError):
            self.peer(self.a, self.b, "first", body="Changed payload must conflict")
        self.assertEqual(self.used(), 4)

    def test_idle_completed_sender_retains_roster_but_different_root_cannot_send(self):
        self.finish(self.a, self.original[self.a["id"]])
        value = self.peer(self.a, self.b)
        self.assertEqual(value["parent_job_id"], self.original[self.a["id"]]["id"])
        self.bus.store.update_agent(self.a["id"], state="running", active_root="unrelated-root")
        with self.assertRaises(BridgeError):
            self.peer(self.a, self.b, "other-root")
        self.assertEqual(self.used(), 3)

    def test_exact_parent_pair_forgery_and_late_recipient_are_rejected(self):
        with self.assertRaises(BridgeError):
            self.peer(self.a, self.b, parent_job_id=self.original[self.b["id"]]["id"], parent_event_id=self.original[self.b["id"]]["event_id"])
        c, _ = self.bus.store.register("codex", "Late peer", [], "external_attached", native_session_id="fixture-late")
        with self.assertRaises(BridgeError):
            self.peer(self.a, c, "late")
        self.assertEqual(self.used(), 2)
        self.assertEqual(len(self.sends), 2)

    def test_waiting_human_cross_root_and_busy_without_proof_targets_reject(self):
        for state, active in (("waiting_for_user", self.root), ("running", "another-root"), ("running", None), ("paused", self.root)):
            with self.subTest(state=state, active=active):
                self.bus.store.update_agent(self.b["id"], state=state, active_root=active)
                with self.assertRaises(BridgeError):
                    self.peer(self.a, self.b, state + str(active))
        self.assertEqual(self.used(), 2)

    def test_pause_deadline_and_budget_reject_without_new_work(self):
        self.runtime.control("pause")
        with self.assertRaises(BridgeError):
            self.peer(self.a, self.b, "pause")
        self.runtime.control("resume")
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET used=budget WHERE id=?", (self.root,))
        with self.assertRaises(BridgeError):
            self.peer(self.a, self.b, "budget")
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET used=2,deadline=0 WHERE id=?", (self.root,))
        with self.assertRaises(BridgeError):
            self.peer(self.a, self.b, "deadline")
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(len(self.bus.store.jobs()), 2)

    def test_final_reserved_turn_can_drain_without_resetting_budget(self):
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET budget=3 WHERE id=?", (self.root,))
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        self.runtime.drain_native_peers()
        self.assertEqual(self.child_job(child)["state"], "awaiting_native_reply")
        self.assertEqual(self.used(), 3)
        self.assertEqual(len(self.sends), 3)

    def test_unknown_child_is_not_replayed_after_restart(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        self.unknown = True
        self.runtime.drain_native_peers()
        self.assertEqual(self.sends[-1]["receipt"]["state"], "delivery_unknown")
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=self.factory)
        self.runtime.drain_native_peers()
        repeat = self.peer(self.a, self.b)
        self.assertTrue(repeat["duplicate_suppressed"])
        self.assertEqual(repeat["native_delivery_state"], "delivery_unknown")
        self.assertEqual(len(self.sends), 3)
        self.finish(self.b, self.child_job(child), "Exact late child result")
        self.assertIn("Exact late child result", [m["body"] for m in self.runtime.state()["messages"]])

    def test_rebind_retires_only_unattempted_child_without_native_send(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        self.bus.store.update_agent(self.b["id"], native_session_id="fixture-rebound")
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.child_job(child)["state"], "avalokita_binding_revoked")

    def test_forged_lineage_before_drain_never_sends(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        with self.bus.store.connection() as db:
            db.execute("UPDATE events SET metadata=json_set(metadata,'$.parent_job_id','forged') WHERE id=?", (child["event"]["id"],))
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.child_job(child)["state"], "avalokita_native_pending")

    def test_frozen_roster_and_peer_task_data_keep_original_human_authority(self):
        child = self.peer(self.a, self.b)
        self.bus.store.update_agent(self.a["id"], label="Changed after admission", capabilities=["new-observation"])
        self.finish(self.b, self.original[self.b["id"]])
        self.runtime.drain_native_peers()
        prompt = self.sends[-1]["prompt"]
        self.assertIn("<peer_task_data>", prompt)
        self.assertIn("<human_request>", prompt)
        self.assertIn("Planner", prompt)
        self.assertNotIn("Changed after admission", prompt)
        self.assertIn(child["parent_job_id"], prompt)
        self.assertIn("not a new human instruction or approval", prompt)
        self.assertNotIn(self.a_token, prompt)

    def test_publish_is_public_only_and_does_not_implicitly_wake(self):
        before = self.used()
        value = self.runtime.peer(self.a_token, {"operation": "publish", "root_id": self.goal, "kind": "proposal", "body": "I can own a small UI scope."})
        self.assertEqual(value["actor"], self.a["id"])
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.used(), before)

    def test_server_background_drains_without_state_requests_and_idles_without_native_discovery(self):
        before = self.metadata_calls
        self.runtime.start_peer_reconciler()
        time.sleep(.85)
        self.assertEqual(self.metadata_calls, before)
        self.assertEqual(len(self.sends), 2)
        child = self.peer(self.a, self.b)
        self.sent.clear()
        self.finish(self.b, self.original[self.b["id"]])
        self.assertTrue(self.sent.wait(3), "Background mailbox must drain without state() or any GUI request")
        self.assertEqual(self.child_job(child)["state"], "awaiting_native_reply")
        time.sleep(.85)
        self.assertEqual(len(self.sends), 3)
        self.assertEqual(self.metadata_calls, before)

    def test_not_sent_and_expired_are_reported_without_resend_or_actor_cleanup(self):
        with self.bus.store.connection() as db:
            db.execute(f"UPDATE {TABLE} SET state='not_sent' WHERE actor=?", (self.a["id"],))
        outcome = next(x for x in self.runtime.bridge.reconcile_dispatches()[self.goal]["outcomes"] if x["agent_id"] == self.a["id"])
        self.assertEqual(outcome["status"], "pending_capability")
        self.assertTrue(outcome["native_input_not_sent"])
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0 WHERE id=?", (self.root,))
        outcome = next(x for x in self.runtime.bridge.reconcile_dispatches()[self.goal]["outcomes"] if x["agent_id"] == self.a["id"])
        self.assertEqual(outcome["status"], "expired")
        self.assertEqual(self.bus.store.agent(self.a["id"])["state"], "awaiting_native_reply")
        self.assertEqual(len(self.sends), 2)

    def test_parent_progress_never_claims_that_a_child_model_started(self):
        self.bus.store.send(self.b["id"], "user", "Parent progress", "progress", in_reply_to=self.original[self.b["id"]]["event_id"])
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        self.runtime.drain_native_peers()
        outcome = next(x for x in self.runtime.bridge.reconcile_dispatches()[self.goal]["outcomes"] if x["job_id"] == child["jobs"][0]["id"])
        self.assertEqual(outcome["status"], "awaiting_native_reply")
        self.bus.store.send(self.b["id"], "user", "Child explicit progress", "progress", in_reply_to=child["event"]["id"])
        outcome = next(x for x in self.runtime.bridge.reconcile_dispatches()[self.goal]["outcomes"] if x["job_id"] == child["jobs"][0]["id"])
        self.assertEqual(outcome["status"], "working")

    def test_rotating_mailbox_heads_do_not_starve_ninth_ready_peer(self):
        peers = [self.bus.store.register("codex", "Extra peer " + str(i), [], "external_attached", native_session_id="fixture-extra-" + str(i)) for i in range(10)]
        result = self.runtime.prompt({"text": "Bounded fairness fixture with an immutable initial roster", "idempotency_key": "fairness-human"})
        goal = result["goal"]["id"]
        sender, token = peers[0]
        children = []
        for i, (target, _) in enumerate(peers[1:]):
            children.append(self.runtime.peer(token, {"operation": "send-keys", "root_id": goal, "recipient": target["id"],
                "body": "Review scope " + str(i), "kind": "question", "idempotency_key": "fair-" + str(i)}))
        last_actor = peers[-1][0]
        with self.bus.store.connection() as db:
            parent = dict(db.execute("SELECT * FROM jobs WHERE agent_id=? AND event_id=?", (last_actor["id"], result["dispatch"]["event_id"])).fetchone())
        self.finish(last_actor, parent)
        before = len(self.sends)
        self.runtime.native_dispatcher.drain(limit=8)
        self.assertEqual(len(self.sends), before)
        self.runtime.native_dispatcher.drain(limit=8)
        self.assertEqual(len(self.sends), before + 1)
        self.assertEqual(self.sends[-1]["target"], last_actor["native_session_id"])
        self.assertEqual(self.child_job(children[-1])["state"], "awaiting_native_reply")

    def test_expired_unsent_head_does_not_block_newer_root_for_same_peer(self):
        old = self.peer(self.a, self.b, "old-root")
        self.finish(self.a, self.original[self.a["id"]])
        self.finish(self.b, self.original[self.b["id"]])
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0 WHERE id=?", (self.root,))
        fresh = self.runtime.prompt({"text": "A separate authorized new human goal", "idempotency_key": "new-human"})
        child = self.runtime.peer(self.a_token, {"operation": "send-keys", "root_id": fresh["goal"]["id"], "recipient": self.b["id"],
            "body": "Review the new root's scope", "kind": "question", "idempotency_key": "new-root-child"})
        with self.bus.store.connection() as db:
            parent = dict(db.execute("SELECT * FROM jobs WHERE event_id=? AND agent_id=?", (fresh["dispatch"]["event_id"], self.b["id"])).fetchone())
        self.finish(self.b, parent)
        before = len(self.sends)
        self.runtime.drain_native_peers()
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), before + 1)
        self.assertEqual(self.child_job(old)["state"], "avalokita_expired")
        self.assertEqual(self.child_job(child)["state"], "awaiting_native_reply")

    def test_pause_keeps_unattempted_child_resumable_before_expiry(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        self.runtime.control("pause")
        self.runtime.drain_native_peers()
        self.assertEqual(self.child_job(child)["state"], "avalokita_native_pending")
        self.assertEqual(len(self.sends), 2)
        self.runtime.control("resume")
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 3)

    def test_changed_original_human_input_after_child_reservation_fails_closed(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        with self.bus.store.connection() as db:
            db.execute("UPDATE events SET body='Changed human authority' WHERE id=?", (self.response["dispatch"]["event_id"],))
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 2)
        self.assertEqual(self.child_job(child)["state"], "awaiting_native_reply")
        # The committed attempt marker fails closed before adapter I/O; it is
        # retained as ambiguous rather than retried with another envelope.
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 2)

    def not_sent_human(self):
        self.finish(self.a, self.original[self.a["id"]])
        self.not_sent = True
        fresh = self.runtime.prompt({"text": "A fresh exact pre-I/O repair fixture", "idempotency_key": "pre-io-human", "recipient_ids": [self.a["id"]]})
        with self.bus.store.connection() as db:
            dispatch = dict(db.execute(f"SELECT * FROM {TABLE} WHERE goal_id=?", (fresh["goal"]["id"],)).fetchone())
            root = tuple(db.execute("SELECT used,budget,deadline FROM roots WHERE id=?", (dispatch["root_id"],)).fetchone())
        self.assertEqual(dispatch["state"], "not_sent")
        return fresh, dispatch, root

    def test_operator_exact_not_sent_repair_releases_actor_without_replay(self):
        fresh, dispatch, before = self.not_sent_human()
        value = self.runtime.native_dispatcher.classify_not_sent(fresh["goal"]["id"], self.a["id"], dispatch["dispatch_id"])
        self.assertTrue(value["classified"])
        self.assertEqual(self.bus.store.agent(self.a["id"])["state"], "idle")
        self.assertEqual(self.bus.store.agent(self.b["id"])["state"], "awaiting_native_reply")
        self.assertEqual(len(self.sends), 3)
        with self.bus.store.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT used,budget,deadline FROM roots WHERE id=?", (dispatch["root_id"],)).fetchone()), before)
            self.assertEqual(db.execute(f"SELECT receipt FROM {TABLE} WHERE dispatch_id=?", (dispatch["dispatch_id"],)).fetchone()[0], dispatch["receipt"])
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE id=?", (dispatch["job_id"],)).fetchone()[0], "avalokita_not_sent")
        again = self.runtime.native_dispatcher.classify_not_sent(fresh["goal"]["id"], self.a["id"], dispatch["dispatch_id"])
        self.assertTrue(again["duplicate_suppressed"])
        self.assertEqual(len(self.sends), 3)

    def test_operator_repair_rejects_unknown_rebind_other_job_and_missing_transport_proof(self):
        fresh, dispatch, _ = self.not_sent_human()
        repair = lambda: self.runtime.native_dispatcher.classify_not_sent(fresh["goal"]["id"], self.a["id"], dispatch["dispatch_id"])
        with self.bus.store.connection() as db:
            db.execute(f"UPDATE {TABLE} SET state='delivery_unknown' WHERE dispatch_id=?", (dispatch["dispatch_id"],))
        with self.assertRaises(BridgeError):
            repair()
        with self.bus.store.connection() as db:
            db.execute(f"UPDATE {TABLE} SET state='not_sent' WHERE dispatch_id=?", (dispatch["dispatch_id"],))
        self.bus.store.update_agent(self.a["id"], native_session_id="rebound")
        with self.assertRaises(BridgeError):
            repair()
        self.bus.store.update_agent(self.a["id"], native_session_id=self.a["native_session_id"])
        foreign = self.bus.store.send("user", self.a["id"], "A separate queued human request")
        foreign_job = foreign["jobs"][0]["id"]
        with self.bus.store.connection() as db:
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (foreign_job,))
        with self.assertRaises(BridgeError):
            repair()
        with self.bus.store.connection() as db:
            db.execute("UPDATE jobs SET state='completed' WHERE id=?", (foreign_job,))
        import sqlite3
        with closing(sqlite3.connect(self.runtime.bridge.receipts.parent / "codex-ipc.sqlite3")) as db:
            with db:
                db.execute("DELETE FROM codex_dispatch WHERE dispatch_id=?", (dispatch["dispatch_id"],))
        with self.assertRaises(BridgeError):
            repair()
        self.assertEqual(self.bus.store.agent(self.a["id"])["state"], "awaiting_native_reply")
        self.assertEqual(len(self.sends), 3)

    def test_idle_receiver_is_contacted_once_with_exact_inherited_parent(self):
        self.finish(self.b, self.original[self.b["id"]])
        child = self.peer(self.a, self.b)
        self.assertEqual(child["state"], "accepted")
        self.assertEqual(child["native_delivery"]["state"], "native_accepted")
        self.assertEqual(self.child_job(child)["state"], "awaiting_native_reply")
        self.assertEqual(len(self.sends), 3)
        self.assertEqual(self.used(), 3)

    def test_cold_background_reconnect_drains_without_fabricating_environment(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        with patch.dict(os.environ, {}, clear=True):
            self.runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=self.factory)
            self.sent.clear()
            self.runtime.start_peer_reconciler()
            self.assertTrue(self.sent.wait(4))
            self.assertIsNone(os.environ.get("CODEX_THREAD_ID"))
            self.assertEqual(self.child_job(child)["state"], "awaiting_native_reply")
            self.runtime.stop_peer_reconciler()
        self.assertEqual(len(self.sends), 3)

    def test_committed_never_attempted_claim_can_resume_once_after_process_restart(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        with patch.object(self.runtime.native_dispatcher, "deliver", side_effect=OSError("fixture exit before attempt")):
            self.runtime.drain_native_peers()
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute(f"SELECT state FROM {TABLE} WHERE dispatch_id=?", (child["dispatch_id"],)).fetchone()[0], "claimed")
        self.assertEqual(len(self.sends), 2)
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=self.factory)
        self.runtime.drain_native_peers()
        self.runtime.drain_native_peers()
        self.assertEqual(len(self.sends), 3)
        self.assertEqual(self.used(), 3)

    def test_expired_never_attempted_claim_releases_only_its_exact_actor(self):
        child = self.peer(self.a, self.b)
        self.finish(self.b, self.original[self.b["id"]])
        with patch.object(self.runtime.native_dispatcher, "deliver", side_effect=OSError("fixture exit before attempt")):
            self.runtime.drain_native_peers()
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0 WHERE id=?", (self.root,))
        self.runtime.drain_native_peers()
        self.assertEqual(self.child_job(child)["state"], "avalokita_expired")
        self.assertEqual(self.bus.store.agent(self.b["id"])["state"], "idle")
        self.assertEqual(self.bus.store.agent(self.a["id"])["state"], "awaiting_native_reply")
        self.assertEqual(self.used(), 3)
        self.assertEqual(len(self.sends), 2)


if __name__ == "__main__":
    unittest.main()
