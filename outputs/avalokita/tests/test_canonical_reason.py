"""Canonical human dispatch with a fake Reason process; native/model calls zero."""
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from adapters.canonical_reason import CanonicalReasonTransport
from adapters.reason_engine import NativeBinding, ReasonEngineAdapter, ReasonEngineError
from bridge import BridgeError
from daemon import BusHTTPServer
from native_dispatch import GRANTS, NativeDispatcher, TABLE, TRANSPORTS, complete
from server import Runtime


FIXTURE = r'''
import json, os, sqlite3, sys, time
mode, trace = sys.argv[1:]
request = json.loads(sys.stdin.readline())
with open(trace, 'a', encoding='utf-8') as stream:
    stream.write('started\n')
    stream.write('operation_timeout_ms=' + str(request['operation_timeout_ms']) + '\n')
base = dict(contract='avalokita.reason/1', dispatch_id=request['dispatch_id'], root_id=request['root_id'])
print(json.dumps(dict(base, type='ready', target_actor_id=request['target_actor_id'],
    native_id=request['binding']['native_id'], request_sha256='a'*64)), flush=True)
commit = sys.stdin.readline()
if not commit:
    sys.exit(0)
with open(trace, 'a', encoding='utf-8') as stream:
    stream.write('commit_received\n')
receipt = dict(base, type='receipt', state='transport_accepted', native_input_attempted=True,
    transport_accepted=True, transport='codex_queue_accepted', native_accepted=False,
    native_result_verified=False, automatic_retry=False, fallback_attempted=False)
if mode == 'unknown':
    receipt.update(state='delivery_unknown', transport_accepted=False)
with sqlite3.connect(request['ledger_path']) as db:
    db.execute('CREATE TABLE IF NOT EXISTS avalokita_reason_attempts (dispatch_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,binding_key TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL)')
    db.execute('INSERT INTO avalokita_reason_attempts VALUES (?,?,?,?,?)',
        (request['dispatch_id'], 'a'*64, 'synthetic-binding', receipt['state'], json.dumps(receipt)))
if mode == 'before_ack':
    with open(trace, 'a', encoding='utf-8') as stream:
        stream.write('engine_attempt_recorded\n')
    while not os.path.exists(trace + '.release'):
        time.sleep(.005)
print(json.dumps(receipt), flush=True)
'''

# Opt-in actual pinned Rust interoperability only; unittest discovery never
# compiles or runs this helper. It accepts a synthetic ID/prompt exclusively.
DELAYED_QUEUE_C_SOURCE = r'''
#include <windows.h>
#include <wchar.h>
#include <stdio.h>
int wmain(int argc, wchar_t **argv) {
    if (argc != 6 || wcscmp(argv[1], L"queue") || wcscmp(argv[2], L"--thread") ||
        wcscmp(argv[3], L"canonical-existing-codex") || wcscmp(argv[4], L"--message") ||
        !wcsstr(argv[5], L"Selected synthetic human work")) return 20;
    ULONGLONG started = GetTickCount64();
    Sleep(125);
    ULONGLONG elapsed = GetTickCount64() - started;
    HANDLE proof = CreateFileW(L"delayed-fake-queue-proof.json", GENERIC_WRITE, 0, NULL,
                              CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (proof == INVALID_HANDLE_VALUE) return 22;
    char body[160];
    int length = sprintf_s(body, sizeof(body), "{\"delay_ms\":%llu,\"native_agents_or_models\":0}", elapsed);
    DWORD count = 0;
    BOOL ok = length > 0 && WriteFile(proof, body, (DWORD)length, &count, NULL);
    CloseHandle(proof);
    return ok && count == (DWORD)length ? 0 : 23;
}
'''


class CanonicalReasonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="canonical-reason-fixture-")
        self.workspace = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop("CODEX_THREAD_ID", None)
        self.bus = BusHTTPServer(self.workspace, enable_native=False, discovery=[])
        (self.bus.store.private / "runtime.json").write_text(json.dumps(self.bus.runtime()), encoding="utf-8")
        self.thread = threading.Thread(target=self.bus.serve_forever, daemon=True)
        self.thread.start()
        self.actor, self.token = self.bus.store.register("codex", "Existing Codex", [], "external_attached",
            role="reviewer", native_session_id="canonical-existing-codex")
        self.runtime = Runtime(self.workspace, discovery=False)
        self.unregister_completion = self.bus.store.register_native_completion_handler(self.runtime.complete_native_result)
        self.goal = self.runtime.store.new_goal("Selected synthetic human work", idempotency_key="canonical-reason-human")
        self.receipt = self.runtime.bridge.dispatch(self.goal, self.goal["prompt"], native_ipc_ids=[self.actor["id"]])
        self.engine = self.workspace / "synthetic-reason.exe"
        self.engine.write_bytes(b"synthetic pinned Reason executable; never run")
        self.executable = self.workspace / "synthetic-codex.exe"
        self.executable.write_bytes(b"synthetic Codex executable; never run")
        self.rollout = self.workspace / "synthetic-session.jsonl"
        self.rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": "canonical-existing-codex", "cwd": str(self.workspace)}}) + "\n", encoding="utf-8")
        self.binding = NativeBinding(provider="codex", native_id="canonical-existing-codex", workspace=str(self.workspace),
            rollout_path=str(self.rollout), executable=str(self.executable), executable_sha256=hashlib.sha256(self.executable.read_bytes()).hexdigest())
        self.script = self.workspace / "synthetic-reason.py"
        self.script.write_text(FIXTURE, encoding="utf-8")
        self.trace = self.workspace / "synthetic-reason-trace.txt"
        self.starts = []
        self.mode = "accepted"
        self.on_process = None
        self.reason = CanonicalReasonTransport(engine_path=self.engine, engine_sha256=hashlib.sha256(self.engine.read_bytes()).hexdigest(),
            bindings={self.actor["id"]: self.binding}, process_factory=self.process_factory)
        self.dispatcher = NativeDispatcher(self.runtime.bridge, harmony_store=self.runtime.store, reason_transport=self.reason)

    def tearDown(self):
        self.unregister_completion()
        self.bus.shutdown()
        self.bus.close()
        self.environment.stop()
        self.directory.cleanup()

    def process_factory(self, args, **kwargs):
        self.starts.append(args)
        process = subprocess.Popen([sys.executable, "-B", str(self.script), self.mode, str(self.trace)], **kwargs)
        if self.on_process:
            self.on_process()
        return process

    def reserve(self):
        return self.dispatcher.reserve(self.goal["id"], self.actor["id"])

    def send(self):
        claim = self.reserve()
        return claim, self.dispatcher.deliver(claim["dispatch_id"])

    def state(self, dispatch_id):
        with self.bus.store.connection() as db:
            return dict(db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone())

    def test_existing_canonical_human_root_without_native_source_and_no_second_authority_ledger(self):
        self.assertIsNone(self.dispatcher.source)
        self.assertEqual(self.dispatcher.available_for(self.actor["id"], self.binding.native_id)["transport"], "reason")
        self.assertEqual(self.starts, [])
        records = []
        prompts = []
        original = ReasonEngineAdapter.send_authorized
        def capture(adapter, *args, **kwargs):
            records.append(kwargs["authorization_record"])
            prompts.append(args[1])
            self.assertEqual(adapter.timeout, 60)
            return original(adapter, *args, **kwargs)
        with patch.object(ReasonEngineAdapter, "send_authorized", capture):
            claim, result = self.send()
        self.assertEqual(result["state"], "transport_accepted")
        self.assertFalse(result["native_accepted"])
        self.assertFalse(result["native_result_verified"])
        self.assertEqual(result["transport_engine"], "reason")
        roster = json.loads(prompts[0].split("<registered_roster>\n", 1)[1].split("\n</registered_roster>", 1)[0])
        self.assertEqual(roster[0]["route"], "reason")
        self.assertEqual(self.state(claim["dispatch_id"])["source_id"], "user")
        record = records[0]
        self.assertEqual(record["source_actor_id"], "user")
        self.assertEqual((record["goal_id"], record["root_id"], record["job_id"], record["event_id"]),
            (self.goal["id"], self.receipt["root_id"], claim["job_id"], self.receipt["event_id"]))
        self.assertNotIn("source_thread_id", record)
        self.assertNotIn("parent_job_id", record)
        self.assertNotIn("parent_event_id", record)
        self.assertEqual(record["bindings"], self.receipt["bindings"])
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT used FROM roots WHERE id=?", (claim["root_id"],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM roots").fetchone()[0], 1)
        with self.runtime.store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM goals").fetchone()[0], 1)
        ledger = self.runtime.bridge.receipts.parent / "reason-transport.sqlite3"
        with closing(sqlite3.connect(ledger)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"avalokita_reason_dispatch", "avalokita_reason_attempts"})

    def test_reason_request_preserves_rust_queue_wait_budget_without_extending_root_deadline(self):
        claim, result = self.send()
        self.assertEqual(result["state"], "transport_accepted")
        self.assertIn("operation_timeout_ms=60000", self.trace.read_text(encoding="utf-8"))
        with self.bus.store.connection() as db:
            original = db.execute("SELECT deadline FROM roots WHERE id=?", (claim["root_id"],)).fetchone()[0]
            fact = json.loads(db.execute(f"SELECT fact FROM {GRANTS} WHERE dispatch_id=?", (claim["dispatch_id"],)).fetchone()[0])
        self.assertEqual(fact["deadline"], original)
        self.assertEqual(self.state(claim["dispatch_id"])["deadline"], original)
        self.assertFalse(result["native_accepted"])
        self.assertFalse(result["native_result_verified"])

    def test_exact_target_readiness_does_not_grant_another_actor_or_native_id(self):
        self.assertEqual(self.dispatcher.available_for(self.actor["id"], "another-native")["state"], "pending_capability")
        self.assertEqual(self.dispatcher.available_for("unconfigured-actor", self.binding.native_id)["state"], "pending_capability")
        self.assertEqual(self.starts, [])

    def test_last_reserved_turn_is_valid_when_unreserved_bus_and_harmony_budget_is_zero(self):
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET budget=1")
        claim = self.reserve()
        with self.runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET budget=1,used=1 WHERE id=?", (self.goal["id"],))
        self.assertEqual(self.dispatcher.deliver(claim["dispatch_id"])["state"], "transport_accepted")
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT used,budget FROM roots").fetchone()[:], (1, 1))

    def test_exact_callback_alone_completes_and_settles_transport_observation(self):
        claim, receipt = self.send()
        self.assertEqual(self.bus.store.jobs()[0]["state"], "awaiting_native_reply")
        self.assertEqual(self.dispatcher.reconcile_completed(), [])
        with self.assertRaises(BridgeError):
            complete(self.workspace, actor=self.actor["id"], native_session_id="wrong-native",
                job_id=claim["job_id"], event_id=claim["event_id"], text="rejected")
        result = complete(self.workspace, actor=self.actor["id"], native_session_id=self.binding.native_id,
            job_id=claim["job_id"], event_id=claim["event_id"], text="Selected synthetic final")
        self.assertTrue(result["completed"])
        self.assertEqual(self.state(claim["dispatch_id"])["state"], "transport_accepted")
        ledger = self.runtime.bridge.receipts.parent / "reason-transport.sqlite3"
        with closing(sqlite3.connect(ledger)) as db:
            observed = json.loads(db.execute("SELECT receipt FROM avalokita_reason_dispatch").fetchone()[0])
            engine_state = db.execute("SELECT state FROM avalokita_reason_attempts").fetchone()[0]
        self.assertEqual(observed["state"], "native_result_verified")
        self.assertEqual(engine_state, "native_result_verified")
        self.assertEqual(observed["native_observation"]["selected_response_sha256"], hashlib.sha256(b"Selected synthetic final").hexdigest())
        self.assertFalse(receipt["native_accepted"])
        self.assertEqual(len(self.starts), 1)

    def test_passive_or_wrong_input_result_never_settles_or_completes(self):
        claim, _ = self.send()
        self.bus.store.native_result(self.actor["id"], self.binding.native_id, "Passive synthetic result")
        self.assertEqual(self.dispatcher.reconcile_completed(), [])
        with self.assertRaises(BridgeError):
            complete(self.workspace, actor=self.actor["id"], native_session_id=self.binding.native_id,
                job_id=claim["job_id"], event_id="wrong-event", text="Not the exact input")
        self.assertEqual(self.bus.store.jobs()[0]["state"], "awaiting_native_reply")
        self.assertEqual(len(self.starts), 1)

    def test_unknown_and_restart_never_retry_switch_or_fall_back(self):
        self.mode = "unknown"
        claim, first = self.send()
        self.assertEqual(first["state"], "delivery_unknown")
        restarted = NativeDispatcher(self.runtime.bridge, reason_transport=None)
        self.assertTrue(restarted.deliver(claim["dispatch_id"])["duplicate_suppressed"])
        self.reason.engine_sha256 = "b" * 64
        self.assertTrue(self.dispatcher.deliver(claim["dispatch_id"])["duplicate_suppressed"])
        self.assertEqual(len(self.starts), 1)
        self.assertFalse(first["fallback_attempted"])

    def test_frozen_selection_cannot_change_before_the_first_attempt(self):
        claim = self.reserve()
        self.reason.engine_sha256 = "b" * 64
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(self.state(claim["dispatch_id"])["state"], "claimed")
        with self.bus.store.connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(f"UPDATE {TRANSPORTS} SET selection='{{}}'")
        self.assertEqual(self.starts, [])

    def test_rebinding_after_engine_preparation_prevents_commit_and_never_retries(self):
        self.on_process = lambda: self.reason.bindings.update({self.actor["id"]: replace(self.binding, native_id="rebound-native")})
        claim, result = self.send()
        self.assertEqual(result["state"], "not_sent")
        self.assertFalse(result["native_input_attempted"])
        self.assertNotIn("commit_received", self.trace.read_text(encoding="utf-8"))
        self.assertTrue(self.dispatcher.deliver(claim["dispatch_id"])["duplicate_suppressed"])
        self.assertEqual(len(self.starts), 1)

    def test_changed_engine_pin_fails_before_process_start_and_has_no_retry(self):
        claim = self.reserve()
        self.engine.write_bytes(b"changed synthetic engine")
        receipt = self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(receipt["state"], "not_sent")
        self.assertFalse(receipt["native_input_attempted"])
        self.assertTrue(self.dispatcher.deliver(claim["dispatch_id"])["duplicate_suppressed"])
        self.assertEqual(self.starts, [])

    def test_slow_reason_ledger_releases_control_writers_and_pause_prevents_ready_grant(self):
        claim = self.reserve()
        entered, paused = threading.Event(), threading.Event()
        original = ReasonEngineAdapter._ledger
        calls, results, failures = [], [], []
        @contextmanager
        def slow_ledger(adapter):
            calls.append(True)
            if len(calls) == 1:
                entered.set()
                time.sleep(2.5)
            with original(adapter) as db:
                yield db
        def send():
            try:
                results.append(self.dispatcher.deliver(claim["dispatch_id"]))
            except Exception as error:
                failures.append(error)
        with patch.object(ReasonEngineAdapter, "_ledger", slow_ledger):
            delivery = threading.Thread(target=send)
            delivery.start()
            self.assertTrue(entered.wait(5))
            control = threading.Thread(target=lambda: (self.runtime.control("pause"), paused.set()))
            control.start()
            timely_pause = paused.wait(1)
            with self.bus.store.connection() as db:
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {GRANTS}").fetchone()[0], 0)
            delivery.join(8)
            control.join(5)
        self.assertTrue(timely_pause, "Slow Reason preparation held a control writer")
        self.assertFalse(delivery.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results[0]["state"], "not_sent")
        self.assertFalse(results[0]["commit_granted"])
        self.assertNotIn("commit_received", self.trace.read_text(encoding="utf-8"))
        self.runtime.control("resume")
        self.assertTrue(self.dispatcher.deliver(claim["dispatch_id"])["duplicate_suppressed"])

    def test_ready_grant_is_durable_immutable_and_pause_does_not_block_slow_receipt_cleanup(self):
        claim = self.reserve()
        entered, release, paused = threading.Event(), threading.Event(), threading.Event()
        original = ReasonEngineAdapter._remember
        calls, results = [], []
        def slow_remember(adapter, *args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                entered.set()
                self.assertTrue(release.wait(8))
            return original(adapter, *args, **kwargs)
        with patch.object(ReasonEngineAdapter, "_remember", slow_remember):
            delivery = threading.Thread(target=lambda: results.append(self.dispatcher.deliver(claim["dispatch_id"])))
            delivery.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.state(claim["dispatch_id"])["state"], "commit_granted")
                with self.bus.store.connection() as db:
                    self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {GRANTS}").fetchone()[0], 1)
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.execute(f"UPDATE {GRANTS} SET fact='{{}}'")
                control = threading.Thread(target=lambda: (self.runtime.control("pause"), paused.set()))
                control.start()
                timely_pause = paused.wait(1)
                control.join(5)
            finally:
                release.set()
                delivery.join(8)
        self.assertTrue(timely_pause, "Receipt cleanup held a control writer")
        self.assertFalse(delivery.is_alive())
        self.assertTrue(results[0]["commit_granted"])
        self.assertFalse(results[0]["native_accepted"])
        with self.runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET deadline=0,used=budget")
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0")
        done = complete(self.workspace, actor=self.actor["id"], native_session_id=self.binding.native_id,
            job_id=claim["job_id"], event_id=claim["event_id"], text="Exact selected final after grant, pause and expiry")
        self.assertTrue(done["completed"])
        self.assertEqual(self.dispatcher.reconcile_completed()[0]["state"], "native_result_verified")

    def test_exact_callback_before_canonical_ack_is_idempotent_and_conflicting_final_is_rejected(self):
        self.mode = "before_ack"
        claim = self.reserve()
        results = []
        delivery = threading.Thread(target=lambda: results.append(self.dispatcher.deliver(claim["dispatch_id"])))
        delivery.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and (not self.trace.exists() or "engine_attempt_recorded" not in self.trace.read_text(encoding="utf-8")):
                time.sleep(.005)
            self.assertIn("engine_attempt_recorded", self.trace.read_text(encoding="utf-8"))
            self.assertEqual(self.state(claim["dispatch_id"])["state"], "commit_granted")
            arguments = dict(actor=self.actor["id"], native_session_id=self.binding.native_id,
                job_id=claim["job_id"], event_id=claim["event_id"], text="Exact selected result before canonical ACK")
            self.assertTrue(complete(self.workspace, **arguments)["completed"])
            self.assertTrue(complete(self.workspace, **arguments)["duplicate_suppressed"])
            with self.assertRaises(BridgeError):
                complete(self.workspace, **{**arguments, "text": "A different conflicting final"})
        finally:
            Path(str(self.trace) + ".release").write_text("release synthetic fixture", encoding="utf-8")
            delivery.join(8)
        self.assertFalse(delivery.is_alive())
        self.assertTrue(results[0]["native_result_verified"])
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='result' AND sender=?", (self.actor["id"],)).fetchone()[0], 1)

    def test_late_result_requires_immutable_grant_not_only_a_transport_state(self):
        claim, _ = self.send()
        with self.bus.store.connection() as db:
            db.execute(f"DROP TRIGGER {GRANTS}_delete")
            db.execute(f"DELETE FROM {GRANTS}")
            db.execute(f"UPDATE {TABLE} SET state='commit_granted'")
        self.runtime.control("pause")
        with self.assertRaisesRegex(BridgeError, "immutable Reason commit grant"):
            complete(self.workspace, actor=self.actor["id"], native_session_id=self.binding.native_id,
                job_id=claim["job_id"], event_id=claim["event_id"], text="State strings are not native grants")
        self.assertEqual(self.bus.store.jobs()[0]["state"], "awaiting_native_reply")

    def test_late_result_rejects_rebound_actor_changed_human_input_and_oversized_body(self):
        claim, _ = self.send()
        arguments = dict(actor=self.actor["id"], native_session_id=self.binding.native_id,
            job_id=claim["job_id"], event_id=claim["event_id"], text="Selected final")
        self.runtime.control("pause")
        with self.assertRaises(BridgeError):
            complete(self.workspace, **{**arguments, "text": "x" * 65537})
        self.bus.store.update_agent(self.actor["id"], native_session_id="a-rebound-native-session")
        with self.assertRaises(BridgeError):
            complete(self.workspace, **arguments)
        self.bus.store.update_agent(self.actor["id"], native_session_id=self.binding.native_id)
        with self.bus.store.connection() as db:
            db.execute("UPDATE events SET body='changed human authority' WHERE id=?", (claim["event_id"],))
        with self.assertRaises(BridgeError):
            complete(self.workspace, **arguments)
        self.assertEqual(self.bus.store.jobs()[0]["state"], "awaiting_native_reply")

    def test_exact_unknown_callback_settles_without_resending_native_input(self):
        self.mode = "unknown"
        claim, receipt = self.send()
        self.assertEqual(receipt["state"], "delivery_unknown")
        result = complete(self.workspace, actor=self.actor["id"], native_session_id=self.binding.native_id,
            job_id=claim["job_id"], event_id=claim["event_id"], text="Exact synthetic result after unknown")
        self.assertTrue(result["completed"])
        observations = self.dispatcher.reconcile_completed()
        self.assertEqual(observations[0]["state"], "native_result_verified")
        self.assertEqual(self.state(claim["dispatch_id"])["state"], "delivery_unknown")
        self.assertEqual(len(self.starts), 1)

    def test_changed_human_input_or_controls_block_before_process_start(self):
        claim = self.reserve()
        self.runtime.store.set_goal_state(self.goal["id"], "paused")
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.runtime.store.set_goal_state(self.goal["id"], "active")
        with self.bus.store.connection() as db:
            db.execute("UPDATE events SET body='changed synthetic request' WHERE id=?", (claim["event_id"],))
        with self.assertRaises(BridgeError):
            self.dispatcher.deliver(claim["dispatch_id"])
        self.assertEqual(self.starts, [])

    def test_completed_observation_remains_valid_after_pause_and_expiry(self):
        claim, _ = self.send()
        self.bus.store.native_result(self.actor["id"], self.binding.native_id, "Exact synthetic callback", claim["job_id"], claim["event_id"])
        self.runtime.control("pause")
        with self.runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET deadline=0,used=budget")
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0")
        observations = self.dispatcher.reconcile_completed()
        self.assertEqual(observations[0]["state"], "native_result_verified")
        self.assertEqual(len(self.starts), 1)

    def test_deleted_new_selection_denies_input_without_reason_fallback(self):
        # Alpha compatibility revision: this is a newly reserved dispatch with
        # its immutable selection deliberately corrupted, not legacy evidence.
        calls = []
        class IPC:
            def __init__(self, **kwargs):
                self.gate = kwargs["authorize_send"]
            def send_authorized(self, native_id, prompt, **kwargs):
                calls.append(native_id)
                assert self.gate(kwargs["authorization_record"])
                return dict(state="native_accepted", native_accepted=True, native_result_verified=False,
                    automatic_retry=False, fallback_attempted=False)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "old-observed-native-source"}):
            old = NativeDispatcher(self.runtime.bridge, adapter_factory=IPC)
            claim = old.reserve(self.goal["id"], self.actor["id"])
            with self.bus.store.connection() as db:
                db.execute(f"DROP TRIGGER {TRANSPORTS}_delete")
                db.execute(f"DELETE FROM {TRANSPORTS}")
            restarted = NativeDispatcher(self.runtime.bridge, adapter_factory=IPC, reason_transport=self.reason)
            with self.assertRaises(BridgeError):
                restarted.deliver(claim["dispatch_id"])
        self.assertEqual(calls, [])
        self.assertEqual(self.starts, [])
        self.assertEqual(self.state(claim["dispatch_id"])["state"], "claimed")
        self.assertIsNone(self.bus.store.jobs()[0]["native_input_reserved_at"])

    def test_reason_peer_sender_requires_original_native_host_proof_and_does_not_debit(self):
        other, _ = self.bus.store.register("codex", "Peer", [], "external_attached", native_session_id="another-existing-codex")
        goal = self.runtime.store.new_goal("Synthetic peer scope", idempotency_key="canonical-reason-peer")
        receipt = self.runtime.bridge.dispatch(goal, goal["prompt"], recipient_ids=[self.actor["id"]],
            collaborator_ids=[self.actor["id"], other["id"]], native_ipc_ids=[self.actor["id"], other["id"]])
        claim = self.dispatcher.reserve(goal["id"], self.actor["id"])
        self.dispatcher.deliver(claim["dispatch_id"])
        with self.assertRaisesRegex(BridgeError, "original native sender host"):
            self.dispatcher.peer_send(self.token, goal["id"], other["id"], "Selected synthetic peer request", "message", "peer-once",
                parent_job_id=claim["job_id"], parent_event_id=claim["event_id"])
        with self.bus.store.connection() as db:
            self.assertEqual(db.execute("SELECT used FROM roots WHERE id=?", (receipt["root_id"],)).fetchone()[0], 1)
        self.assertEqual(len(self.starts), 1)


