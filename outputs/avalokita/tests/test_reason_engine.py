from __future__ import annotations

from dataclasses import replace
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.reason_engine import NativeBinding, ReasonEngineAdapter, ReasonEngineError


# A separate, unprivileged process exercises real stdio/exit/crash boundaries.
# It never loads a native agent or uses the production Rust executable.
FIXTURE = r'''
import json, os, sys, time
mode, trace = sys.argv[1:]
def note(value):
    with open(trace, 'a', encoding='utf-8') as stream:
        stream.write(value + '\n')
def emit(value):
    print(json.dumps(value), flush=True)
request = json.loads(sys.stdin.readline())
note('started')
base = dict(contract='avalokita.reason/1', dispatch_id=request['dispatch_id'], root_id=request['root_id'])
if mode == 'before_ready_crash':
    sys.exit(1)
ready = dict(base, type='ready', target_actor_id=request['target_actor_id'],
             native_id=request['binding']['native_id'], request_sha256='a'*64)
if mode == 'wrong_target':
    ready['native_id'] = 'a-different-conversation'
emit(ready)
commit = sys.stdin.readline()
if not commit:
    sys.exit(0)
note('commit_received')
if mode == 'postcommit_crash':
    os._exit(1)
if mode == 'postcommit_timeout':
    time.sleep(5)
receipt = dict(base, type='receipt', state='transport_accepted', native_input_attempted=True,
               transport_accepted=True, transport='codex_queue_accepted', native_accepted=False,
               native_result_verified=False, automatic_retry=False, fallback_attempted=False)
if mode == 'dishonest_completion':
    receipt['native_result_verified'] = True
if mode == 'wrong_receipt':
    receipt['dispatch_id'] = 'a-different-dispatch'
if mode == 'unknown':
    receipt.update(state='delivery_unknown', transport_accepted=False, reason_code='native_timeout')
emit(receipt)
'''

# Build only when explicitly requested by the build/root lane. This helper has
# no agent, login, network or model code; its one output is in the test cwd.
SMOKE_C_SOURCE = r'''
#include <windows.h>
#include <wchar.h>
int wmain(int argc, wchar_t **argv) {
    if (argc != 6 || wcscmp(argv[1], L"queue") || wcscmp(argv[2], L"--thread") ||
        wcscmp(argv[3], L"synthetic-native-thread") || wcscmp(argv[4], L"--message")) return 20;
    if (!wcscmp(argv[5], L"avalokita.synthetic.force_nonzero")) return 17;
    if (wcscmp(argv[5], L"avalokita.synthetic.queue-proof")) return 21;
    HANDLE proof = CreateFileW(L"fake-queue-proof.txt", GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
    if (proof == INVALID_HANDLE_VALUE) return 22;
    const char body[] = "synthetic queue argv verified; no native agent or model";
    DWORD count = 0;
    BOOL ok = WriteFile(proof, body, sizeof(body)-1, &count, NULL);
    CloseHandle(proof);
    return ok && count == sizeof(body)-1 ? 0 : 23;
}
'''


