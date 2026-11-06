"""Temporary workspaces and a disabled native supervisor; no model calls."""
import json
from pathlib import Path
import sys
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError, SharebusBridge
from server import Runtime, Server
from daemon import BusHTTPServer
from mcp_fixture import mcp_exchange


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.bus = BusHTTPServer(self.workspace, enable_native=False, discovery=[])
        (self.bus.store.private / "runtime.json").write_text(json.dumps(self.bus.runtime()), encoding="utf-8")
        self.thread = threading.Thread(target=self.bus.serve_forever, daemon=True)
        self.thread.start()
        self.agent, self.token = self.bus.store.register("claude", "Existing CEO", ["public-output"], "native_attached", role="CEO", native_session_id="original-claude")
        self.other, self.other_token = self.bus.store.register("antigravity", "Existing designer", [], "native_attached", role="Designer", native_session_id="original-agy")
        # Explicit synthetic live delivery metadata, not registration-as-readiness.
        self.bus.native_enabled = True
        self.bus.store.configure_auto(enabled=True, suspended=False)
        for actor in (self.agent, self.other):
            self.bus.store.update_agent(actor["id"], channel_verified=True)
            self.bus.store.observe_auto(actor["id"], "watching", "ready")
        self.fake, _ = self.bus.store.register("codex", "Old fixture", [], "external", native_session_id="not-attached")
        self.runtime = Runtime(self.workspace, discovery=False)
        self.bridge = self.runtime.bridge

    def tearDown(self):
        self.bus.shutdown()
        self.bus.close()
        self.temp.cleanup()

    def prompt(self, key="human-one", text="Build the selected document", **kwargs):
        return self.runtime.prompt({"text": text, "idempotency_key": key, **kwargs})

    def test_exact_native_subset_idempotency_and_restart(self):
        first = self.prompt()
        second = self.prompt()
        self.assertEqual(first["goal"]["id"], second["goal"]["id"])
        self.assertEqual({j["agent_id"] for j in self.bus.store.jobs()}, {self.agent["id"], self.other["id"]})
        self.assertEqual(len(self.bus.store.jobs()), 2)
        restarted = Runtime(self.workspace, discovery=False)
        third = restarted.prompt({"text": "Build the selected document", "idempotency_key": "human-one"})
        self.assertEqual(third["dispatch"]["root_id"], first["dispatch"]["root_id"])
        self.assertEqual(len(self.bus.store.jobs()), 2)
        self.assertEqual(self.bus.store.agent(self.agent["id"])["role"], "CEO")

    def test_unknown_outcome_never_resends(self):
        with patch.object(self.bridge, "_pinned_db", side_effect=BridgeError("uncertain", 409)):
            first = self.prompt()
        self.assertEqual(first["dispatch"]["state"], "unknown")
        self.assertEqual(self.prompt()["dispatch"]["state"], "unknown")
        self.assertEqual(self.bus.store.jobs(), [])

    def test_stale_or_unverified_registration_never_enters_default_queue(self):
        self.bus.store.update_agent(self.agent["id"], channel_verified=False)
        with self.bus.store.connection() as db:
            db.execute("UPDATE auto_observations SET updated_at=? WHERE agent_id=?", (time.time() - 121, self.other["id"]))
        result = self.prompt()
        self.assertEqual(result["dispatch"]["status"], "pending_capability")
        self.assertEqual(self.bus.store.jobs(), [])
        self.assertTrue(all(not actor["readiness"]["can_dispatch"] for actor in self.runtime.state()["participants"]))

    def test_quarantine_only_exact_unstarted_owned_unroutable_job(self):
        result = self.prompt()
        self.bus.store.observe_auto(self.agent["id"], "offline", "offline")
        original = {job["agent_id"]: dict(job) for job in self.bus.store.jobs()}
        value = self.bridge.quarantine_unroutable(result["goal"]["id"], self.agent["id"])
        self.assertTrue(value["classified"])
        jobs = {job["agent_id"]: job for job in self.bus.store.jobs()}
        self.assertEqual(jobs[self.agent["id"]]["id"], original[self.agent["id"]]["id"])
        self.assertEqual(jobs[self.agent["id"]]["state"], "avalokita_unroutable")
        self.assertEqual(jobs[self.other["id"]]["state"], "queued")
        self.assertEqual(self.bus.store.agent(self.agent["id"])["state"], "idle")
        self.assertFalse(self.bus.store.paused())
        self.bus.store.claim(self.other["id"])
        self.bus.store.observe_auto(self.other["id"], "offline", "offline")
        with self.assertRaises(BridgeError):
            self.bridge.quarantine_unroutable(result["goal"]["id"], self.other["id"])
        self.assertEqual(self.bus.store.jobs()[1]["state"] if self.bus.store.jobs()[1]["agent_id"] == self.other["id"] else self.bus.store.jobs()[0]["state"], "running")

    def test_pause_blocks_existing_queue_scope_only_and_resume_same_ids(self):
        result = self.prompt()
        original_ids = {j["id"] for j in self.bus.store.jobs()}
        self.runtime.control("pause")
        self.assertIsNone(self.bus.store.claim(self.agent["id"]))
        self.assertTrue(all(j["state"] == "avalokita_paused" for j in self.bus.store.jobs()))
        self.assertFalse(self.bus.store.paused())
        self.runtime.control("resume")
        self.assertEqual(original_ids, {j["id"] for j in self.bus.store.jobs()})
        self.assertTrue(all(j["state"] == "queued" for j in self.bus.store.jobs()))
        self.assertEqual(self.bus.store.claim(self.agent["id"])["root_id"], result["dispatch"]["root_id"])

    def test_pause_does_not_revoke_accepted_work_or_resume_foreign_native_control(self):
        self.prompt()
        job = self.bus.store.claim(self.agent["id"])
        self.runtime.control("pause")
        self.assertEqual(next(j for j in self.bus.store.jobs() if j["id"] == job["id"])["state"], "running")
        self.bus.store.update_agent(self.other["id"], state="paused")
        self.runtime.control("resume")
        self.assertEqual(self.bus.store.agent(self.other["id"])["state"], "paused")
        self.assertEqual(next(j for j in self.bus.store.jobs() if j["agent_id"] == self.other["id"])["state"], "avalokita_paused")

    def test_exact_question_continuation_and_parent_correlation(self):
        first = self.prompt()
        job = self.bus.store.claim(self.agent["id"])
        question = self.bus.store.send(self.agent["id"], "user", "Which version?", "question")
        public = self.runtime.state()
        shown = next(m for m in public["messages"] if m.get("question_id") == question["event"]["id"])
        self.assertTrue(shown["question_pending"])
        answer = self.prompt("answer-one", "Use version 2", reply_to=shown["question_id"])
        old = next(j for j in self.bus.store.jobs() if j["id"] == job["id"])
        self.assertEqual(old["state"], "input_received")
        claimed = self.bus.store.claim(self.agent["id"])
        self.assertEqual(claimed["root_id"], answer["dispatch"]["root_id"])
        self.assertEqual(claimed["event"]["metadata"]["parent_root_id"], first["dispatch"]["root_id"])
        self.assertEqual(answer["dispatch"]["participants"], [self.agent["id"]])
        self.assertFalse(any(m.get("question_id") == shown["question_id"] for m in self.runtime.state()["messages"]))
        self.prompt("answer-one", "Use version 2", reply_to=shown["question_id"])
        self.assertEqual(len(self.bus.store.jobs()), 3)

    def test_generic_prompt_does_not_implicitly_answer_waiting_question(self):
        self.prompt()
        job = self.bus.store.claim(self.agent["id"])
        self.bus.store.send(self.agent["id"], "user", "Which version?", "question")
        self.prompt("other-request", "Independent task")
        self.assertEqual(next(j for j in self.bus.store.jobs() if j["id"] == job["id"])["state"], "waiting_user")
        self.assertEqual(self.bus.store.agent(self.agent["id"])["state"], "waiting_for_user")

    def test_peer_exact_root_impersonation_and_paused_public_read(self):
        result = self.prompt()
        job = self.bus.store.claim(self.agent["id"])
        root = result["goal"]["id"]
        self.runtime.peer(self.token, {"root_id": root, "operation": "publish", "kind": "result", "body": "Selected output"})
        with self.assertRaises(BridgeError):
            self.runtime.peer(self.token, {"actor": self.other["id"], "root_id": root, "operation": "publish", "body": "impersonation"})
        self.runtime.control("pause")
        capture = self.runtime.peer(self.token, {"root_id": root, "operation": "capture-pane"})
        self.assertEqual(capture["messages"][0]["body"], "Selected output")
        with self.assertRaises(BridgeError):
            self.runtime.peer(self.token, {"root_id": root, "operation": "publish", "body": "paused"})
        self.assertEqual(self.bus.store.agent(self.agent["id"])["active_root"], job["root_id"])

    def test_peer_send_same_transaction_rejects_changed_root_and_never_retries(self):
        result = self.prompt()
        self.bus.store.claim(self.agent["id"])
        original = self.bridge._pinned_db
        def switched():
            db = original()
            with db:
                db.execute("UPDATE agents SET active_root='different-root' WHERE id=?", (self.agent["id"],))
            return db
        with patch.object(self.bridge, "_pinned_db", side_effect=switched):
            receipt = self.bridge.peer_send(self.token, self.other["id"], "One explicit peer message", "message", result["dispatch"]["root_id"], "peer-one")
        self.assertEqual(receipt["state"], "unknown")
        self.assertFalse(any(e["body"] == "One explicit peer message" for e in self.bus.store.events()["events"]))
        self.bus.store.update_agent(self.agent["id"], active_root=result["dispatch"]["root_id"])
        again = self.bridge.peer_send(self.token, self.other["id"], "One explicit peer message", "message", result["dispatch"]["root_id"], "peer-one")
        self.assertEqual(again["state"], "unknown")
        self.assertEqual(len(self.bus.store.jobs()), 2)

    def test_selected_output_excludes_unrelated_root_and_fake_history(self):
        result = self.prompt()
        self.bus.store.claim(self.agent["id"])
        self.bus.store.send(self.agent["id"], "user", "Selected current root", "result")
        unrelated = self.bus.store.send("user", self.fake["id"], "Unrelated business", "message")
        with self.bus.store.connection() as db:
            self.bus.store._insert_event(db, "result", self.fake["id"], "user", "Private unrelated output", unrelated["root_id"])
        bodies = [m["body"] for m in self.runtime.state()["messages"]]
        self.assertIn("Selected current root", bodies)
        self.assertNotIn("Private unrelated output", bodies)
        self.assertNotIn("Unrelated business", bodies)

    def test_peer_send_recipient_rebind_is_rejected_atomically(self):
        result = self.prompt()
        self.bus.store.claim(self.agent["id"])
        original = self.bridge._pinned_db
        def rebound():
            db = original()
            with db:
                db.execute("UPDATE agents SET native_session_id='new-unrelated-native' WHERE id=?", (self.other["id"],))
            return db
        with patch.object(self.bridge, "_pinned_db", side_effect=rebound):
            receipt = self.bridge.peer_send(self.token, self.other["id"], "Do not deliver after rebind", "message", result["dispatch"]["root_id"], "peer-rebind")
        self.assertEqual(receipt["state"], "unknown")
        self.assertFalse(any(e["body"] == "Do not deliver after rebind" for e in self.bus.store.events()["events"]))

    def test_peer_unready_or_unsupported_route_creates_no_intent_or_job(self):
        result = self.prompt()
        self.bus.store.claim(self.agent["id"])
        self.bus.store.observe_auto(self.other["id"], "offline", "offline")
        count = len(self.bus.store.jobs())
        with self.assertRaises(BridgeError) as caught:
            self.bridge.peer_send(self.token, self.other["id"], "Unavailable peer", "message", result["dispatch"]["root_id"], "peer-unready")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("pending_capability", str(caught.exception))
        codex, _ = self.bus.store.register("codex", "Direct-only peer", [], "external_attached", native_session_id="exact-codex-peer")
        with self.assertRaises(BridgeError):
            self.bridge.peer_send(self.token, codex["id"], "No pretend native peer IPC", "message", result["dispatch"]["root_id"], "peer-codex")
        self.assertEqual(len(self.bus.store.jobs()), count)
        with self.bridge._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM peer_dispatch").fetchone()[0], 0)

    def test_peer_fresh_legacy_route_accepts_and_readiness_race_is_known_unsent(self):
        result = self.prompt()
        self.bus.store.claim(self.agent["id"])
        accepted = self.bridge.peer_send(self.token, self.other["id"], "One ready peer message", "message", result["dispatch"]["root_id"], "peer-ready")
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(len(accepted["jobs"]), 1)
        count = len(self.bus.store.jobs())
        original = self.bridge._pinned_db
        def offline():
            db = original()
            with db:
                db.execute("UPDATE auto_observations SET state='offline',note='offline' WHERE agent_id=?", (self.other["id"],))
            return db
        with patch.object(self.bridge, "_pinned_db", side_effect=offline):
            receipt = self.bridge.peer_send(self.token, self.other["id"], "Route lost before queue", "message", result["dispatch"]["root_id"], "peer-race")
        self.assertEqual(receipt["state"], "pending_capability")
        self.assertFalse(receipt["native_attempted"])
        self.assertEqual(len(self.bus.store.jobs()), count)
        self.assertFalse(any(event["body"] == "Route lost before queue" for event in self.bus.store.events()["events"]))

    def test_resume_does_not_dispatch_to_rebound_native_or_expired_root(self):
        self.prompt()
        self.runtime.control("pause")
        self.bus.store.update_agent(self.other["id"], native_session_id="rebound")
        self.runtime.control("resume")
        other = next(j for j in self.bus.store.jobs() if j["agent_id"] == self.other["id"])
        self.assertEqual(other["state"], "avalokita_paused")
        self.runtime.control("pause")
        with self.bus.store.connection() as db:
            db.execute("UPDATE roots SET deadline=0")
        self.runtime.control("resume")
        self.assertTrue(all(j["state"] == "avalokita_paused" for j in self.bus.store.jobs()))

    def test_actual_peer_cli_and_mcp_tools_call_authenticate_original_job(self):
        result = self.prompt()
        self.bus.store.claim(self.agent["id"])
        server = Server(self.workspace, runtime=self.runtime)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            cli = subprocess.run([sys.executable, str(HERE / "avalokita.py"), "--workspace", str(self.workspace), "peer",
                                  "--actor", self.agent["id"], "--native-session-id", "original-claude", "--root-id", result["goal"]["id"],
                                  "--url", server.url, "--operation", "publish", "--arguments", json.dumps({"kind": "result", "body": "CLI selected output"})], capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout)["body"], "CLI selected output")
            env = {**os.environ, "AVALOKITA_WORKSPACE": str(self.workspace), "AVALOKITA_ACTOR": self.agent["id"],
                   "AVALOKITA_NATIVE_SESSION_ID": "original-claude", "AVALOKITA_ROOT_ID": result["goal"]["id"], "AVALOKITA_URL": server.url}
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "avalokita_peer", "arguments": {"operation": "publish", "arguments_json": json.dumps({"kind": "result", "body": "MCP selected output"})}}},
            ]
            mcp = mcp_exchange(HERE / "mcp_server.py", messages, env=env)
            self.assertEqual(mcp.returncode, 0, mcp.stderr)
            self.assertIn("MCP selected output", mcp.stdout)
            self.assertNotIn(self.token, cli.stdout + cli.stderr + mcp.stdout + mcp.stderr)
            self.assertNotIn(server.token, cli.stdout + cli.stderr + mcp.stdout + mcp.stderr)
        finally:
            server.shutdown()
            server.server_close()

    def test_concurrent_same_key_different_targets_rejects_losing_semantics(self):
        goal = self.runtime.store.new_goal("Same human body", idempotency_key="concurrent-human-key")
        other_bridge = SharebusBridge(self.workspace)
        barrier = threading.Barrier(2)
        first_observe, second_observe = self.bridge.observe, other_bridge.observe
        def observed(call):
            value = call()
            barrier.wait(timeout=3)
            return value
        results, errors = [], []
        def send(bridge, actor):
            try:
                results.append(bridge.dispatch(goal, "Same human body", recipient_ids=[actor]))
            except BridgeError as exc:
                errors.append(exc)
        with patch.object(self.bridge, "observe", side_effect=lambda: observed(first_observe)), patch.object(other_bridge, "observe", side_effect=lambda: observed(second_observe)):
            first = threading.Thread(target=send, args=(self.bridge, self.agent["id"]))
            second = threading.Thread(target=send, args=(other_bridge, self.other["id"]))
            first.start()
            second.start()
            first.join(8)
            second.join(8)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)
        self.assertEqual(len(self.bus.store.jobs()), 1)
        self.assertEqual(results[0]["participants"], [self.bus.store.jobs()[0]["agent_id"]])


if __name__ == "__main__":
    unittest.main()