def real_delayed_engine_smoke(engine_path: Path, engine_sha256: str,
                              fake_cli_path: Path, fake_cli_sha256: str) -> dict:
    """One explicit actual Rust + delayed fake queue; never a native provider."""
    if (hashlib.sha256(engine_path.read_bytes()).hexdigest() != engine_sha256
            or hashlib.sha256(fake_cli_path.read_bytes()).hexdigest() != fake_cli_sha256):
        raise AssertionError("Pinned synthetic interoperability executable changed")
    case = CanonicalReasonTests()
    case.setUp()
    try:
        case.reason.engine_path = engine_path
        case.reason.engine_sha256 = engine_sha256
        case.binding = replace(case.binding, executable=str(fake_cli_path), executable_sha256=fake_cli_sha256)
        case.reason.bindings = {case.actor["id"]: case.binding}
        starts = []
        def factory(args, **kwargs):
            starts.append(args)
            return subprocess.Popen(args, **kwargs)
        case.reason.process_factory = factory
        claim, receipt = case.send()
        assert receipt["state"] == "transport_accepted", receipt
        assert receipt["native_accepted"] is False and receipt["native_result_verified"] is False
        assert receipt["commit_granted"] is True and len(starts) == 1
        proof_path = case.workspace / "delayed-fake-queue-proof.json"
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        assert proof["delay_ms"] >= 100 and proof["native_agents_or_models"] == 0, proof
        with case.bus.store.connection() as db:
            budget = db.execute("SELECT used,budget FROM roots WHERE id=?", (claim["root_id"],)).fetchone()
            assert budget[0] == 1
        return {"scope": "Canonical controller -> frozen Python adapter -> actual pinned Rust -> delayed synthetic queue CLI",
                "checks_passed": 1, "engine_sha256": engine_sha256, "fake_cli_sha256": fake_cli_sha256,
                "fake_cli_delay_ms": proof["delay_ms"], "reason_operation_timeout_ms": 60000,
                "engine_starts": len(starts), "fake_queue_helpers_accepted": 1, "reserved_turns_used": budget[0],
                "state": receipt["state"], "native_accepted": False, "native_result_verified": False,
                "native_agents_or_models_invoked": 0, "automatic_retry": False, "fallback_attempted": False,
                "proof_sha256": hashlib.sha256(proof_path.read_bytes()).hexdigest()}
    finally:
        case.tearDown()


if __name__ == "__main__":
    unittest.main()
