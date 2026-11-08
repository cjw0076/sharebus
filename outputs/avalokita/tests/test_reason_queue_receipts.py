from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters.reason_engine import NativeBinding, ReasonEngineAdapter, ReasonEngineError


# A local fake Reason process writes synthetic attempt/queue facts. It cannot
# start Codex, contact a provider, or consume/start/delete a native queue item.
FIXTURE = r'''
import hashlib, json, os, sqlite3, sys, time
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
options = json.loads(sys.argv[1])
request = json.loads(sys.stdin.readline())
def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
base = dict(contract='avalokita.reason/1', dispatch_id=request['dispatch_id'], root_id=request['root_id'])
request_hash = digest('synthetic-ready:' + json.dumps(request, ensure_ascii=False, sort_keys=True))
binding_key = digest('synthetic-exact-binding')
emit(dict(base, type='ready', target_actor_id=request['target_actor_id'],
          native_id=request['binding']['native_id'], request_sha256=request_hash))
commit = json.loads(sys.stdin.readline())
assert commit['request_sha256'] == request_hash and commit['authorize'] is True
attempt = dict(base, type='receipt', state='delivery_unknown', native_input_attempted=True,
               transport_accepted=False, native_accepted=False, native_result_verified=False,
               automatic_retry=False, fallback_attempted=False)
db = sqlite3.connect(request['ledger_path'], timeout=3)
with db:
    db.execute('CREATE TABLE avalokita_reason_attempts (dispatch_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,binding_key TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL)')
    db.execute('INSERT INTO avalokita_reason_attempts VALUES (?,?,?,?,?)',
               (request['dispatch_id'], options.get('attempt_fingerprint', request_hash),
                binding_key, attempt['state'], json.dumps(attempt)))
if options.get('wait_for_observation'):
    limit = time.monotonic() + 5
    while db.execute('SELECT state FROM avalokita_reason_attempts').fetchone()[0] != 'native_result_verified':
        if time.monotonic() > limit:
            raise RuntimeError('Synthetic observation did not arrive')
        time.sleep(0.01)
queue = dict(contract='avalokita.codex-queue/1', queued_submission_id='synthetic-queue:opaque/id',
             native_id=request['binding']['native_id'], selected_input_sha256=digest(request['prompt']),
             engine_request_sha256=request_hash, authorization_sha256=request['authorization_sha256'],
             deadline_unix_ms=request['deadline_unix_ms'], executable_sha256=request['binding']['executable_sha256'],
             stdout_sha256=digest('synthetic-queue:opaque/id\n'))
facts = dict(queue, **options.get('facts_patch', {}))
for key in options.get('facts_remove', []):
    facts.pop(key)
transport = 'codex_queue_accepted' if request['binding']['provider'] == 'codex' else 'claude_socket_written'
final = dict(attempt, state='transport_accepted', transport_accepted=True, transport=transport)
if not options.get('legacy'):
    if not options.get('without_table'):
        with db:
            db.execute('CREATE TABLE avalokita_reason_queue_receipts (dispatch_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,binding_key TEXT NOT NULL,native_id TEXT NOT NULL,queued_submission_id TEXT NOT NULL,selected_input_sha256 TEXT NOT NULL,authorization_sha256 TEXT NOT NULL,deadline_unix_ms INTEGER NOT NULL,receipt TEXT NOT NULL)')
            if not options.get('without_row'):
                columns = dict(queue, **options.get('column_patch', {}))
                db.execute('INSERT INTO avalokita_reason_queue_receipts VALUES (?,?,?,?,?,?,?,?,?)',
                           (request['dispatch_id'], options.get('queue_fingerprint', request_hash),
                            options.get('queue_binding_key', binding_key), columns['native_id'],
                            columns['queued_submission_id'], columns['selected_input_sha256'],
                            columns['authorization_sha256'], columns['deadline_unix_ms'],
                            options.get('facts_text', json.dumps(facts, ensure_ascii=False))))
            db.executescript("""CREATE TRIGGER synthetic_queue_no_update BEFORE UPDATE ON avalokita_reason_queue_receipts
                BEGIN SELECT RAISE(ABORT,'immutable synthetic queue fact'); END;
                CREATE TRIGGER synthetic_queue_no_delete BEFORE DELETE ON avalokita_reason_queue_receipts
                BEGIN SELECT RAISE(ABORT,'immutable synthetic queue fact'); END;""")
    if not options.get('omit_wire_queue'):
        final['queue_receipt'] = dict(facts, **options.get('wire_patch', {}))
        for key in options.get('wire_remove', []):
            final['queue_receipt'].pop(key)
with db:
    db.execute('UPDATE avalokita_reason_attempts SET state=?,receipt=?', (final['state'], json.dumps(final)))
db.close()
if options.get('crash_after_queue'):
    os._exit(2)
emit(final)
'''


class ReasonQueueReceiptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="avalokita-queue-receipt-fixture-")
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.engine = self.workspace / "synthetic-engine.exe"
        self.engine.write_bytes(b"synthetic engine pin; never a native executable")
        self.script = self.workspace / "synthetic_reason.py"
        self.script.write_text(FIXTURE, encoding="utf-8")
        self.ledger = self.workspace / "transport.sqlite3"
        self.binding = NativeBinding(provider="codex", native_id="synthetic-original-thread",
                                     workspace=str(self.workspace), executable=str(self.workspace / "synthetic-codex.exe"),
                                     executable_sha256="b" * 64, rollout_path=str(self.workspace / "synthetic-rollout.jsonl"))
        self.deadline = time.time() + 60
        self.prompt = "Synthetic selected envelope\n작업: 전체 제출 본문을 확인\nfinal line"
        self.starts = []

    def authority(self, binding, *, root="human-root", dispatch="dispatch-1"):
        return dict(source_actor_id="synthetic-source", target_actor_id="synthetic-recipient", root_id=root,
                    dispatch_id=dispatch, binding_sha256=binding.sha256,
                    prompt_sha256=hashlib.sha256(self.prompt.encode("utf-8")).hexdigest(), deadline=self.deadline,
                    goal_id="synthetic-goal", input_id="synthetic-input", parent_job_id="synthetic-parent",
                    human_authorized=True, root_active=True, recipient_registered=True, workspace_paused=False,
                    recipient_paused=False, budget_remaining=1, claim_state="claimed")

    def adapter(self, **options):
        def factory(args, **kwargs):
            self.starts.append(tuple(args))
            return subprocess.Popen([sys.executable, "-B", str(self.script), json.dumps(options)], **kwargs)
        return ReasonEngineAdapter(engine_path=self.engine, engine_sha256=hashlib.sha256(self.engine.read_bytes()).hexdigest(),
                                   ledger_path=self.ledger, authorize_send=lambda _: True, timeout=10, process_factory=factory)

    def send(self, adapter=None, *, binding=None, root="human-root", dispatch="dispatch-1"):
        binding = binding or self.binding
        return (adapter or self.adapter()).send_authorized(binding, self.prompt,
            source_actor_id="synthetic-source", target_actor_id="synthetic-recipient", root_id=root,
            dispatch_id=dispatch, deadline=self.deadline, authorization_record=self.authority(binding, root=root, dispatch=dispatch))

    def observation(self, kind="result_completed"):
        return dict(kind=kind, actor_id="synthetic-recipient", goal_id="synthetic-goal", input_id="synthetic-input",
                    selected_response_sha256=hashlib.sha256(b"synthetic selected response").hexdigest())

    def isolate_case(self, name):
        self.ledger = self.workspace / (name + ".sqlite3")

    def test_first_return_preserves_exact_queue_facts_without_claiming_native_execution(self):
        result = self.send()
        queue = result["queue_receipt"]
        self.assertEqual(result["state"], "transport_accepted")
        self.assertFalse(result["native_accepted"])
        self.assertFalse(result["native_result_verified"])
        self.assertFalse(result["automatic_retry"])
        self.assertFalse(result["fallback_attempted"])
        self.assertEqual(queue["native_id"], self.binding.native_id)
        self.assertEqual(queue["selected_input_sha256"], hashlib.sha256(self.prompt.encode("utf-8")).hexdigest())
        self.assertEqual(queue["engine_request_sha256"], result["engine_request_sha256"])
        canonical_authority = json.dumps(self.authority(self.binding), ensure_ascii=False, sort_keys=True,
                                         separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertEqual(queue["authorization_sha256"], hashlib.sha256(canonical_authority).hexdigest())
        self.assertEqual(queue["deadline_unix_ms"], int(self.deadline * 1000))
        self.assertEqual(queue["executable_sha256"], self.binding.executable_sha256)
        with closing(sqlite3.connect(self.ledger)) as db:
            dispatch = db.execute("SELECT fingerprint,receipt FROM avalokita_reason_dispatch").fetchone()
            attempt = db.execute("SELECT fingerprint FROM avalokita_reason_attempts").fetchone()[0]
            facts = db.execute("SELECT receipt FROM avalokita_reason_queue_receipts").fetchone()[0]
        self.assertNotEqual(dispatch[0], attempt)
        self.assertEqual(attempt, queue["engine_request_sha256"])
        self.assertEqual(json.loads(dispatch[1])["queue_receipt"], json.loads(facts))
        self.assertNotIn(self.prompt, dispatch[1])

    def test_duplicate_and_restart_recover_facts_without_a_new_process_or_mutating_the_fact(self):
        first = self.send()
        with closing(sqlite3.connect(self.ledger)) as db, db:
            facts_before = db.execute("SELECT * FROM avalokita_reason_queue_receipts").fetchone()
            for table in ("avalokita_reason_dispatch", "avalokita_reason_attempts"):
                stored = json.loads(db.execute("SELECT receipt FROM " + table).fetchone()[0])
                stored.pop("queue_receipt", None)
                db.execute("UPDATE " + table + " SET receipt=?", (json.dumps(stored),))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE avalokita_reason_queue_receipts SET queued_submission_id='changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM avalokita_reason_queue_receipts")
        restarted = self.send(self.adapter())
        self.assertTrue(restarted["duplicate_suppressed"])
        self.assertEqual(restarted["queue_receipt"], first["queue_receipt"])
        self.assertEqual(len(self.starts), 1)
        with closing(sqlite3.connect(self.ledger)) as db:
            self.assertEqual(db.execute("SELECT * FROM avalokita_reason_queue_receipts").fetchone(), facts_before)

    def test_authenticated_callback_can_precede_queue_capture_without_losing_either_fact(self):
        adapter = self.adapter(wait_for_observation=True)
        observed = []
        failures = []

        def observe():
            try:
                limit = time.monotonic() + 5
                while time.monotonic() < limit:
                    try:
                        with closing(sqlite3.connect(self.ledger)) as db:
                            if db.execute("SELECT count(*) FROM avalokita_reason_attempts").fetchone()[0] == 1:
                                break
                    except sqlite3.Error:
                        pass
                    time.sleep(0.01)
                observed.append(adapter.reconcile_native_observation("dispatch-1", "human-root", self.binding,
                    self.observation(), validate_observation=lambda _: True))
            except Exception as error:
                failures.append(error)

        observer = Thread(target=observe)
        observer.start()
        first = self.send(adapter)
        observer.join(timeout=6)
        self.assertFalse(observer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(observed[0]["state"], "native_result_verified")
        self.assertNotIn("queue_receipt", observed[0])
        self.assertEqual(first["state"], "native_result_verified")
        self.assertTrue(first["native_result_verified"])
        with closing(sqlite3.connect(self.ledger)) as db:
            attempt = json.loads(db.execute("SELECT receipt FROM avalokita_reason_attempts").fetchone()[0])
            fingerprint = db.execute("SELECT fingerprint FROM avalokita_reason_dispatch").fetchone()[0]
        self.assertNotIn("queue_receipt", attempt)
        stale = {**first, "state": "transport_accepted", "native_accepted": False, "native_result_verified": False}
        stale.pop("native_observation")
        stale.pop("queue_receipt")
        remembered = adapter._remember("dispatch-1", fingerprint, stale, binding=self.binding)
        self.assertEqual(remembered["state"], "native_result_verified")
        self.assertEqual(remembered["queue_receipt"], first["queue_receipt"])
        late = self.adapter().reconcile_native_observation("dispatch-1", "human-root", self.binding,
            self.observation("input_processed"), validate_observation=lambda _: True)
        self.assertEqual(late["state"], "native_result_verified")
        self.assertEqual(late["native_observation"]["kind"], "result_completed")
        self.assertEqual(late["queue_receipt"], first["queue_receipt"])
        duplicate = self.send(self.adapter())
        self.assertEqual(duplicate["queue_receipt"], first["queue_receipt"])
        self.assertEqual(duplicate["state"], "native_result_verified")
        self.assertEqual(len(self.starts), 1)

    def test_legacy_engine_without_optional_table_is_compatible_and_does_not_invent_facts(self):
        first = self.send(self.adapter(legacy=True))
        self.assertEqual(first["state"], "transport_accepted")
        self.assertNotIn("queue_receipt", first)
        self.assertNotIn("queue_receipt", self.send(self.adapter()))
        self.assertEqual(len(self.starts), 1)

    def test_restart_observation_after_expiry_needs_no_new_send_authority(self):
        first = self.send()
        restarted = self.adapter()
        restarted.authorize_send = None
        with patch("adapters.reason_engine.time.time", return_value=self.deadline + 1):
            result = restarted.reconcile_native_observation("dispatch-1", "human-root", self.binding,
                self.observation(), validate_observation=lambda _: True)
        self.assertEqual(result["state"], "native_result_verified")
        self.assertEqual(result["queue_receipt"], first["queue_receipt"])
        self.assertEqual(result["queue_receipt"]["deadline_unix_ms"], int(self.deadline * 1000))
        self.assertEqual(len(self.starts), 1)

    def test_unanchored_wire_receipt_is_unknown_and_never_replayed(self):
        for option in ("without_table", "without_row"):
            with self.subTest(option=option):
                self.isolate_case(option)
                before = len(self.starts)
                first = self.send(self.adapter(**{option: True}))
                self.assertEqual(first["state"], "delivery_unknown")
                self.assertNotIn("queue_receipt", first)
                repeated = self.send(self.adapter())
                self.assertEqual(repeated["state"], "delivery_unknown")
                self.assertNotIn("queue_receipt", repeated)
                self.assertEqual(len(self.starts), before + 1)

    def test_all_submitted_metadata_mismatches_are_rejected(self):
        patches = [dict(native_id="another-thread"), dict(selected_input_sha256="c" * 64),
                   dict(engine_request_sha256="d" * 64), dict(authorization_sha256="e" * 64),
                   dict(deadline_unix_ms=int(self.deadline * 1000) + 1), dict(executable_sha256="f" * 64)]
        for index, patch in enumerate(patches):
            with self.subTest(field=next(iter(patch))):
                self.isolate_case("metadata-" + str(index))
                result = self.send(self.adapter(facts_patch=patch))
                self.assertEqual(result["state"], "delivery_unknown")
                self.assertNotIn("queue_receipt", result)
                self.assertFalse(result["native_result_verified"])

    def test_malformed_contract_keys_ids_hashes_and_deadlines_are_rejected(self):
        options = [dict(facts_patch={"contract": "unknown/1"}), dict(facts_patch={"extra": "unsupported"}),
                   dict(facts_remove=["stdout_sha256"]), dict(facts_patch={"queued_submission_id": ""}),
                   dict(facts_patch={"queued_submission_id": "q" * 513}),
                   dict(facts_patch={"queued_submission_id": "한" * 171}),
                   dict(facts_patch={"queued_submission_id": "queue id"}),
                   dict(facts_patch={"queued_submission_id": "queue\u00a0id"}),
                   dict(facts_patch={"queued_submission_id": "queue\u007fid"}),
                   dict(facts_patch={"queued_submission_id": "queue\u0085id"}),
                   dict(facts_patch={"stdout_sha256": "A" * 64}), dict(facts_patch={"stdout_sha256": "a" * 63}),
                   dict(facts_patch={"stdout_sha256": None}), dict(facts_patch={"deadline_unix_ms": True}),
                   dict(facts_patch={"deadline_unix_ms": float(int(self.deadline * 1000))})]
        for index, option in enumerate(options):
            with self.subTest(index=index):
                self.isolate_case("malformed-" + str(index))
                result = self.send(self.adapter(**option))
                self.assertEqual(result["state"], "delivery_unknown")
                self.assertNotIn("queue_receipt", result)

    def test_opaque_id_accepts_512_utf8_bytes_without_uuid_assumptions(self):
        identifier = "한" * 170 + "ab"
        self.assertEqual(len(identifier.encode("utf-8")), 512)
        result = self.send(self.adapter(facts_patch={"queued_submission_id": identifier},
                                       column_patch={"queued_submission_id": identifier}))
        self.assertEqual(result["state"], "transport_accepted")
        self.assertEqual(result["queue_receipt"]["queued_submission_id"], identifier)

    def test_queue_row_must_match_actual_engine_attempt_and_its_own_columns(self):
        options = [dict(queue_fingerprint="c" * 64), dict(queue_binding_key="d" * 64),
                   dict(attempt_fingerprint="e" * 64), dict(column_patch={"native_id": "different-thread"}),
                   dict(column_patch={"queued_submission_id": "different-queue"}),
                   dict(column_patch={"selected_input_sha256": "f" * 64}),
                   dict(column_patch={"authorization_sha256": "a" * 64}),
                   dict(column_patch={"deadline_unix_ms": int(self.deadline * 1000) + 1})]
        for index, option in enumerate(options):
            with self.subTest(index=index):
                self.isolate_case("row-" + str(index))
                result = self.send(self.adapter(**option))
                self.assertEqual(result["state"], "delivery_unknown")
                self.assertNotIn("queue_receipt", result)

    def test_invalid_final_metadata_cannot_replace_valid_immutable_facts(self):
        result = self.send(self.adapter(wire_patch={"queued_submission_id": "forged-final-queue"}))
        self.assertEqual(result["state"], "delivery_unknown")
        self.assertEqual(result["queue_receipt"]["queued_submission_id"], "synthetic-queue:opaque/id")
        self.assertFalse(result["native_accepted"])
        self.assertFalse(result["native_result_verified"])
        self.assertEqual(self.send(self.adapter())["queue_receipt"], result["queue_receipt"])
        self.assertEqual(len(self.starts), 1)

    def test_crash_after_fact_commit_recovers_on_first_return_and_restart_without_replay(self):
        first = self.send(self.adapter(crash_after_queue=True))
        self.assertEqual(first["state"], "delivery_unknown")
        self.assertIn("queue_receipt", first)
        restarted = self.send(self.adapter())
        self.assertEqual(restarted["state"], "delivery_unknown")
        self.assertEqual(restarted["queue_receipt"], first["queue_receipt"])
        with self.assertRaises(ReasonEngineError):
            self.send(self.adapter(), root="another-human-root", dispatch="another-dispatch")
        self.assertEqual(len(self.starts), 1)

    def test_malformed_durable_json_is_ignored_even_when_wire_omits_optional_receipt(self):
        for index, text in enumerate(("{invalid", "[]", "null")):
            with self.subTest(text=text):
                self.isolate_case("json-" + str(index))
                result = self.send(self.adapter(facts_text=text, omit_wire_queue=True))
                self.assertEqual(result["state"], "transport_accepted")
                self.assertNotIn("queue_receipt", result)
                self.assertNotIn("queue_receipt", self.send(self.adapter()))

    def test_unanchored_public_fields_are_removed_on_duplicate_and_observation(self):
        first = self.send(self.adapter(legacy=True))
        with closing(sqlite3.connect(self.ledger)) as db, db:
            first["queue_receipt"] = {"queued_submission_id": "unanchored-public-field"}
            db.execute("UPDATE avalokita_reason_dispatch SET receipt=?", (json.dumps(first),))
        repeated = self.send(self.adapter())
        self.assertNotIn("queue_receipt", repeated)
        observed = self.adapter().reconcile_native_observation("dispatch-1", "human-root", self.binding,
            self.observation(), validate_observation=lambda _: True)
        self.assertEqual(observed["state"], "native_result_verified")
        self.assertNotIn("queue_receipt", observed)
        self.assertEqual(len(self.starts), 1)

    def test_claude_receipts_cannot_claim_a_codex_queue(self):
        binding = replace(self.binding, provider="claude-code")
        result = self.send(self.adapter(), binding=binding)
        self.assertEqual(result["state"], "delivery_unknown")
        self.assertNotIn("queue_receipt", result)
        self.isolate_case("legacy-claude")
        legacy = self.send(self.adapter(legacy=True), binding=binding)
        self.assertEqual(legacy["state"], "transport_accepted")
        self.assertNotIn("queue_receipt", legacy)


if __name__ == "__main__":
    unittest.main()
