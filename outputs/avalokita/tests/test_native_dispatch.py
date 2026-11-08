"""Disposable durable bus + fake pipe. Native applications are never opened."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError
from daemon import BusHTTPServer
from native_dispatch import NativeDispatcher, TABLE, complete
from server import Runtime
from adapters.codex_app_tools import CodexAppToolsAdapter
from adapters.codex_app_tools import CodexPipeError
from adapters.codex_app_tools import PROVENANCE, SUPPORTED_PROVENANCES
from test_codex_app_tools import FakePipe, FakeSystem, SEND_TOOL, SID, inventory, rpc_result


class RoutingMetadata:
    """Model-free fake of the pinned transport's source compatibility probe."""
    def discover(self):
        return {"same_handle_server_verified": True, "native_message_sent": False,
                "owner_sid_sha256": hashlib.sha256(SID.encode()).hexdigest()}

    def probe_metadata(self, source):
        return RoutingMetadata.discover(self)


class NativeDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CODEX_THREAD_ID": "fixture-source-native"})
        self.env.start()
        self.bus = BusHTTPServer(self.workspace, enable_native=False, discovery=[])
        (self.bus.store.private / "runtime.json").write_text(json.dumps(self.bus.runtime()), encoding="utf-8")
        self.thread = threading.Thread(target=self.bus.serve_forever, daemon=True)
        self.thread.start()
        self.actor, self.token = self.bus.store.register("codex", "Existing CEO", ["selected-public"], "external_attached", role="CEO", native_session_id="fixture-original-codex")
        self.runtime = Runtime(self.workspace, discovery=False)
        self.unregister_completion = self.bus.store.register_native_completion_handler(self.runtime.complete_native_result)
        goal = self.runtime.store.new_goal("Fixture proof only. Do not edit business files.", idempotency_key="fixture-root")
        self.goal = {"goal": goal, "dispatch": self.runtime.bridge.dispatch(goal, goal["prompt"], native_ipc_ids=[self.actor["id"]])}
        self.pipe = FakePipe(inventory([SEND_TOOL]) + rpc_result(2, {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps({"threadId": "fixture-original-codex"})}]}))
        self.system = FakeSystem(self.pipe)
        self.factory_calls = 0
        def factory(**kwargs):
            self.factory_calls += 1
            return CodexAppToolsAdapter(system=self.system, **kwargs)
        self.dispatcher = NativeDispatcher(self.runtime.bridge, adapter_factory=factory)

    def tearDown(self):
        self.unregister_completion()
        self.bus.shutdown()
        self.bus.close()
        self.env.stop()
        self.temp.cleanup()

    def reserve(self):
        return self.dispatcher.reserve(self.goal["goal"]["id"], self.actor["id"])

    def test_atomic_claim_authority_root_budget_and_exact_envelope(self):
        claim = self.reserve()
        job = self.bus.store.jobs()[0]
        self.assertEqual(job["state"], "awaiting_native_reply")
        self.assertIsNone(self.bus.store.claim(self.actor["id"]))
        with self.bus.store.connection() as db:
            root = db.execute("SELECT * FROM roots WHERE id=?", (claim["root_id"],)).fetchone()
            self.assertEqual(root["used"], 1)
            auth = dict(db.execute(f"SELECT * FROM {TABLE}").fetchone())
        self.assertEqual(auth["native_id"], "fixture-original-codex")
        self.assertEqual(auth["source_id"], "fixture-source-native")
        self.assertEqual(self.factory_calls, 0)
        accepted = self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(accepted["state"], "native_accepted")
        self.assertFalse(accepted["native_result_verified"])
        tool_call = self.pipe.writes[1]
        self.assertEqual(tool_call["params"]["tool"], "send_message_to_thread")
        args = tool_call["params"]["arguments"]
        self.assertEqual(args["threadId"], "fixture-original-codex")
        self.assertEqual(hashlib.sha256(args["prompt"].encode()).hexdigest(), auth["prompt_sha256"])
        self.assertIn(claim["job_id"], args["prompt"])
        self.assertIn(claim["event_id"], args["prompt"])
        self.assertIn("HARMONY.md", args["prompt"])
        self.assertNotIn(self.token, args["prompt"])

    def test_preassigned_direct_job_cannot_be_reassigned_to_supervisor(self):
        with self.bus.store.connection() as db:
            db.execute("UPDATE jobs SET state='queued'")
        self.assertIsNone(self.bus.store.claim(self.actor["id"]))
        self.assertEqual(self.bus.store.jobs()[0]["execution_owner"], "native_dispatch")
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.system.opens, [])
        claim = self.reserve()
        self.assertEqual(claim["state"], "claimed")
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.system.opens, [])

    def test_no_human_root_or_forged_root_metadata_cannot_authorize(self):
        with self.assertRaises(BridgeError):
            self.dispatcher.reserve("peer-invented-root", self.actor["id"])
        with self.bus.store.connection() as db:
            db.execute("UPDATE events SET metadata='{}' WHERE sender='user'")
        with self.assertRaises(BridgeError):
            self.reserve()
        self.assertEqual(self.factory_calls, 0)

    def test_final_reserved_turn_can_send_once_without_resetting_budget(self):
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET budget=1")
        claim = self.reserve()
        self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "native_accepted")
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT used,budget FROM roots").fetchone()[:], (1, 1))

    def test_restart_same_authorization_cannot_resend(self):
        claim = self.reserve()
        self.dispatcher.deliver(claim["dispatch_id"])
        restarted = NativeDispatcher(self.runtime.bridge, adapter_factory=self.dispatcher.adapter_factory)
        duplicate = restarted.deliver(claim["dispatch_id"])
        self.assertTrue(duplicate["duplicate_suppressed"])
        self.assertEqual(len(self.system.opens), 1)
        self.assertEqual(len(self.pipe.writes), 2)
        self.assertEqual(self.reserve()["dispatch_id"], claim["dispatch_id"])

    def test_unknown_write_never_requeues_or_falls_back(self):
        self.pipe.response = bytearray(inventory([SEND_TOOL]))
        claim = self.reserve()
        result = self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(result["state"], "delivery_unknown")
        self.assertFalse(result["fallback_attempted"])
        self.assertEqual(self.bus.store.jobs()[0]["state"], "awaiting_native_reply")
        self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(len(self.system.opens), 1)

    def test_attempt_marker_survives_crash_and_prevents_replay(self):
        claim = self.reserve()
        with self.bus.store.connection() as db:
            db.execute(f"UPDATE {TABLE} SET state='attempting'")
        result = self.dispatcher.deliver(claim["dispatch_id"])
        self.assertTrue(result["duplicate_suppressed"])
        self.assertEqual(self.factory_calls, 0)

    def test_paused_rebound_expired_and_missing_routing_context_fail_before_native_io(self):
        claim = self.reserve()
        self.runtime.control("pause")
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.runtime.control("resume")
        self.bus.store.update_agent(self.actor["id"], native_session_id="rebound")
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.bus.store.update_agent(self.actor["id"], native_session_id="fixture-original-codex")
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0")
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(self.factory_calls, 0)

    def test_pause_admission_waits_until_already_admitted_bounded_call_finishes(self):
        claim = self.reserve()
        entered = threading.Event()
        release = threading.Event()
        completed_pause = threading.Event()
        original = self.dispatcher.adapter_factory
        def factory(**kwargs):
            adapter = original(**kwargs)
            send = adapter.send_authorized
            def blocked(*args, **values):
                entered.set()
                self.assertTrue(release.wait(15))
                return send(*args, **values)
            adapter.send_authorized = blocked
            return adapter
        self.dispatcher.adapter_factory = factory
        delivered = []
        delivery = threading.Thread(target=lambda: delivered.append(self.dispatcher.deliver(claim["dispatch_id"])))
        pause = threading.Thread(target=lambda: (self.runtime.control("pause"), completed_pause.set()))
        delivery.start()
        try:
            # Windows path verification precedes this barrier and is outside
            # the bounded native call. It may be slow on a loaded filesystem.
            self.assertTrue(entered.wait(15))
            pause.start()
            self.assertFalse(completed_pause.wait(.1))
        finally:
            release.set()
            delivery.join(15)
            if pause.ident:
                pause.join(15)
        self.assertFalse(delivery.is_alive())
        self.assertFalse(pause.is_alive())
        self.assertTrue(completed_pause.is_set())
        self.assertEqual(delivered[0]["state"], "native_accepted")
        self.assertTrue(self.runtime.bridge.paused())

    def test_exact_authenticated_completion_and_unrelated_callback_rejected(self):
        claim = self.reserve()
        self.dispatcher.deliver(claim["dispatch_id"])
        with self.assertRaises(BridgeError):
            complete(self.workspace, actor=self.actor["id"], native_session_id="wrong", job_id=claim["job_id"], event_id=claim["event_id"], text="wrong")
        result = complete(self.workspace, actor=self.actor["id"], native_session_id="fixture-original-codex", job_id=claim["job_id"], event_id=claim["event_id"], text="Selected native fixture result")
        self.assertTrue(result["completed"])
        self.assertEqual(self.bus.store.jobs()[0]["state"], "completed")
        state = self.runtime.state()
        self.assertTrue(any(m["body"] == "Selected native fixture result" for m in state["messages"]))
        self.assertTrue(self.bus.store.agent(self.actor["id"])["channel_verified"])

    def test_missing_source_routing_context_cannot_reserve_job(self):
        with patch.dict(os.environ, {}, clear=True):
            dispatcher = NativeDispatcher(self.runtime.bridge)
            with self.assertRaises(BridgeError):
                dispatcher.reserve(self.goal["goal"]["id"], self.actor["id"])
        self.assertEqual(self.bus.store.jobs()[0]["state"], "avalokita_native_pending")

    def test_harmony_stopped_paused_expired_exhausted_authority_blocks_reserve(self):
        root = self.goal["goal"]["id"]
        self.runtime.store.set_goal_state(root, "paused")
        with self.assertRaises(BridgeError):
            self.reserve()
        self.runtime.store.set_goal_state(root, "active")
        with self.runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET used=budget WHERE id=?", (root,))
        with self.assertRaises(BridgeError):
            self.reserve()
        with self.runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET used=0,deadline=0 WHERE id=?", (root,))
        with self.assertRaises(BridgeError):
            self.reserve()
        self.assertEqual(self.factory_calls, 0)

    def test_harmony_terminal_stop_between_claim_and_delivery_prevents_io(self):
        claim = self.reserve()
        self.runtime.store.set_goal_state(self.goal["goal"]["id"], "stopped")
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(self.factory_calls, 0)

    def test_runtime_exact_native_subset_is_never_visible_to_legacy_queue(self):
        with self.bus.store.connection() as db:
            db.execute("UPDATE jobs SET state='completed'")
        other, _ = self.bus.store.register("claude", "Other existing CEO", [], "native_attached", role="CEO", native_session_id="fixture-other-native")
        discoveries = []
        class Metadata(RoutingMetadata):
            def discover(self):
                discoveries.append(True)
                return super().discover()
            def probe_metadata(self, source):
                discoveries.append(True)
                return super().probe_metadata(source)
        def factory(**kwargs):
            return CodexAppToolsAdapter(system=self.system, **kwargs) if "ledger_path" in kwargs else Metadata()
        runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
        runtime.callback = "http://127.0.0.1:54321"
        original = runtime.bridge._receipt
        def receipt(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[3] == "queued" and not value.get("recovered"):
                with self.bus.store.connection() as db:
                    target = db.execute("SELECT state FROM jobs WHERE event_id=? AND agent_id=?", (value["event_id"], self.actor["id"])).fetchone()
                self.assertEqual(target[0], "avalokita_native_pending")
                self.assertIsNone(self.bus.store.claim(self.actor["id"]))
            return value
        payload = {"text": "Single exact native fixture proof", "idempotency_key": "native-subset", "recipient_ids": [self.actor["id"]], "require_native_ipc": True}
        with patch.object(runtime.bridge, "_receipt", side_effect=receipt):
            response = runtime.prompt(payload)
        self.assertEqual(response["dispatch"]["participants"], [self.actor["id"]])
        self.assertEqual(response["native_delivery"][0]["state"], "native_accepted")
        self.assertEqual(len(self.pipe.writes), 2)
        self.assertIn("--url http://127.0.0.1:54321", self.pipe.writes[1]["params"]["arguments"]["prompt"])
        self.assertFalse(any(j["agent_id"] == other["id"] for j in self.bus.store.jobs()))
        # Native host may now be busy. The same human key still recovers the
        # original transport receipt without capability probes or a second send.
        repeated = runtime.prompt(payload)
        self.assertEqual(repeated["goal"]["id"], response["goal"]["id"])
        self.assertEqual(len(discoveries), 1)
        self.assertEqual(len(self.pipe.writes), 2)
        with self.assertRaises(BridgeError):
            runtime.prompt({**payload, "require_native_ipc": False})
        with self.assertRaises(BridgeError):
            runtime.prompt({**payload, "recipient_ids": [other["id"]]})

    def test_required_ipc_unavailable_creates_no_goal_or_job(self):
        def missing(**kwargs):
            raise CodexPipeError("Fixture unavailable")
        runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=missing)
        before_goals = len(runtime.store.snapshot()["goals"])
        before_jobs = len(self.bus.store.jobs())
        with self.assertRaises(BridgeError):
            runtime.prompt({"text": "Must not queue anywhere", "idempotency_key": "unavailable-native", "recipient_ids": [self.actor["id"]], "require_native_ipc": True})
        self.assertEqual(len(runtime.store.snapshot()["goals"]), before_goals)
        self.assertEqual(len(self.bus.store.jobs()), before_jobs)

    def test_required_ipc_without_source_creates_no_goal_or_job(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = Runtime(self.workspace, discovery=False, native_ipc=True)
            with self.assertRaises(BridgeError):
                runtime.prompt({"text": "No guessed routing context", "idempotency_key": "no-source", "recipient_ids": [self.actor["id"]], "require_native_ipc": True})
        self.assertEqual(len(runtime.store.snapshot()["goals"]), 1)
        self.assertEqual(len(self.bus.store.jobs()), 1)

    def test_native_question_answer_keeps_exact_ipc_route(self):
        # Existing exact native question, with its currently owned waiting job.
        claim = self.reserve()
        self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "native_accepted")
        self.assertEqual(len(self.pipe.writes), 2)
        question = self.bus.store.send(self.actor["id"], "user", "Which fixture version?", "question")
        self.pipe = FakePipe(inventory([SEND_TOOL]) + rpc_result(2, {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps({"threadId": "fixture-original-codex"})}]}))
        self.system = FakeSystem(self.pipe)
        def factory(**kwargs):
            return CodexAppToolsAdapter(system=self.system, **kwargs) if "ledger_path" in kwargs else RoutingMetadata()
        runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
        result = runtime.prompt({"text": "Use version 2", "idempotency_key": "exact-native-answer", "reply_to": question["event"]["id"],
                                 "recipient_ids": [self.actor["id"]], "require_native_ipc": True})
        self.assertEqual(result["native_delivery"][0]["state"], "native_accepted")
        self.assertEqual(result["dispatch"]["native_ipc_ids"], [self.actor["id"]])
        self.assertEqual(len(self.pipe.writes), 2)
        with self.bus.store.connection() as db:
            event = db.execute("SELECT metadata FROM events WHERE id=?", (result["dispatch"]["event_id"],)).fetchone()
        self.assertEqual(json.loads(event[0])["in_reply_to"], question["event"]["id"])

    def test_default_composer_only_routes_ready_codex_not_registered_claude(self):
        with self.bus.store.connection() as db:
            db.execute("UPDATE jobs SET state='completed'")
        other, _ = self.bus.store.register("claude", "Registered but unready", [], "native_attached", native_session_id="unready-original")
        def factory(**kwargs):
            return CodexAppToolsAdapter(system=self.system, **kwargs) if "ledger_path" in kwargs else RoutingMetadata()
        runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
        response = runtime.prompt({"text": "안녕하세요, 자연스럽게 답해 주세요.", "idempotency_key": "default-composer"})
        self.assertEqual(response["dispatch"]["participants"], [self.actor["id"]])
        self.assertEqual(response["native_delivery"][0]["state"], "native_accepted")
        self.assertEqual(response["dispatch"]["status"], "awaiting_native_reply")
        self.assertTrue(response["dispatch"]["durable_accepted"])
        self.assertFalse(any(job["agent_id"] == other["id"] for job in self.bus.store.jobs()))
        self.assertEqual(next(item for item in response["dispatch"]["skipped"] if item["agent_id"] == other["id"])["reason"], "native_wake_not_verified")
        prompt = self.pipe.writes[1]["params"]["arguments"]["prompt"]
        self.assertIn("readable natural language", prompt)
        self.assertIn("previous diagnostic/proof", prompt)

    def test_cold_default_composer_saves_without_unverified_queue_or_send(self):
        with patch.dict(os.environ, {}, clear=True):
            runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=self.dispatcher.adapter_factory)
            count = len(self.bus.store.jobs())
            response = runtime.prompt({"text": "Cold ordinary prompt", "idempotency_key": "cold-default"})
            self.assertEqual(response["dispatch"]["state"], "waiting")
            self.assertEqual(response["dispatch"]["status"], "pending_capability")
            self.assertTrue(response["dispatch"]["durable_accepted"])
            self.assertEqual(len(self.bus.store.jobs()), count)
            self.assertEqual(self.factory_calls, 0)
            self.assertEqual(self.system.opens, [])

    def test_explicit_unready_recipient_fails_before_goal_or_job(self):
        other, _ = self.bus.store.register("claude", "Unready", [], "native_attached", native_session_id="unready-existing")
        goals, jobs = len(self.runtime.store.snapshot()["goals"]), len(self.bus.store.jobs())
        with self.assertRaises(BridgeError):
            self.runtime.prompt({"text": "Do not enqueue", "idempotency_key": "explicit-unready", "recipient_ids": [other["id"]]})
        self.assertEqual(len(self.runtime.store.snapshot()["goals"]), goals)
        self.assertEqual(len(self.bus.store.jobs()), jobs)

    def test_unknown_transport_reconciles_exact_callback_after_pause_expiry_and_restart(self):
        self.pipe.response = bytearray(inventory([SEND_TOOL]))
        claim = self.reserve()
        self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "delivery_unknown")
        complete(self.workspace, actor=self.actor["id"], native_session_id="fixture-original-codex",
                 job_id=claim["job_id"], event_id=claim["event_id"], text="실제 작업 결과를 읽기 쉽게 전달합니다.")
        root = self.goal["goal"]["id"]
        self.runtime.store.set_goal_state(root, "stopped")
        self.runtime.control("pause")
        with self.runtime.store._connection(write=True) as db:
            before = tuple(db.execute("SELECT state,budget,used FROM goals WHERE id=?", (root,)).fetchone())
            db.execute("UPDATE goals SET deadline=0 WHERE id=?", (root,))
        restarted = Runtime(self.workspace, discovery=False)
        state = restarted.state()
        outcome = state["dispatches"][root]
        self.assertEqual(outcome["status"], "completed")
        self.assertTrue(outcome["delivery_verified"])
        self.assertEqual(outcome["outcomes"][0]["transport_state"], "delivery_unknown")
        self.assertIn("실제 작업 결과를 읽기 쉽게 전달합니다.", [item["body"] for item in state["messages"]])
        with restarted.store._connection() as db:
            self.assertEqual(tuple(db.execute("SELECT state,budget,used FROM goals WHERE id=?", (root,)).fetchone()), before)
        self.assertEqual(len(self.pipe.writes), 2)
        self.assertEqual(len(self.system.opens), 1)

    def test_cold_launch_uses_sealed_observed_source_without_fabricating_environment(self):
        seed = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: RoutingMetadata(), harmony_store=self.runtime.store)
        self.assertEqual(seed.available()["state"], "metadata_verified")
        saved = json.loads(seed.routing_path.read_text(encoding="utf-8"))["record"]
        self.assertEqual(saved["source_thread_id"], "fixture-source-native")
        self.assertEqual(saved["origin"], "observed_CODEX_THREAD_ID")
        self.assertFalse(saved["source_thread_existence_verified"])
        self.assertFalse(saved["metadata_is_native_attestation"])
        def factory(**kwargs):
            return CodexAppToolsAdapter(system=self.system, **kwargs) if "ledger_path" in kwargs else RoutingMetadata()
        with patch.dict(os.environ, {}, clear=True):
            runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
            response = runtime.prompt({"text": "Ordinary cold composer", "idempotency_key": "cold-observed-source"})
            self.assertEqual(response["dispatch"]["participants"], [self.actor["id"]])
            self.assertEqual(response["native_delivery"][0]["state"], "native_accepted")
            self.assertIsNone(os.environ.get("CODEX_THREAD_ID"))
            self.assertEqual(self.pipe.writes[1]["params"]["threadId"], saved["source_thread_id"])
            self.assertEqual(self.pipe.writes[1]["params"]["arguments"]["threadId"], "fixture-original-codex")
            restarted = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
            restarted.state()
            self.assertEqual(restarted.prompt({"text": "Ordinary cold composer", "idempotency_key": "cold-observed-source"})["goal"]["id"], response["goal"]["id"])
        self.assertEqual(len(self.pipe.writes), 2)

    def test_cold_routing_tamper_owner_or_version_change_fail_closed(self):
        seed = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: RoutingMetadata())
        self.assertEqual(seed.available()["state"], "metadata_verified")
        original = seed.routing_path.read_text(encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            for field, value in (("source_thread_id", "invented"), ("workspace_id", "wrong"), ("workspace", "C:/other")):
                altered = json.loads(original)
                altered["record"][field] = value
                seed.routing_path.write_text(json.dumps(altered), encoding="utf-8")
                dispatcher = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: self.fail("Tamper must fail before native metadata I/O"))
                self.assertEqual(dispatcher.available()["state"], "pending_capability")
            seed.routing_path.write_text(original, encoding="utf-8")
            class WrongOwner(RoutingMetadata):
                def discover(self):
                    return {**super().discover(), "owner_sid_sha256": "0" * 64}
                def probe_metadata(self, source):
                    return self.discover()
            dispatcher = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: WrongOwner())
            self.assertEqual(dispatcher.available()["state"], "pending_capability")
            with patch("native_dispatch.PROVENANCE", {"changed_version": True}):
                dispatcher = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: self.fail("Changed pins must fail before native I/O"))
                self.assertEqual(dispatcher.available()["state"], "pending_capability")
        self.assertEqual(self.pipe.writes, [])

    def test_known_old_sealed_routing_migrates_only_after_fresh_current_same_owner_probe(self):
        seed = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: RoutingMetadata())
        self.assertEqual(seed.available()["state"], "metadata_verified")
        saved = json.loads(seed.routing_path.read_text(encoding="utf-8"))
        saved["record"]["provenance"] = SUPPORTED_PROVENANCES[0]
        saved["seal"] = hmac.new(seed._routing_key(), seed._routing_bytes(saved["record"]), hashlib.sha256).hexdigest()
        seed.routing_path.write_text(json.dumps(saved), encoding="utf-8")
        probes = []
        class Fresh(RoutingMetadata):
            def probe_metadata(self, source):
                probes.append(source)
                return super().probe_metadata(source)
        with patch.dict(os.environ, {}, clear=True):
            cold = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: Fresh())
            self.assertFalse(cold._source_allowed(cold.source))
            self.assertEqual(cold.available()["state"], "metadata_verified")
            self.assertEqual(probes, [saved["record"]["source_thread_id"]])
            self.assertIsNone(os.environ.get("CODEX_THREAD_ID"))
            migrated = json.loads(cold.routing_path.read_text(encoding="utf-8"))["record"]
            self.assertEqual(migrated["provenance"], PROVENANCE)
            self.assertEqual(migrated["observed_at"], saved["record"]["observed_at"])
            self.assertEqual(migrated["source_thread_id"], saved["record"]["source_thread_id"])
            self.assertFalse(migrated["source_thread_existence_verified"])
            self.assertFalse(migrated["metadata_is_native_attestation"])
        self.assertEqual(self.pipe.writes, [])

    def test_sealed_unknown_version_and_old_owner_mismatch_never_reseal_or_send(self):
        seed = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: RoutingMetadata())
        seed.available()
        saved = json.loads(seed.routing_path.read_text(encoding="utf-8"))
        saved["record"]["provenance"] = {**SUPPORTED_PROVENANCES[0], "application_version": "uninspected"}
        saved["seal"] = hmac.new(seed._routing_key(), seed._routing_bytes(saved["record"]), hashlib.sha256).hexdigest()
        seed.routing_path.write_text(json.dumps(saved), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            rejected = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: self.fail("Unknown provenance cannot probe"))
            self.assertEqual(rejected.available()["state"], "pending_capability")
            saved["record"]["provenance"] = SUPPORTED_PROVENANCES[0]
            saved["seal"] = hmac.new(seed._routing_key(), seed._routing_bytes(saved["record"]), hashlib.sha256).hexdigest()
            original = json.dumps(saved)
            seed.routing_path.write_text(original, encoding="utf-8")
            class Wrong(RoutingMetadata):
                def probe_metadata(self, source):
                    return {**super().probe_metadata(source), "owner_sid_sha256": "0" * 64}
            rejected = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: Wrong())
            self.assertEqual(rejected.available()["state"], "pending_capability")
            self.assertEqual(seed.routing_path.read_text(encoding="utf-8"), original)
        self.assertEqual(self.pipe.writes, [])

    def test_waiting_input_recovers_once_when_route_arrives_and_never_replays_after_restart(self):
        def factory(**kwargs):
            return CodexAppToolsAdapter(system=self.system, **kwargs) if "ledger_path" in kwargs else RoutingMetadata()
        with patch.dict(os.environ, {}, clear=True):
            waiting_runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
            saved = waiting_runtime.prompt({"text": "Saved while source unavailable", "idempotency_key": "late-ready"})
        root = saved["goal"]["id"]
        self.assertEqual(saved["dispatch"]["state"], "waiting")
        runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
        state = runtime.state()
        self.assertEqual(state["dispatches"][root]["status"], "awaiting_native_reply")
        job_ids = [job["id"] for job in self.bus.store.jobs() if job["root_id"] == saved["dispatch"]["root_id"]]
        self.assertEqual(len(job_ids), 1)
        runtime.state()
        with patch.dict(os.environ, {}, clear=True):
            Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory).state()
        self.assertEqual(len(self.pipe.writes), 2)
        self.assertEqual([job["id"] for job in self.bus.store.jobs() if job["root_id"] == saved["dispatch"]["root_id"]], job_ids)

    def test_waiting_recovery_pause_stop_expiry_and_unknown_never_send(self):
        def factory(**kwargs):
            return CodexAppToolsAdapter(system=self.system, **kwargs) if "ledger_path" in kwargs else RoutingMetadata()
        with patch.dict(os.environ, {}, clear=True):
            runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
            saved = [runtime.prompt({"text": "Blocked " + str(index), "idempotency_key": "blocked-" + str(index)}) for index in range(3)]
        runtime.store.set_goal_state(saved[0]["goal"]["id"], "stopped")
        with runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET deadline=0 WHERE id=?", (saved[1]["goal"]["id"],))
        value = saved[2]["dispatch"]
        runtime.bridge._receipt(saved[2]["goal"]["id"], value["root_id"], value["event_id"], "unknown", {"participants": [], "pending": True})
        ready = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
        ready.control("pause")
        ready.state()
        ready.control("resume")
        ready.state()
        self.assertEqual(self.pipe.writes, [])
        self.assertEqual(len(self.bus.store.jobs()), 1)

    def test_more_than_eight_waiting_goals_do_not_starve_later_live_goal(self):
        def factory(**kwargs):
            return CodexAppToolsAdapter(system=self.system, **kwargs) if "ledger_path" in kwargs else RoutingMetadata()
        with patch.dict(os.environ, {}, clear=True):
            runtime = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
            saved = [runtime.prompt({"text": "Pending " + str(index), "idempotency_key": "fair-" + str(index)}) for index in range(9)]
        for value in saved[:8]:
            runtime.store.set_goal_state(value["goal"]["id"], "stopped")
        ready = Runtime(self.workspace, discovery=False, native_ipc=True, native_factory=factory)
        ready.state()
        self.assertEqual(self.pipe.writes, [])
        state = ready.state()
        self.assertEqual(state["dispatches"][saved[8]["goal"]["id"]]["status"], "awaiting_native_reply")
        ready.state()
        self.assertEqual(len(self.pipe.writes), 2)

    def test_cold_process_probes_source_once_and_rejection_never_sends(self):
        seed = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: RoutingMetadata())
        self.assertEqual(seed.available()["state"], "metadata_verified")
        calls = {"probe": 0, "discover": 0}
        class Counting(RoutingMetadata):
            def discover(self):
                calls["discover"] += 1
                return super().discover()
            def probe_metadata(self, source):
                calls["probe"] += 1
                self_source.append(source)
                return super().probe_metadata(source)
        self_source = []
        with patch.dict(os.environ, {}, clear=True):
            cold = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: Counting())
            self.assertEqual(cold.available()["state"], "metadata_verified")
            self.assertEqual(cold.available()["state"], "metadata_verified")
            self.assertEqual(calls, {"probe": 1, "discover": 1})
            self.assertEqual(self_source, ["fixture-source-native"])
            class Rejected(RoutingMetadata):
                def probe_metadata(self, source):
                    raise CodexPipeError("Synthetic source-field compatibility rejection")
            cold = NativeDispatcher(self.runtime.bridge, adapter_factory=lambda **kwargs: Rejected())
            self.assertEqual(cold.available()["state"], "pending_capability")
            with self.assertRaises(BridgeError):
                cold.reserve(self.goal["goal"]["id"], self.actor["id"])
        self.assertEqual(self.pipe.writes, [])

    def test_claim_and_acceptance_do_not_claim_model_work_until_public_progress(self):
        root = self.goal["goal"]["id"]
        claim = self.reserve()
        claimed = self.runtime.state()["dispatches"][root]
        self.assertEqual(claimed["status"], "awaiting_native_reply")
        self.assertEqual(claimed["outcomes"][0]["transport_state"], "claimed")
        self.dispatcher.deliver(claim["dispatch_id"])
        accepted = self.runtime.state()["dispatches"][root]
        self.assertEqual(accepted["status"], "awaiting_native_reply")
        self.assertEqual(accepted["outcomes"][0]["transport_state"], "native_accepted")
        self.assertFalse(accepted["delivery_verified"])
        self.bus.store.send(self.actor["id"], "user", "I have started the requested task", "progress")
        self.assertEqual(self.runtime.state()["dispatches"][root]["status"], "awaiting_native_reply")
        self.bus.store.send(self.actor["id"], "user", "Progress for this exact input", "progress", in_reply_to=claim["event_id"])
        self.assertEqual(self.runtime.state()["dispatches"][root]["status"], "working")


if __name__ == "__main__":
    unittest.main()