def real_engine_smoke(engine_path: Path, engine_sha256: str, fake_cli_path: Path, fake_cli_sha256: str) -> dict:
    """Opt-in real Rust interoperability; never invoked by unittest discovery.

    Root/build supplies the freshly compiled, pinned fake CLI from SMOKE_C_SOURCE.
    Only a new Temp workspace, synthetic metadata and synthetic command are used.
    """
    if hashlib.sha256(fake_cli_path.read_bytes()).hexdigest() != fake_cli_sha256:
        raise AssertionError("Synthetic CLI pin mismatch")
    with tempfile.TemporaryDirectory(prefix="avalokita-reason-interop-") as directory:
        workspace = Path(directory)
        rollout = workspace / "synthetic-rollout.jsonl"
        native_id = "synthetic-native-thread"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": native_id, "cwd": str(workspace)}}) + "\n", encoding="utf-8")
        binding = NativeBinding(provider="codex", native_id=native_id, workspace=str(workspace),
                                rollout_path=str(rollout), executable=str(fake_cli_path), executable_sha256=fake_cli_sha256)
        ledger = workspace / "private.sqlite3"
        deadline = time.time() + 120
        starts = []
        gates = []
        record = {}

        def factory(args, **kwargs):
            starts.append(tuple(args))
            return subprocess.Popen(args, **kwargs)

        def gate(supplied):
            gates.append(dict(supplied))
            return dict(supplied) == record

        def authority(prompt, dispatch="synthetic-dispatch", root="synthetic-human-root"):
            return dict(source_actor_id="synthetic-claude", target_actor_id="synthetic-codex", root_id=root,
                        dispatch_id=dispatch, binding_sha256=binding.sha256, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                        deadline=deadline, goal_id="synthetic-goal", input_id=dispatch, parent_job_id="synthetic-parent-job",
                        parent_event_id="synthetic-parent-event", human_authorized=True, root_active=True,
                        recipient_registered=True, workspace_paused=False, recipient_paused=False,
                        budget_remaining=1, claim_state="claimed")

        def send(prompt, dispatch="synthetic-dispatch", root="synthetic-human-root", gate_fn=gate):
            nonlocal record
            record = authority(prompt, dispatch, root)
            adapter = ReasonEngineAdapter(engine_path=engine_path, engine_sha256=engine_sha256, ledger_path=ledger,
                                          authorize_send=gate_fn, process_factory=factory, timeout=15)
            return adapter.send_authorized(binding, prompt, source_actor_id="synthetic-claude",
                target_actor_id="synthetic-codex", root_id=root, dispatch_id=dispatch,
                deadline=deadline, authorization_record=record)

        accepted = send("avalokita.synthetic.queue-proof")
        assert accepted["state"] == "transport_accepted", accepted
        assert accepted["transport"] == "codex_queue_accepted"
        assert accepted["native_result_verified"] is False
        proof = workspace / "fake-queue-proof.txt"
        expected_proof = b"synthetic queue argv verified; no native agent or model"
        assert proof.read_bytes() == expected_proof
        starts_after_first = len(starts)
        duplicate = send("avalokita.synthetic.queue-proof")
        assert duplicate["duplicate_suppressed"] is True and len(starts) == starts_after_first

        # A valid target can be denied after Rust ready, before the native queue.
        decisions = iter([True, False])
        denied = send("avalokita.synthetic.queue-proof", "synthetic-denied", gate_fn=lambda _: next(decisions))
        assert denied["state"] == "not_sent" and denied["native_input_attempted"] is False
        assert proof.read_bytes() == expected_proof

        # A trusted record cannot override a different ID in the selected native header.
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": "another-synthetic-thread", "cwd": str(workspace)}}) + "\n", encoding="utf-8")
        mismatch = send("avalokita.synthetic.queue-proof", "synthetic-mismatch")
        assert mismatch["state"] == "not_sent" and mismatch["native_input_attempted"] is False
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": native_id, "cwd": str(workspace)}}) + "\n", encoding="utf-8")

        unknown = send("avalokita.synthetic.force_nonzero", "synthetic-unknown")
        assert unknown["state"] == "delivery_unknown", unknown
        before_replay = len(starts)
        try:
            send("avalokita.synthetic.force_nonzero", "synthetic-new-dispatch", "synthetic-new-human-root")
        except ReasonEngineError:
            pass
        else:
            raise AssertionError("Fresh root replay was allowed")
        assert len(starts) == before_replay
        with closing(sqlite3.connect(ledger)) as db:
            engine_rows = db.execute("SELECT state FROM avalokita_reason_attempts ORDER BY dispatch_id").fetchall()
        assert sorted(row[0] for row in engine_rows) == ["delivery_unknown", "transport_accepted"]
        return dict(scope="real fork binary/Python stdio with synthetic metadata and a tiny fake CLI only",
                    checks_passed=6, engine_sha256=engine_sha256, fake_cli_sha256=fake_cli_sha256,
                    fork_process_starts=len(starts), queue_helpers_accepted=1, queue_helpers_nonzero=1,
                    native_agents_or_models_invoked=0, proof_sha256=hashlib.sha256(expected_proof).hexdigest(),
                    tested_cases=["real Rust ready/commit/ledger/queue argv", "duplicate suppression", "gate revoked after ready",
                                  "mismatched exact native metadata", "queue nonzero remains unknown", "fresh root replay blocked"],
                    native_result_verified=False)


class ReasonDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)
        self.engine = self.workspace / "pinned-engine.exe"
        self.engine.write_bytes(b"synthetic executable pin, not a real native engine")
        self.engine_hash = hashlib.sha256(self.engine.read_bytes()).hexdigest()
        self.script = self.workspace / "stdio_fixture.py"
        self.script.write_text(FIXTURE, encoding="utf-8")
        self.trace = self.workspace / "safe-trace.txt"
        self.ledger = self.workspace / "private.sqlite3"
        self.binding = NativeBinding(provider="codex", native_id="the-selected-original-thread",
                                     workspace=str(self.workspace), rollout_path=str(self.workspace / "selected.jsonl"),
                                     executable=str(self.workspace / "codex.exe"), executable_sha256="b" * 64)
        self.deadline = time.time() + 60
        self.calls = []
        self.gates = []
        self.record = self.authority(self.binding)

    def authority(self, binding):
        return dict(source_actor_id="claude-ceo", target_actor_id="codex-design", root_id="human-root",
                    dispatch_id="reserved-dispatch", binding_sha256=binding.sha256,
                    prompt_sha256=hashlib.sha256(b"review the selected design").hexdigest(), deadline=self.deadline,
                    goal_id="original-goal", input_id="original-native-input", job_id="reserved-job", parent_job_id="exact-parent-job",
                    parent_event_id="exact-parent-event", human_authorized=True, root_active=True,
                    recipient_registered=True, workspace_paused=False, recipient_paused=False,
                    budget_remaining=1, claim_state="claimed")

    def gate(self, record):
        self.gates.append(dict(record))
        return dict(record) == self.record

    def adapter(self, mode="accepted", gate=None, timeout=2):
        def process_factory(args, **kwargs):
            self.calls.append(args)
            return subprocess.Popen([sys.executable, str(self.script), mode, str(self.trace)], **kwargs)
        return ReasonEngineAdapter(engine_path=self.engine, engine_sha256=self.engine_hash,
                                   ledger_path=self.ledger, authorize_send=gate or self.gate,
                                   process_factory=process_factory, timeout=timeout)

    def send(self, adapter, *, binding=None, record=None, **overrides):
        arguments = dict(source_actor_id="claude-ceo", target_actor_id="codex-design", root_id="human-root",
                         dispatch_id="reserved-dispatch", deadline=self.deadline,
                         authorization_record=self.record if record is None else record)
        arguments.update(overrides)
        return adapter.send_authorized(binding or self.binding, "review the selected design", **arguments)

    def events(self):
        return self.trace.read_text(encoding="utf-8").splitlines() if self.trace.exists() else []

    def seed_synthetic_engine_attempt(self, receipt):
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("CREATE TABLE avalokita_reason_attempts (dispatch_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,binding_key TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL)")
            db.execute("INSERT INTO avalokita_reason_attempts VALUES (?,?,?,?,?)", (receipt["dispatch_id"],
                receipt["engine_request_sha256"], "synthetic-logical-binding", receipt["state"], json.dumps(receipt)))

    def observation(self, kind="input_processed"):
        return dict(kind=kind, actor_id="codex-design", goal_id="original-goal", input_id="original-native-input",
                    selected_response_sha256=hashlib.sha256(b"selected synthetic peer result").hexdigest())

    def test_transport_ack_does_not_claim_a_native_response_or_completion(self):
        result = self.send(self.adapter())
        self.assertEqual(result["state"], "transport_accepted")
        self.assertTrue(result["native_input_attempted"])
        self.assertTrue(result["transport_accepted"])
        self.assertFalse(result["native_accepted"])
        self.assertFalse(result["native_result_verified"])
        self.assertEqual(len(self.gates), 2)
        self.assertEqual(self.events(), ["started", "commit_received"])

    def test_changed_ready_target_never_receives_a_commit(self):
        result = self.send(self.adapter("wrong_target"))
        self.assertEqual(result["state"], "not_sent")
        self.assertFalse(result["native_input_attempted"])
        self.assertEqual(self.events(), ["started"])

    def test_pause_revoked_after_target_preparation_blocks_the_write(self):
        decisions = iter([True, False])
        result = self.send(self.adapter(gate=lambda _: next(decisions)))
        self.assertEqual(result["state"], "not_sent")
        self.assertFalse(result["native_input_attempted"])
        self.assertEqual(self.events(), ["started"])

    def test_missing_durable_control_gate_does_no_process_io(self):
        adapter = self.adapter()
        adapter.authorize_send = None
        with self.assertRaises(ReasonEngineError):
            self.send(adapter)
        self.assertEqual(self.calls, [])

    def test_other_workspace_or_native_binding_cannot_reuse_an_authorization(self):
        other = self.workspace / "other-workspace"
        other.mkdir()
        for binding in (replace(self.binding, native_id="another-thread"), replace(self.binding, workspace=str(other))):
            with self.subTest(binding=binding), self.assertRaises(ReasonEngineError):
                self.send(self.adapter(), binding=binding)
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(os.name == "nt", "Windows native command line contract")
    def test_impossible_windows_arguments_fail_before_process_start_or_commit(self):
        for index, (prompt, code) in enumerate([("x" * 40000, "native_command_line_limit"), ("x\0y", "invalid_native_argument")]):
            with self.subTest(code=code):
                self.ledger = self.workspace / ("argv-" + str(index) + ".sqlite3")
                self.record = {**self.record, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
                result = self.adapter().send_authorized(self.binding, prompt, source_actor_id="claude-ceo",
                    target_actor_id="codex-design", root_id="human-root", dispatch_id="reserved-dispatch",
                    deadline=self.deadline, authorization_record=self.record)
                self.assertEqual(result["state"], "not_sent")
                self.assertFalse(result["native_input_attempted"])
                self.assertEqual(result["reason_code"], code)
        self.assertEqual(self.calls, [])

    def test_uncertain_commit_is_durable_and_not_replayed_by_a_new_adapter(self):
        first = self.send(self.adapter("postcommit_timeout", timeout=2))
        self.assertEqual(first["state"], "delivery_unknown")
        self.assertTrue(first["native_input_attempted"])
        second = self.send(self.adapter(timeout=3))
        self.assertTrue(second["duplicate_suppressed"])
        self.assertEqual(second["state"], "delivery_unknown")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.events(), ["started", "commit_received"])
        self.assertFalse(second["automatic_retry"])
        self.assertFalse(second["fallback_attempted"])

    def test_native_timeout_receipt_is_not_classified_as_unsent(self):
        first = self.send(self.adapter("unknown"))
        self.assertEqual(first["state"], "delivery_unknown")
        second = self.send(self.adapter())
        self.assertEqual(second["state"], "delivery_unknown")
        self.assertEqual(len(self.calls), 1)

    def test_new_human_root_cannot_automatically_replay_an_unknown_native_input(self):
        self.send(self.adapter("unknown"))
        self.record = {**self.record, "root_id": "fresh-human-root", "dispatch_id": "fresh-dispatch"}
        with self.assertRaises(ReasonEngineError):
            self.send(self.adapter(), root_id="fresh-human-root", dispatch_id="fresh-dispatch")
        self.assertEqual(len(self.calls), 1)

    def test_crash_after_commit_never_falls_back(self):
        first = self.send(self.adapter("postcommit_crash"))
        self.assertEqual(first["state"], "delivery_unknown")
        self.assertTrue(first["native_input_attempted"])
        self.assertEqual(self.send(self.adapter())["state"], "delivery_unknown")
        self.assertEqual(len(self.calls), 1)

    def test_exact_native_input_observation_settles_unknown_and_survives_late_transport_receipts(self):
        adapter = self.adapter("unknown")
        stale = self.send(adapter)
        self.seed_synthetic_engine_attempt(stale)
        observed = adapter.reconcile_native_observation("reserved-dispatch", "human-root", self.binding,
            self.observation(), validate_observation=lambda proof: proof == self.observation())
        self.assertEqual(observed["state"], "native_input_verified")
        self.assertTrue(observed["native_accepted"])
        self.assertFalse(observed["native_result_verified"])
        # Simulate the real engine finishing its transport receipt after the
        # authenticated peer callback. The SQLite trigger must restore evidence.
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("UPDATE avalokita_reason_attempts SET state=?,receipt=?", ("transport_accepted", json.dumps(stale)))
            engine_state = db.execute("SELECT state,receipt FROM avalokita_reason_attempts").fetchone()
            fingerprint = db.execute("SELECT fingerprint FROM avalokita_reason_dispatch").fetchone()[0]
        self.assertEqual(engine_state[0], "native_input_verified")
        self.assertTrue(json.loads(engine_state[1])["native_accepted"])
        remembered = adapter._remember("reserved-dispatch", fingerprint, stale)
        self.assertEqual(remembered["state"], "native_input_verified")
        self.assertTrue(remembered["native_accepted"])

    def test_untrusted_or_rebound_native_observation_does_not_clear_unknown(self):
        adapter = self.adapter("unknown")
        stale = self.send(adapter)
        self.seed_synthetic_engine_attempt(stale)
        for root, binding, observation, gate in [
            ("human-root", self.binding, self.observation(), lambda _: False),
            ("another-root", self.binding, self.observation(), lambda _: True),
            ("human-root", replace(self.binding, native_id="another-thread"), self.observation(), lambda _: True),
            ("human-root", self.binding, {**self.observation(), "actor_id": "another-actor"}, lambda _: True),
        ]:
            with self.assertRaises(ReasonEngineError):
                adapter.reconcile_native_observation("reserved-dispatch", root, binding, observation, validate_observation=gate)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute("SELECT state FROM avalokita_reason_dispatch").fetchone()[0], "delivery_unknown")
            self.assertEqual(db.execute("SELECT state FROM avalokita_reason_attempts").fetchone()[0], "delivery_unknown")

    def test_completed_result_observation_cannot_be_downgraded_by_a_late_input_observation(self):
        adapter = self.adapter("unknown")
        stale = self.send(adapter)
        self.seed_synthetic_engine_attempt(stale)
        completed = adapter.reconcile_native_observation("reserved-dispatch", "human-root", self.binding,
            self.observation("result_completed"), validate_observation=lambda _: True)
        self.assertTrue(completed["native_result_verified"])
        late = adapter.reconcile_native_observation("reserved-dispatch", "human-root", self.binding,
            self.observation(), validate_observation=lambda _: True)
        self.assertEqual(late["state"], "native_result_verified")
        self.assertTrue(late["native_result_verified"])
        self.assertEqual(late["native_observation"]["kind"], "result_completed")

    def test_malformed_receipt_after_commit_stays_unknown(self):
        for mode in ("dishonest_completion", "wrong_receipt"):
            with self.subTest(mode=mode):
                self.ledger = self.workspace / (mode + ".sqlite3")
                result = self.send(self.adapter(mode))
                self.assertEqual(result["state"], "delivery_unknown")
                self.assertFalse(result["native_result_verified"])

    def test_pin_change_prevents_process_start_and_same_dispatch_is_not_retried(self):
        self.engine.write_bytes(b"changed engine")
        first = self.send(self.adapter())
        self.assertEqual(first["state"], "not_sent")
        self.assertEqual(self.calls, [])
        second = self.send(self.adapter())
        self.assertTrue(second["duplicate_suppressed"])
        self.assertEqual(self.calls, [])

    def test_dispatch_cannot_be_rebound_to_another_root_or_parent(self):
        self.send(self.adapter("postcommit_crash"))
        changed = {**self.record, "parent_job_id": "different-parent"}
        self.record = changed
        with self.assertRaises(ReasonEngineError):
            self.send(self.adapter(), record=changed)
        self.assertEqual(len(self.calls), 1)

    def test_only_safe_receipt_metadata_is_stored(self):
        self.send(self.adapter())
        with closing(sqlite3.connect(self.ledger)) as db:
            row = db.execute("SELECT receipt FROM avalokita_reason_dispatch").fetchone()[0]
        self.assertNotIn("review the selected design", row)
        self.assertNotIn(str(self.workspace), row)
        self.assertNotIn("the-selected-original-thread", row)


if __name__ == "__main__":
    if "--engine-smoke" in sys.argv:
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--engine-smoke", type=Path, required=True)
        parser.add_argument("--engine-sha256", required=True)
        parser.add_argument("--fake-cli", type=Path, required=True)
        parser.add_argument("--fake-cli-sha256", required=True)
        parser.add_argument("--receipt", type=Path, required=True)
        args = parser.parse_args()
        result = real_engine_smoke(args.engine_smoke, args.engine_sha256, args.fake_cli, args.fake_cli_sha256)
        args.receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    else:
        unittest.main()
