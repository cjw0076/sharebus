"""Disposable QA ledgers and fake Reason adapters; native input/model calls 0."""
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from adapters.reason_engine import NativeBinding
from reason_sessions import (
    Participant,
    ReasonSessions,
    SessionError,
    _read_selected,
    dispatch_reserved_continuation,
    verify_native_host,
)

RUNNER_PATH = HERE.parents[1] / "work" / "reason-native-qa" / "runner.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("reason_native_qa_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(runner)
DIRECT_SPEC = importlib.util.spec_from_file_location("reason_native_direct_probe", RUNNER_PATH.with_name("direct_probe.py"))
direct_probe = importlib.util.module_from_spec(DIRECT_SPEC)
DIRECT_SPEC.loader.exec_module(direct_probe)


class ReasonSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.engine = self.workspace / "fixture-engine"
        self.engine.write_bytes(b"not an executable")
        self.sent = []
        self.next_state = "transport_accepted"
        self.before_commit = None
        self.on_native_input = None
        self.host_ok = True
        self.observed = []
        fixture = self

        class Adapter:
            def __init__(self, **kwargs):
                self.gate = kwargs["authorize_send"]

            def send_authorized(self, binding, prompt, **kwargs):
                if self.gate(kwargs["authorization_record"]) is not True:
                    return self.result("not_sent")
                if fixture.before_commit:
                    fixture.before_commit()
                if self.gate(kwargs["authorization_record"]) is not True:
                    return self.result("not_sent")
                fixture.sent.append({"binding": binding, "prompt": prompt, **kwargs})
                if fixture.on_native_input:
                    fixture.on_native_input(binding, kwargs)
                return self.result(fixture.next_state)

            @staticmethod
            def result(state):
                return {"state": state, "native_input_attempted": state != "not_sent", "transport_accepted": state == "transport_accepted",
                        "native_accepted": state in {"native_input_verified", "native_result_verified"},
                        "native_result_verified": state == "native_result_verified", "automatic_retry": False, "fallback_attempted": False}

            def reconcile_native_observation(self, dispatch_id, root_id, binding, observation, *, validate_observation):
                if not validate_observation(observation):
                    raise ValueError("fixture rejected observation")
                fixture.observed.append({"dispatch_id": dispatch_id, "root_id": root_id, "binding": binding, **observation})
                return {"native_accepted": True, "native_result_verified": observation["kind"] == "result_completed"}

        self.factory = Adapter
        self.verifier = lambda participant: self.host_ok
        self.store, self.operator = ReasonSessions.bootstrap(self.workspace, engine_path=self.engine,
            engine_sha256=hashlib.sha256(self.engine.read_bytes()).hexdigest(), adapter_factory=self.factory, host_verifier=self.verifier)
        a = NativeBinding("claude-code", "native-existing-a", str(self.workspace))
        b = NativeBinding("codex", "native-existing-b", str(self.workspace))
        self.participants = [Participant("a", "CEO", a, ("b",)), Participant("b", "CEO", b, ("a",))]
        self.goal = self.start()

    def start(self, budget=3, key="one-human"):
        return self.store.start_human_goal(self.operator, prompt="Only selected nonce/hash peer QA; do not change the project.",
            participants=self.participants, budget=budget, deadline=time.time() + 300, goal_key=key)

    def seed(self):
        return self.store.seed_human_goal(self.operator, goal_id=self.goal["goal_id"], target="a")

    def send(self, parent_id, body="selected A request", key="a-to-b"):
        return self.store.send(self.goal["permits"]["a"], target="b", parent_id=parent_id, body=body, key=key)

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_peer_chain_is_separate_from_transport_receipt(self):
        seed = self.seed()
        self.assertFalse(seed["native_result_verified"])
        ab = self.send(seed["message_id"])
        ba = self.store.reply(self.goal["permits"]["b"], parent_id=ab["message_id"], body="selected B counterexample and hash", key="b-to-a")
        result = self.store.final(self.goal["permits"]["a"], parent_id=ba["message_id"], body="Selected result depends on B", key="a-final")
        status = self.store.public_status(self.goal["goal_id"])
        self.assertEqual(len(self.sent), 3)
        self.assertEqual((status["used"], status["budget"], status["state"]), (3, 3, "completed"))
        self.assertTrue(all(row["native_response_verified"] for row in status["messages"]))
        self.assertEqual(result["state"], "selected_final")
        self.assertEqual([row["kind"] for row in self.observed], ["input_processed", "input_processed", "result_completed"])
        self.assertEqual([row["dispatch_id"] for row in self.observed], [seed["message_id"], ab["message_id"], ba["message_id"]])

    def test_sealed_capability_cannot_override_actor_or_roster(self):
        self.seed()
        path = Path(self.goal["permits"]["a"])
        permit = json.loads(path.read_text(encoding="utf-8"))
        permit["payload"]["actor_id"] = "b"
        path.write_text(json.dumps(permit), encoding="utf-8")
        with self.assertRaises(SessionError):
            self.store.peer_status(path)
        self.assertEqual(len(self.sent), 1)

    def test_wrong_native_host_cannot_read_status_or_send_or_final(self):
        seed = self.seed()
        self.host_ok = False
        for operation in (lambda: self.store.peer_status(self.goal["permits"]["a"]),
                          lambda: self.send(seed["message_id"]),
                          lambda: self.store.final(self.goal["permits"]["a"], parent_id=seed["message_id"], body="fake", key="f")):
            with self.assertRaises(SessionError):
                operation()
        self.assertEqual(len(self.sent), 1)

    def test_outside_roster_and_forged_parent_reject_without_budget(self):
        seed = self.seed()
        for target, parent in (("unknown", seed["message_id"]), ("b", "unknown")):
            with self.assertRaises(SessionError):
                self.store.send(self.goal["permits"]["a"], target=target, parent_id=parent, body="selected", key=target)
        with self.assertRaises(SessionError):
            self.store.send(self.goal["permits"]["b"], target="a", parent_id=seed["message_id"], body="spoof", key="spoof")
        self.assertEqual(self.store.public_status(self.goal["goal_id"])["used"], 1)

    def test_repeat_same_key_returns_receipt_even_after_restart_no_native_send(self):
        seed = self.seed()
        ab = self.send(seed["message_id"])
        restarted = ReasonSessions(self.workspace, adapter_factory=self.factory, host_verifier=self.verifier)
        again = restarted.send(self.goal["permits"]["a"], target="b", parent_id=seed["message_id"], body="selected A request", key="a-to-b")
        self.assertEqual(again["message_id"], ab["message_id"])
        self.assertTrue(again["duplicate_suppressed"])
        self.assertEqual(len(self.sent), 2)
        with self.assertRaises(SessionError):
            self.send(seed["message_id"], body="changed under the same key")

    def test_unknown_is_not_retried_with_another_key(self):
        seed = self.seed()
        self.next_state = "delivery_unknown"
        result = self.send(seed["message_id"])
        self.assertEqual(result["state"], "delivery_unknown")
        with self.assertRaises(SessionError):
            self.send(seed["message_id"], key="new-key-to-retry")
        self.assertEqual(len(self.sent), 2)

    def test_unknown_seed_can_be_resolved_by_exact_native_host_response(self):
        self.next_state = "delivery_unknown"
        seed = self.seed()
        self.next_state = "transport_accepted"
        ab = self.send(seed["message_id"])
        self.assertEqual(ab["state"], "transport_accepted")
        self.assertEqual(self.store.public_status(self.goal["goal_id"])["messages"][0]["native_response_verified"], 1)

    def test_not_sent_parent_is_not_an_input_and_cannot_be_reseeded(self):
        self.next_state = "not_sent"
        seed = self.seed()
        with self.assertRaises(SessionError):
            self.send(seed["message_id"])
        with self.assertRaises(SessionError):
            self.store.seed_human_goal(self.operator, goal_id=self.goal["goal_id"], target="a", key="different-seed")
        same = self.seed()
        self.assertTrue(same["duplicate_suppressed"])
        self.assertEqual(len(self.sent), 1)

    def test_pause_before_commit_blocks_native_input_and_late_selected_result_is_preserved(self):
        self.before_commit = lambda: self.store.control(self.operator, goal_id=self.goal["goal_id"], action="pause")
        seed = self.seed()
        self.assertEqual(seed["state"], "not_sent")
        self.assertEqual(self.sent, [])

    def test_deadline_budget_pause_and_operator_auth_fail_closed(self):
        seed = self.seed()
        with self.assertRaises(SessionError):
            self.store.control("wrong", goal_id=self.goal["goal_id"], action="pause")
        self.store.control(self.operator, goal_id=self.goal["goal_id"], action="pause")
        with self.assertRaises(SessionError):
            self.send(seed["message_id"])
        self.store.control(self.operator, goal_id=self.goal["goal_id"], action="resume")
        with self.store._db() as db:
            db.execute("UPDATE goals SET deadline=0")
        with self.assertRaises(SessionError):
            self.send(seed["message_id"])
        late = self.store.final(self.goal["permits"]["a"], parent_id=seed["message_id"], body="Late selected result", key="late")
        self.assertEqual(late["state"], "selected_final")
        self.assertEqual(self.store.public_status(self.goal["goal_id"])["deadline"], 0)

    def test_public_projection_contains_no_text_paths_native_ids_or_capabilities(self):
        self.seed()
        text = json.dumps(self.store.public_status(self.goal["goal_id"]))
        for private in ("Only selected", "native-existing-a", "native-existing-b", self.operator, str(self.workspace), "reason-permits"):
            self.assertNotIn(private, text)
        self.assertIn("isolated_native_qa", text)

    def test_goal_cannot_be_duplicated_or_extend_immutable_roster(self):
        with self.assertRaises(SessionError):
            self.start(key="fresh-human-key")
        with self.assertRaises(SessionError):
            self.start()
        self.assertEqual(self.store.public_status(self.goal["goal_id"])["used"], 0)

    def test_private_file_and_credential_body_are_not_selected_output(self):
        with self.assertRaises(SessionError):
            _read_selected(Path(self.goal["permits"]["a"]))
        seed = self.seed()
        with self.assertRaises(SessionError):
            self.send(seed["message_id"], body="Authorization: Bearer do-not-copy-this")
        self.assertEqual(len(self.sent), 1)

    def test_canonical_seam_uses_existing_root_and_reservation_without_qa_budget(self):
        source, target = self.participants
        prompt = "Selected canonical continuation"
        record = {"authority_scope": "harmony", "goal_id": "canonical-goal", "root_id": "canonical-root", "input_id": "child-event",
            "job_id": "child-job", "event_id": "child-event", "human_event_id": "human-event", "parent_job_id": "parent-job", "parent_event_id": "parent-event",
            "dispatch_id": "child-job", "source_actor_id": "a", "target_actor_id": "b", "binding_sha256": target.binding.sha256,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "deadline": time.time() + 60,
            "human_authorized": True, "root_active": True, "recipient_registered": True, "workspace_paused": False,
            "recipient_paused": False, "claim_state": "claimed", "budget_remaining": 1}
        gate_calls = []
        result = dispatch_reserved_continuation(source=source, target=target, prompt=prompt, authorization_record=record,
            authorize_send=lambda value: gate_calls.append(value["root_id"]) or True,
            engine_path=self.engine, engine_sha256="f" * 64, ledger_path=self.store.private / "canonical-transport.sqlite3",
            adapter_factory=self.factory, host_verifier=self.verifier)
        self.assertEqual(result["state"], "transport_accepted")
        self.assertEqual(gate_calls, ["canonical-root", "canonical-root"])
        self.assertEqual(self.store.public_status(self.goal["goal_id"])["used"], 0)

    def test_canonical_seam_rejects_missing_lineage_or_gate_revocation(self):
        source, target = self.participants
        with self.assertRaises(SessionError):
            dispatch_reserved_continuation(source=source, target=target, prompt="selected", authorization_record={}, authorize_send=lambda _: True,
                engine_path=self.engine, engine_sha256="f" * 64, ledger_path=self.store.private / "canonical.sqlite3", adapter_factory=self.factory, host_verifier=self.verifier)
        self.assertEqual(len(self.sent), 0)

    def test_codex_identity_requires_current_workspace_and_exact_thread_env(self):
        participant = self.participants[1]
        with patch("reason_sessions.Path.cwd", return_value=self.workspace), patch.dict(os.environ, {"CODEX_THREAD_ID": participant.binding.native_id}):
            self.assertTrue(verify_native_host(participant))
        with patch("reason_sessions.Path.cwd", return_value=self.workspace), patch.dict(os.environ, {"CODEX_THREAD_ID": "another-thread"}):
            self.assertFalse(verify_native_host(participant))
        with patch("reason_sessions.Path.cwd", return_value=self.workspace.parent), patch.dict(os.environ, {"CODEX_THREAD_ID": participant.binding.native_id}):
            self.assertFalse(verify_native_host(participant))

    def test_claude_identity_requires_exact_registry_process_birth_and_ancestor(self):
        registry = self.workspace / "claude-fixture.json"
        data = {"sessionId": "native-a", "pid": 123, "cwd": str(self.workspace), "startedAt": 1000, "procStart": "123456", "messagingSocketPath": "fixture-pipe"}
        registry.write_text(json.dumps(data), encoding="utf-8")
        binding = NativeBinding("claude-code", "native-a", str(self.workspace), registry_path=str(registry), pid=123, started_at_ms=1000, socket_path="fixture-pipe")
        participant = Participant("a", "CEO", binding, ("b",), "123456")
        identities = {123: {"pid": 123, "parent_pid": 1, "started_at": "123456"}, 900: {"pid": 900, "parent_pid": 123, "started_at": "999999"}}
        with patch("reason_sessions.Path.cwd", return_value=self.workspace), patch("reason_sessions.os.getpid", return_value=900), patch("reason_sessions.process_identity", side_effect=identities.__getitem__):
            self.assertTrue(verify_native_host(participant))
            self.assertFalse(verify_native_host(replace(participant, process_started_at="reused-pid")))
            identities[900]["parent_pid"] = 1
            self.assertFalse(verify_native_host(participant))

    def test_prepared_without_durable_native_attempt_is_not_a_response_parent(self):
        seed = self.seed()
        with self.store._db() as db:
            db.execute("UPDATE messages SET state='prepared' WHERE id=?", (seed["message_id"],))
        with self.assertRaises(SessionError):
            self.send(seed["message_id"])
        self.assertEqual(len(self.sent), 1)

    def test_fast_native_callback_requires_exact_original_durable_attempt(self):
        seed = self.seed()
        with self.store._db() as db:
            db.execute("UPDATE messages SET state='prepared' WHERE id=?", (seed["message_id"],))
        proof = sqlite3.connect(self.store.private / "reason-transport.sqlite3")
        try:
            proof.execute("CREATE TABLE avalokita_reason_dispatch(dispatch_id TEXT PRIMARY KEY,receipt TEXT)")
            receipt = {"dispatch_id": seed["message_id"], "root_id": "another-root", "binding_sha256": self.participants[0].binding.sha256, "native_input_attempted": True}
            proof.execute("INSERT INTO avalokita_reason_dispatch VALUES(?,?)", (seed["message_id"], json.dumps(receipt)))
            proof.commit()
            with self.assertRaises(SessionError):
                self.send(seed["message_id"])
            receipt["root_id"] = self.goal["goal_id"]
            proof.execute("UPDATE avalokita_reason_dispatch SET receipt=?", (json.dumps(receipt),))
            proof.commit()
        finally:
            proof.close()
        result = self.send(seed["message_id"])
        self.assertEqual(result["state"], "transport_accepted")
        self.assertEqual(len(self.sent), 2)

    def test_fast_helper_callback_is_not_downgraded_by_late_transport_return(self):
        def callback(binding, values):
            self.on_native_input = None
            proof = sqlite3.connect(self.store.private / "reason-transport.sqlite3")
            try:
                proof.execute("CREATE TABLE avalokita_reason_dispatch(dispatch_id TEXT PRIMARY KEY,receipt TEXT)")
                receipt = {"dispatch_id": values["dispatch_id"], "root_id": values["root_id"],
                    "binding_sha256": binding.sha256, "native_input_attempted": True}
                proof.execute("INSERT INTO avalokita_reason_dispatch VALUES(?,?)", (values["dispatch_id"], json.dumps(receipt)))
                proof.commit()
            finally:
                proof.close()
            self.send(values["dispatch_id"])
            self.next_state = "native_input_verified"
        self.on_native_input = callback
        seed = self.seed()
        self.assertEqual(seed["state"], "native_input_verified")
        self.assertTrue(seed["native_accepted"])
        self.assertFalse(seed["native_result_verified"])
        original = self.store.public_status(self.goal["goal_id"])["messages"][0]
        self.assertEqual((original["state"], original["native_response_verified"]), ("native_input_verified", 1))
        self.assertEqual(len(self.sent), 2)

    def test_runner_verifies_nonce_dependency_and_concrete_review_in_fake_chain(self):
        seed = self.seed()
        nonce_a, nonce_b = "a" * 32, "b" * 32
        a = {"step": "challenge", "nonce_a": nonce_a, "candidate_id": nonce_a[:6], "proposal": "short_prefix"}
        ab = self.send(seed["message_id"], body=json.dumps(a))
        digest = hashlib.sha256((nonce_a + "|" + nonce_b).encode()).hexdigest()
        b = {"step": "review", "nonce_a": nonce_a, "nonce_b": nonce_b, "digest": digest,
             "collision": {"left": nonce_a, "right": nonce_a[:6] + "0" * 26}, "recommendation": "full_sha256"}
        ba = self.store.reply(self.goal["permits"]["b"], parent_id=ab["message_id"], body=json.dumps(b), key="b-review")
        final = {"step": "final", "nonce_a": nonce_a, "nonce_b": nonce_b, "digest": digest, "chosen_policy": "full_sha256", "review_applied": True}
        self.store.final(self.goal["permits"]["a"], parent_id=ba["message_id"], body=json.dumps(final), key="a-final")
        path = self.store.private / "fixture-manifest.json"
        path.write_text(json.dumps({"contract": "avalokita.reason-native-qa/1", "workspace": str(self.workspace),
            "goal_id": self.goal["goal_id"], "initiator": "a", "reviewer": "b"}), encoding="utf-8")
        result = runner.verify(path)
        self.assertTrue(all(result["checks"].values()))
        self.assertNotIn(nonce_a, json.dumps(result))
        self.assertIn("not_production_gui", result["scope"])

    def test_runner_incomplete_transport_is_not_a_roundtrip(self):
        self.seed()
        path = self.store.private / "fixture-manifest.json"
        path.write_text(json.dumps({"contract": "avalokita.reason-native-qa/1", "workspace": str(self.workspace),
            "goal_id": self.goal["goal_id"], "initiator": "a", "reviewer": "b"}), encoding="utf-8")
        result = runner.verify(path)
        self.assertEqual(result["verdict"], "incomplete_or_failed")
        self.assertFalse(result["checks"]["all_native_callbacks"])

    def test_native_host_lost_at_second_gate_is_not_sent(self):
        seed = self.seed()
        self.before_commit = lambda: setattr(self, "host_ok", False)
        result = self.send(seed["message_id"])
        self.assertEqual(result["state"], "not_sent")
        self.assertEqual(len(self.sent), 1)

    def test_copying_database_to_another_workspace_does_not_copy_authority(self):
        import shutil
        other = self.workspace / "different-workspace"
        (other / ".avalokita" / "private").mkdir(parents=True)
        shutil.copyfile(self.store.db_path, other / ".avalokita" / "private" / "reason-sessions.sqlite3")
        with self.assertRaises(SessionError):
            ReasonSessions(other).public_status(self.goal["goal_id"])

    def direct_goal(self, *, budget=1, participant=None, key="direct-human"):
        participant = participant or replace(self.participants[1], allowed_targets=())
        return self.store.start_human_goal(self.operator, prompt="A new direct arithmetic QA only.", participants=[participant],
            budget=budget, deadline=time.time() + 300, goal_key=key)

    def test_single_codex_requires_no_peer_targets_and_exactly_one_input_budget(self):
        self.store.control(self.operator, goal_id=self.goal["goal_id"], action="stop")
        for budget, participant in ((2, replace(self.participants[1], allowed_targets=())),
                                    (1, self.participants[1]), (1, replace(self.participants[0], allowed_targets=()))):
            with self.subTest(budget=budget, provider=participant.binding.provider), self.assertRaises(SessionError):
                self.direct_goal(budget=budget, participant=participant)
        goal = self.direct_goal()
        self.assertEqual((self.store.public_status(goal["goal_id"])["budget"], self.store.public_status(goal["goal_id"])["used"]), (1, 0))

    def test_single_codex_cannot_overlap_old_authority_or_implicitly_stop_it(self):
        before = self.store.public_status(self.goal["goal_id"])
        with self.assertRaises(SessionError):
            self.direct_goal()
        self.assertEqual(before, self.store.public_status(self.goal["goal_id"]))

    def test_single_codex_has_no_peer_send_reply_or_second_human_seed(self):
        self.store.control(self.operator, goal_id=self.goal["goal_id"], action="stop")
        goal = self.direct_goal()
        seed = self.store.seed_human_goal(self.operator, goal_id=goal["goal_id"], target="b")
        permit = goal["permits"]["b"]
        for operation in (lambda: self.store.send(permit, target="a", parent_id=seed["message_id"], body="forbidden", key="peer"),
                          lambda: self.store.send(permit, target="b", parent_id=seed["message_id"], body="forbidden", key="self"),
                          lambda: self.store.reply(permit, parent_id=seed["message_id"], body="forbidden", key="reply"),
                          lambda: self.store.seed_human_goal(self.operator, goal_id=goal["goal_id"], target="b", key="different-seed")):
            with self.assertRaises(SessionError):
                operation()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.public_status(goal["goal_id"])["used"], 1)

    def test_single_codex_unknown_is_not_replayed_and_exact_final_is_distinct(self):
        self.store.control(self.operator, goal_id=self.goal["goal_id"], action="stop")
        goal = self.direct_goal()
        self.next_state = "delivery_unknown"
        seed = self.store.seed_human_goal(self.operator, goal_id=goal["goal_id"], target="b")
        again = self.store.seed_human_goal(self.operator, goal_id=goal["goal_id"], target="b")
        self.assertTrue(again["duplicate_suppressed"])
        self.assertEqual(seed["state"], "delivery_unknown")
        self.host_ok = False
        with self.assertRaises(SessionError):
            self.store.final(goal["permits"]["b"], parent_id=seed["message_id"], body="wrong host", key="final")
        self.host_ok = True
        self.store.final(goal["permits"]["b"], parent_id=seed["message_id"], body="selected exact final", key="final")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.observed[-1]["kind"], "result_completed")

    def test_direct_probe_prepare_and_verified_final_preserve_prior_goal(self):
        self.store.control(self.operator, goal_id=self.goal["goal_id"], action="stop")
        prior = self.store.public_status(self.goal["goal_id"])
        spec_path = self.workspace / "direct-fixture-spec.json"
        spec_path.write_text(json.dumps({"workspace": str(self.workspace), "engine_path": str(self.engine),
            "engine_sha256": hashlib.sha256(self.engine.read_bytes()).hexdigest(),
            "participants": [asdict(replace(self.participants[1], allowed_targets=()))]}), encoding="utf-8")
        prepared = direct_probe.prepare(spec_path)
        path = Path(prepared["manifest_path"])
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(self.sent, [])
        self.assertEqual(direct_probe.verify(path)["verdict"], "incomplete_or_failed")
        goal_id = prepared["goal_id"]
        with self.store._db() as db:
            permit = db.execute("SELECT permit_path FROM participants WHERE goal_id=?", (goal_id,)).fetchone()[0]
        seed = self.store.seed_human_goal(self.operator, goal_id=goal_id, target="b")
        body = {"step": "direct_final", "nonce": manifest["nonce"], "a": manifest["a"], "b": manifest["b"],
                "result": manifest["a"] * manifest["b"] + manifest["a"] - manifest["b"]}
        self.store.final(permit, parent_id=seed["message_id"], body=json.dumps(body), key="final")
        verified = direct_probe.verify(path)
        self.assertEqual(verified["verdict"], "existing_codex_direct_final_pass")
        self.assertNotIn(manifest["nonce"], json.dumps(verified))
        self.assertEqual(prior, self.store.public_status(self.goal["goal_id"]))
        with self.store._db() as db:
            db.execute("UPDATE messages SET body=? WHERE goal_id=? AND kind='final'", (json.dumps({**body, "result": body["result"] + 1}), goal_id))
        self.assertEqual(direct_probe.verify(path)["verdict"], "incomplete_or_failed")


if __name__ == "__main__":
    unittest.main()
