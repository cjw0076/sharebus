"""Inline public policy and failure atomicity; synthetic bus, no native processes."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import server
from bridge import BridgeError
import test_bridge as bridge_fixture


class PromptPolicyTests(unittest.TestCase):
    def setUp(self):
        bridge_fixture.BridgeTests.setUp(self)
        self.assets = self.workspace / "policy-assets"
        self.assets.mkdir()
        self.policy = self.assets / "HARMONY.md"
        self.policy.write_bytes(b"Selected synthetic collaboration policy.\r\n")
        self.asset_patch = patch.object(server, "HERE", self.assets)
        self.asset_patch.start()
        self.addCleanup(self.asset_patch.stop)

    def tearDown(self):
        self.runtime.begin_closing()
        self.runtime.hosted.close()
        self.runtime.stop_peer_reconciler()
        self.runtime.device_discovery.stop()
        bridge_fixture.BridgeTests.tearDown(self)

    def assert_rejected_without_work(self, body="Selected human work"):
        with patch.object(self.runtime.hosted, "before_prompt") as onboarding, \
                patch.object(self.runtime.store, "new_goal", wraps=self.runtime.store.new_goal) as new_goal, \
                patch.object(self.bridge, "dispatch", wraps=self.bridge.dispatch) as dispatch:
            with self.assertRaises(BridgeError):
                self.runtime.prompt({"text": body, "idempotency_key": "rejected-policy"})
        onboarding.assert_not_called()
        new_goal.assert_not_called()
        dispatch.assert_not_called()
        self.assertIsNone(self.runtime.hosted.server)
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertEqual(self.bus.store.jobs(), [])
        with self.bridge._db() as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0], 0)

    def test_missing_policy_is_rejected_before_onboarding_or_goal(self):
        self.policy.unlink()
        self.assert_rejected_without_work()

    def test_unreadable_policy_is_rejected_before_onboarding_or_goal(self):
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == self.policy:
                raise PermissionError("synthetic public asset refusal")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", denied):
            self.assert_rejected_without_work()

    def test_bad_utf8_policy_is_rejected_before_onboarding_or_goal(self):
        self.policy.write_bytes(b"invalid-utf8:\xff")
        self.assert_rejected_without_work()

    def test_oversize_policy_is_rejected_before_onboarding_or_goal(self):
        self.policy.write_bytes(b"x" * (server.MAX_PROMPT_POLICY_BYTES + 1))
        self.assert_rejected_without_work()

    def test_combined_utf8_byte_overflow_is_rejected_before_onboarding_or_goal(self):
        self.policy.write_bytes(b"x" * 12000)
        body = "가" * 20000
        self.assertLess(len(body.encode("utf-8")), server.MAX_NATIVE_PROMPT_BYTES)
        self.assert_rejected_without_work(body)

    def test_inline_policy_preserves_raw_human_bytes_hash_and_native_metadata(self):
        body = "  사용자 의도\r\n한글과 🍃\t\n"
        policy = self.policy.read_bytes()
        payload = {"text": body, "idempotency_key": "raw-human", "budget": 7, "ttl": 120}
        response = self.runtime.prompt(payload)
        self.assertEqual(response["dispatch"]["state"], "queued")
        with self.bus.store.connection() as db:
            forwarded = db.execute("SELECT body FROM events WHERE id=?", (response["dispatch"]["event_id"],)).fetchone()[0]
        self.assertIn(b"<avalokita_policy>\n" + policy + b"\n</avalokita_policy>", forwarded.encode("utf-8"))
        self.assertTrue(forwarded.encode("utf-8").endswith(b"<human_prompt>\n" + body.encode("utf-8") + b"\n</human_prompt>"))
        self.assertNotIn("Read " + str(self.policy), forwarded)
        self.assertIn(str(self.assets / "avalokita.py") + " peer --help", forwarded)
        self.assertIn("Harmony goal: " + response["goal"]["id"] + ".", forwarded)
        expected_hash = hashlib.sha256(json.dumps([body, 7, 120.0], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self.runtime.store._connection() as db:
            goal = db.execute("SELECT prompt,request_hash FROM goals WHERE id=?", (response["goal"]["id"],)).fetchone()
        self.assertEqual(goal["prompt"].encode("utf-8"), body.encode("utf-8"))
        self.assertEqual(goal["request_hash"], expected_hash)

    def test_accepted_receipt_recovers_with_missing_policy_without_rewriting_event(self):
        payload = {"text": "Original accepted human work", "idempotency_key": "accepted-policy"}
        first = self.runtime.prompt(payload)
        self.assertEqual(first["dispatch"]["state"], "queued")
        with self.bus.store.connection() as db:
            event = tuple(db.execute("SELECT * FROM events WHERE id=?", (first["dispatch"]["event_id"],)).fetchone())
            roots = tuple(tuple(row) for row in db.execute("SELECT * FROM roots"))
            jobs = tuple(tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id"))
        with self.runtime.store._connection() as db:
            goal = tuple(db.execute("SELECT * FROM goals WHERE id=?", (first["goal"]["id"],)).fetchone())
        self.policy.unlink()
        with patch.object(self.runtime.hosted, "before_prompt", side_effect=AssertionError("Recovery initialized providers")):
            recovered = self.runtime.prompt(payload)
        self.assertEqual(recovered["goal"]["id"], first["goal"]["id"])
        self.assertEqual(recovered["dispatch"]["event_id"], first["dispatch"]["event_id"])
        with self.bus.store.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM events WHERE id=?", (first["dispatch"]["event_id"],)).fetchone()), event)
            self.assertEqual(tuple(tuple(row) for row in db.execute("SELECT * FROM roots")), roots)
            self.assertEqual(tuple(tuple(row) for row in db.execute("SELECT * FROM jobs ORDER BY id")), jobs)
        with self.runtime.store._connection() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM goals WHERE id=?", (first["goal"]["id"],)).fetchone()), goal)


if __name__ == "__main__":
    unittest.main()
