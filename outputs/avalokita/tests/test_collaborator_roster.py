"""Human admission / peer-scope regression in disposable synthetic workspaces.

No installed native adapter, executable, model, settings or existing goal is used.
The fixture supervisor is stopped before work is created; tests reserve jobs only.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError, BusStore, agent_binding
from server import Runtime
from test_hosted import FakeNative


class CollaboratorRosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        FakeNative.instances, FakeNative.gates, FakeNative.unknown, FakeNative.approvals = [], {}, set(), set()
        candidates = [{"provider": provider, "installed": True, "managed_supported": True} for provider in ("codex", "claude")]
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False,
                               hosted_factory=FakeNative, hosted_discovery=candidates)
        configured = self.runtime.hosted.configure({"providers": ["codex", "claude"]})
        self.bus = self.runtime.hosted.server
        supervisor = self.bus.supervisor
        dispatcher = supervisor._dispatcher
        supervisor._stop.set()
        supervisor.wake()
        dispatcher.join(timeout=2)
        self.assertFalse(dispatcher.is_alive(), "Fixture dispatcher must stop before jobs are created")
        self.actors = {item["provider"]: item["agent_id"] for item in configured["sessions"]}
        self.a, self.b = self.actors["codex"], self.actors["claude"]

    def tearDown(self):
        self.assertEqual(FakeNative.instances, [])
        self.runtime.hosted.close()
        self.temp.cleanup()

    def prompt(self, key="selected-goal", **overrides):
        return self.runtime.prompt({"text": "Coordinate the selected synthetic design", "idempotency_key": key,
                                    "recipient_ids": [self.a], "budget": 4, "ttl": 120, **overrides})

    def admit_a(self, result):
        job = self.bus.store.claim(self.a)
        self.assertEqual(job["root_id"], result["dispatch"]["root_id"])
        ledger = self.bus.supervisor.hosted
        ledger.begin(self.bus.store.agent(self.a), job, 1)
        self.bus.store.update_agent(self.a, generation=1)
        ledger.event(self.a, job, 1, {"type": "session", "session_id": "synthetic-observed-a"})
        token = (self.bus.store.private / "credentials" / (self.a + ".token")).read_text(encoding="utf-8").strip()
        return job, token

    def peer(self, result, job, token, **overrides):
        return self.runtime.peer(token, {"operation": "send-keys", "actor": self.a,
            "root_id": result["goal"]["id"], "recipient": self.b, "body": "Review the selected synthetic scope",
            "kind": "message", "idempotency_key": "a-to-b", "parent_job_id": job["id"],
            "parent_event_id": job["event_id"], **overrides})

    def test_initial_a_only_and_authorized_b_preserve_one_root_and_exact_parent(self):
        result = self.prompt(collaborator_ids=[self.b, self.a])
        receipt = result["dispatch"]
        self.assertEqual(receipt["recipient_ids"], [self.a])
        self.assertEqual(receipt["collaborator_ids"], sorted([self.a, self.b]))
        self.assertEqual(receipt["participants"], sorted([self.a, self.b]))
        self.assertEqual({job["agent_id"] for job in self.bus.store.jobs()}, {self.a})
        self.assertIsNone(self.bus.store.agent(self.b)["native_session_id"])
        expected_bindings = {actor: agent_binding(self.bus.store.agent(actor)) for actor in (self.a, self.b)}
        self.assertEqual(receipt["bindings"], expected_bindings)
        with self.bus.store.connection() as db:
            human = db.execute("SELECT metadata FROM events WHERE id=?", (receipt["event_id"],)).fetchone()
            meta = json.loads(human[0])
        self.assertEqual(meta["recipient_ids"], [self.a])
        self.assertEqual(meta["collaborator_ids"], sorted([self.a, self.b]))
        self.assertEqual(meta["bindings"], expected_bindings)
        self.assertTrue(meta["human_authorized"])

        job, token = self.admit_a(result)
        sent = self.peer(result, job, token)
        self.assertEqual(sent["state"], "accepted")
        self.assertEqual(sent["root_id"], receipt["root_id"])
        self.assertEqual(len(sent["jobs"]), 1)
        self.assertEqual(sent["jobs"][0]["agent_id"], self.b)
        self.assertEqual(sent["event"]["metadata"]["parent_job_id"], job["id"])
        self.assertEqual(sent["event"]["metadata"]["parent_event_id"], job["event_id"])
        self.assertEqual(sent["event"]["metadata"]["human_event_id"], receipt["event_id"])
        with self.bus.store.connection() as db:
            root = db.execute("SELECT budget,used,deadline FROM roots WHERE id=?", (receipt["root_id"],)).fetchone()
        self.assertEqual((root["budget"], root["used"]), (4, 1))
        self.assertEqual(root["deadline"], result["goal"]["deadline"])
        self.assertEqual(len(self.bus.store.jobs()), 2)
        duplicate = self.peer(result, job, token)
        self.assertEqual(duplicate["event"]["id"], sent["event"]["id"])
        self.assertEqual(len(self.bus.store.jobs()), 2)

    def test_omitted_roster_keeps_legacy_recipient_only_authority_and_403(self):
        result = self.prompt()
        self.assertEqual(result["dispatch"]["participants"], [self.a])
        job, token = self.admit_a(result)
        with self.assertRaises(BridgeError) as caught:
            self.peer(result, job, token)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(len(self.bus.store.jobs()), 1)
        with self.runtime.bridge._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM peer_dispatch").fetchone()[0], 0)

    def test_same_human_idempotency_key_cannot_expand_shrink_or_remove_the_roster(self):
        result = self.prompt(collaborator_ids=[self.a, self.b])
        for roster in ([self.a], None):
            with self.subTest(roster=roster), self.assertRaises(BridgeError) as caught:
                self.prompt(collaborator_ids=roster)
            self.assertEqual(caught.exception.status, 409)
        again = self.prompt(collaborator_ids=[self.b, self.a])
        self.assertEqual(again["goal"]["id"], result["goal"]["id"])
        self.assertEqual(len(self.bus.store.jobs()), 1)

    def test_legacy_goal_cannot_gain_collaborators_on_a_later_retry(self):
        self.prompt()
        with self.assertRaises(BridgeError) as caught:
            self.prompt(collaborator_ids=[self.a, self.b])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.bus.store.jobs()), 1)

    def test_initial_recipients_must_be_an_explicit_subset_of_the_authorized_roster(self):
        for fields in ({"collaborator_ids": [self.b]}, {"collaborator_ids": [self.a, self.a]},
                       {"collaborator_ids": [self.a, self.b], "recipient_ids": None}):
            with self.subTest(fields=fields), self.assertRaises(BridgeError):
                self.prompt(**fields)
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertEqual(self.bus.store.jobs(), [])

    def test_other_workspace_actor_cannot_join_this_human_roster(self):
        other = Path(self.temp.name) / "other-workspace"
        other.mkdir()
        foreign, _ = BusStore(other).register("claude", "Synthetic other workspace", [], "native_attached",
                                             native_session_id="synthetic-foreign-native")
        with self.assertRaises(BridgeError) as caught:
            self.prompt(collaborator_ids=[self.a, foreign["id"]])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertEqual(self.bus.store.jobs(), [])

    def test_paused_collaborator_is_rejected_before_human_goal_or_jobs(self):
        self.bus.store.update_agent(self.b, state="paused")
        with self.assertRaises(BridgeError) as caught:
            self.prompt(collaborator_ids=[self.a, self.b])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertEqual(self.bus.store.jobs(), [])

    def test_changed_hosted_owner_cannot_be_admitted_as_a_collaborator(self):
        with self.bus.store.connection() as db:
            db.execute("UPDATE avalokita_hosted_owners SET owner_id='synthetic-rebound-owner' WHERE actor=?", (self.b,))
        with self.assertRaises(BridgeError) as caught:
            self.prompt(collaborator_ids=[self.a, self.b])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertEqual(self.bus.store.jobs(), [])

    def test_hosted_owner_changed_after_observation_is_rechecked_in_the_admission_transaction(self):
        original = self.runtime.bridge._pinned_db

        def owner_changed():
            db = original()
            with db:
                db.execute("UPDATE avalokita_hosted_owners SET owner_id='synthetic-racing-owner' WHERE actor=?", (self.b,))
            return db

        with patch.object(self.runtime.bridge, "_pinned_db", side_effect=owner_changed):
            result = self.prompt(collaborator_ids=[self.a, self.b])
        self.assertEqual(result["dispatch"]["state"], "unknown")
        self.assertEqual(self.bus.store.jobs(), [])
        self.assertEqual(self.bus.store.events()["events"], [])

    def test_peer_cannot_supply_or_expand_the_human_roster(self):
        result = self.prompt()
        job, token = self.admit_a(result)
        with self.assertRaises(BridgeError) as caught:
            self.peer(result, job, token, collaborator_ids=[self.a, self.b])
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.runtime.bridge.dispatches()[result["goal"]["id"]]["participants"], [self.a])
        self.assertEqual(len(self.bus.store.jobs()), 1)

    def test_authorized_roster_does_not_make_an_unrelated_parent_job_valid(self):
        result = self.prompt(collaborator_ids=[self.a, self.b])
        job, token = self.admit_a(result)
        denied = self.peer(result, job, token, parent_job_id="unrelated-synthetic-job")
        self.assertEqual(denied["state"], "unknown")
        self.assertFalse(denied["delivery_verified"])
        self.assertEqual(len(self.bus.store.jobs()), 1)
        denied_again = self.peer(result, job, token, parent_job_id="unrelated-synthetic-job")
        self.assertEqual(denied_again["state"], "unknown")
        self.assertEqual(len(self.bus.store.jobs()), 1)


if __name__ == "__main__":
    unittest.main()
