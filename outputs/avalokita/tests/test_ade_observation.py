"""Disposable workspace fixtures; no provider or model is started."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import uuid

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError
from device_discovery import DeviceDiscovery
from native_dispatch import NativeDispatcher
from server import Runtime, Server
from daemon import BusHTTPServer


class ADEObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "a"
        self.workspace.mkdir()
        self.bus = BusHTTPServer(self.workspace, enable_native=False, discovery=[])
        (self.bus.store.private / "runtime.json").write_text(json.dumps(self.bus.runtime()), encoding="utf-8")
        self.bus_thread = threading.Thread(target=self.bus.serve_forever, daemon=True)
        self.bus_thread.start()
        self.actor, self.capability = self.bus.store.register("claude", "Existing engineer", [], "native_attached", role="CTO", native_session_id="own-original")
        self.other, _ = self.bus.store.register("antigravity", "Existing designer", [], "native_attached", role="Design", native_session_id="other-original")
        self.bus.native_enabled = True
        self.bus.store.configure_auto(enabled=True, suspended=False)
        for actor in (self.actor, self.other):
            self.bus.store.update_agent(actor["id"], channel_verified=True)
            self.bus.store.observe_auto(actor["id"], "watching", "ready")
        self.runtime = Runtime(self.workspace, discovery=False)
        self.server = Server(self.workspace, runtime=self.runtime)
        # Join this fixture's handlers before removing its temporary databases.
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.bus.shutdown()
        self.bus.close()
        self.temp.cleanup()

    def request(self, path, payload=None, token=None, *, server=None):
        server = server or self.server
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"X-Avalokita-Request": "1", "Authorization": "Bearer " + (server.token if token is None else token)}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request("POST" if payload is not None else "GET", path, None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            return result
        finally:
            connection.close()

    def goal(self):
        result = self.runtime.prompt({"text": "Publish selected work only", "idempotency_key": "one-real-human-scope"})
        self.job = self.bus.store.claim(self.actor["id"])
        return result["goal"]["id"], result["dispatch"]

    @staticmethod
    def rows(path):
        db = sqlite3.connect(path)
        try:
            return {name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()

    def test_summary_is_operator_only_pure_observation_without_native_or_recovery(self):
        goal, _ = self.goal()
        self.runtime.store.register_pane(self.actor["id"], "claude", "CTO", "own-original", [], verified=True)
        before = {path: self.rows(path) for path in (self.runtime.store.db_path, self.runtime.bridge.receipts, self.bus.store.db_path)}
        forbidden = lambda *_args, **_kwargs: self.fail("Summary crossed into live state/recovery/native authority")
        with patch.object(self.runtime, "state", forbidden), patch.object(self.runtime, "_recover_waiting", forbidden), \
                patch.object(self.runtime, "drain_native_peers", forbidden), patch.object(self.runtime.bridge, "observe", forbidden), \
                patch.object(self.runtime.bridge, "request", forbidden), patch.object(self.runtime.device_discovery, "refresh", forbidden):
            status, summary = self.request("/api/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["workspace"]["path"], str(self.workspace))
        self.assertEqual(summary["counts"]["goals"], 1)
        self.assertEqual(summary["counts"]["jobs"], 2)
        self.assertEqual(summary["counts"]["active_jobs"], 2)
        self.assertEqual(summary["counts"]["participants"], 2)
        self.assertEqual(summary["counts"]["published_panes"], 1)
        self.assertEqual(summary["inspection"]["scope"], "latest_50_goals")
        self.assertGreater(summary["observed_at"], 0)
        self.assertEqual(summary["discovery_status"]["state"], "disabled")
        self.assertEqual(before, {path: self.rows(path) for path in before})
        self.assertEqual(self.request("/api/summary", token="")[0], 401)
        self.assertEqual(self.request("/api/summary", token=self.capability)[0], 401)
        self.assertNotIn(self.capability, json.dumps(summary))

    def test_selected_context_and_staged_artifact_metadata_without_source_reads_or_private_content(self):
        goal, dispatch = self.goal()
        context_file = self.workspace / "selected.txt"
        context_file.write_text("Public selected content", encoding="utf-8")
        context = self.runtime.peer(self.capability, {"operation": "context", "root_id": goal, "paths": ["selected.txt"], "label": "Shared reference"})
        exported = self.bus.store.publish(self.actor["id"], text="Public output", filename="result.html")
        # The inspector does not open the original or execute an HTML export.
        context_file.unlink()
        public = self.request("/api/state")[1]
        selected = next(row for row in public["contexts"] if row["id"] == context["id"])
        self.assertEqual(selected["files"][0]["path"], "selected.txt")
        self.assertEqual(selected["files"][0]["sha256"], hashlib.sha256(b"Public selected content").hexdigest())
        self.assertFalse(selected["content_included"])
        self.assertEqual(selected["freshness"], "not_rechecked")
        artifact = next(row for row in public["artifacts"] if row["id"] == exported["id"])
        self.assertEqual(artifact["root_id"], goal)
        self.assertEqual(artifact["bus_root_id"], dispatch["root_id"])
        self.assertEqual(artifact["filename"], "result.html")
        self.assertFalse(artifact["preview_supported"])
        self.assertNotIn("relative_path", artifact)
        serialized = json.dumps({"contexts": public["contexts"], "artifacts": public["artifacts"]})
        self.assertNotIn("Public selected content", serialized)
        self.assertNotIn("Public output", serialized)
        self.assertNotIn("token", serialized)

    def test_unrelated_roots_unpublished_rows_and_nonroster_artifacts_are_excluded(self):
        goal, dispatch = self.goal()
        allowed = self.bus.store.publish(self.actor["id"], text="allowed", filename="allowed.txt")
        outsider, _ = self.bus.store.register("claude", "Unrelated", [], "native_attached", native_session_id="outsider")
        self.bus.store.update_agent(outsider["id"], active_root=dispatch["root_id"])
        forbidden = self.bus.store.publish(outsider["id"], text="unrelated", filename="foreign.txt")
        with self.bus.store.connection() as db:
            fake = {**allowed, "id": "never-published", "filename": "guess.txt"}
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)", tuple(fake.values()))
            unrelated = self.bus.store._insert_event(db, "message", "user", self.actor["id"], "Unrelated private root", "different-root")
            db.execute("INSERT INTO jobs(id,event_id,agent_id,root_id,state,created_at) VALUES (?,?,?,?,?,?)",
                ("foreign-job", unrelated["id"], self.actor["id"], "different-root", "queued", time.time()))
        state = self.request("/api/state")[1]
        self.assertEqual([row["id"] for row in state["artifacts"]], [allowed["id"]])
        self.assertNotIn(forbidden["id"], json.dumps(state["artifacts"]))
        self.assertNotIn("foreign-job", {row["id"] for row in state["jobs"]})
        self.assertEqual(state["inspector"]["totals"]["jobs"], 2)

    def test_actual_descendant_job_preserves_recipient_root_and_exact_parent(self):
        goal, dispatch = self.goal()
        child = self.bus.store.send(self.actor["id"], self.other["id"], "Please review selected work", "proposal")
        with self.bus.store.connection() as db:
            NativeDispatcher.schema(db)
            event_id, job_id = child["event"]["id"], child["jobs"][0]["id"]
            db.execute("INSERT INTO avalokita_native_peers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("selected-child", goal, dispatch["root_id"], dispatch["event_id"], self.job["event_id"], self.job["id"],
                 self.actor["id"], "own-original", self.other["id"], "other-original", event_id, job_id, "public-hash", time.time()))
        jobs = self.request("/api/state")[1]["jobs"]
        shown = next(row for row in jobs if row["id"] == job_id)
        self.assertEqual(shown["root_id"], goal)
        self.assertEqual(shown["bus_root_id"], dispatch["root_id"])
        self.assertEqual(shown["parent_job_id"], self.job["id"])
        self.assertEqual(shown["parent_event_id"], self.job["event_id"])
        self.assertEqual(shown["sender"], self.actor["id"])
        self.assertEqual(shown["recipient"], self.other["id"])
        self.assertNotIn("Please review", json.dumps(jobs))

    def test_bounds_are_reported_without_leaking_native_errors_or_full_event_metadata(self):
        goal, dispatch = self.goal()
        with self.bus.store.connection() as db:
            now = time.time()
            for number in range(215):
                event = self.bus.store._insert_event(db, "proposal", self.actor["id"], self.other["id"], "selected", dispatch["root_id"],
                    {"authorization": "SECRET-UNRELATED-METADATA", "native_transcript": "PRIVATE"})
                db.execute("INSERT INTO jobs(id,event_id,agent_id,root_id,state,created_at,error) VALUES (?,?,?,?,?,?,?)",
                    ("job-" + str(number), event["id"], self.other["id"], dispatch["root_id"], "failed", now + number, "SECRET-ERROR"))
        state = self.request("/api/state")[1]
        self.assertEqual(len(state["jobs"]), 200)
        self.assertEqual(state["inspector"]["totals"]["jobs"], 217)
        self.assertTrue(state["inspector"]["truncated"]["jobs"])
        self.assertNotIn("SECRET", json.dumps(state["jobs"]))
        self.assertNotIn("PRIVATE", json.dumps(state["jobs"]))
        selected = self.runtime.bridge.selected_outputs(goal_ids=[goal])
        self.assertEqual(len(selected), 200)
        self.assertEqual([row["seq"] for row in selected], sorted(row["seq"] for row in selected))
        self.bus.store.await_native(self.job["id"], "Synthetic accepted input; no provider called")
        self.bus.store.native_result(self.actor["id"], "own-original", "Newest selected result survives the window",
                                    job_id=self.job["id"], event_id=self.job["event_id"])
        newest = self.runtime.bridge.selected_outputs(goal_ids=[goal])[-1]
        self.assertTrue(newest["completion_verified"])
        self.runtime.store.set_goal_state(goal, "stopped")
        self.runtime.store.set_paused(True)
        with self.runtime.store._connection(write=True) as db:
            db.execute("UPDATE goals SET deadline=0,used=budget WHERE id=?", (goal,))
        before_goal = self.runtime.store.get_goal(goal)
        next_state = self.request("/api/state")[1]
        self.assertIn("Newest selected result survives the window", [row["body"] for row in next_state["messages"]])
        self.assertEqual(self.runtime.bridge.selected_outputs(goal_ids=[goal])[-1]["id"], newest["id"])
        self.assertEqual(self.runtime.store.get_goal(goal), before_goal)
        before = [row["id"] for row in next_state["messages"]]
        repeated = self.request("/api/state")[1]
        self.assertEqual(before, [row["id"] for row in repeated["messages"]])

    def test_context_and_lease_are_bound_to_workspace_root_not_query_or_body_path(self):
        goal, _ = self.goal()
        forbidden = {"workspace": str(self.workspace.parent / "b"), "path": "../b", "token": "foreign"}
        self.assertEqual(self.request("/api/state?workspace=foreign")[0], 400)
        self.assertEqual(self.request("/api/summary?root_id=foreign")[0], 400)
        self.assertEqual(self.request("/api/discovery/refresh", forbidden)[0], 400)
        self.assertEqual(self.request("/api/control", {"action": "resume", "workspace": "other"})[0], 400)
        with self.assertRaises(BridgeError):
            self.runtime.peer(self.capability, {"operation": "claim", "root_id": goal, "resources": ["local.txt"], "workspace": "other"})
        with self.assertRaises(Exception):
            self.runtime.peer(self.capability, {"operation": "claim", "root_id": goal, "resources": ["../b/foreign.txt"]})
        with self.assertRaises(Exception):
            self.runtime.peer(self.capability, {"operation": "context", "root_id": goal, "paths": [str(self.workspace.parent / "b/file.txt")]})
        self.assertEqual(self.runtime.store.snapshot()["leases"], [])
        self.assertEqual(self.runtime.store.snapshot()["contexts"], [])

    def test_separate_runtime_tokens_summary_and_saved_roots_never_select_another_workspace(self):
        goal, _ = self.goal()
        b = self.workspace.parent / "b"
        b.mkdir()
        other_runtime = Runtime(b, discovery=False)
        other_server = Server(b, runtime=other_runtime)
        thread = threading.Thread(target=other_server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertEqual(self.request("/api/summary", token=self.server.token, server=other_server)[0], 401)
            status, summary = self.request("/api/summary", server=other_server)
            self.assertEqual(status, 200)
            self.assertEqual(summary["workspace"]["path"], str(b))
            self.assertEqual(summary["counts"]["goals"], 0)
            self.assertNotIn(goal, json.dumps(summary))
            self.assertEqual(self.request("/api/prompt", {"text": "Do not redirect", "idempotency_key": "b", "workspace": str(self.workspace)}, server=other_server)[0], 400)
            self.assertEqual(other_runtime.store.snapshot()["goals"], [])
            self.assertEqual(len(self.runtime.store.snapshot()["goals"]), 1)
        finally:
            other_server.shutdown()
            other_server.server_close()

    def test_failed_bus_scan_is_explicit_not_a_successful_empty_inspector(self):
        self.goal()
        with patch.object(self.runtime.bridge, "_pinned_db", side_effect=BridgeError("Private source error text")):
            summary = self.runtime.summary()
        self.assertEqual(summary["inspection"]["status"], "unavailable")
        self.assertEqual(summary["inspection"]["error_code"], "public_bus_observation_unavailable")
        self.assertNotIn("Private source", json.dumps(summary))

    def test_server_scanner_ownership_does_not_scan_on_constructor_or_state_reads(self):
        calls = []
        scanner = DeviceDiscovery(self.workspace, scanner=lambda _: calls.append(time.monotonic()) or [], interval=60)
        # Use the existing running server only for observations; explicitly own
        # scanner lifecycle here to avoid adding another native reconciler.
        self.runtime.device_discovery = scanner
        self.assertEqual(calls, [])
        scanner.start()
        try:
            deadline = time.monotonic() + 2
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            for _ in range(4):
                self.request("/api/summary")
            self.assertEqual(len(calls), 1)
        finally:
            scanner.stop()

    def test_replaced_directory_retires_old_runtime_without_reading_or_writing_replacement(self):
        self.runtime.draft({"text": "Original A only", "reply": None, "pending": None})
        old_rows = self.rows(self.runtime.store.db_path)
        retired = self.workspace.parent / "retired-a"
        replacement = self.workspace.parent / "replacement-b"
        # Both resolved rename targets are within this test's owned Temp root.
        self.assertEqual(retired.parent.resolve(), Path(self.temp.name).resolve())
        self.assertEqual(replacement.parent.resolve(), Path(self.temp.name).resolve())
        self.workspace.rename(retired)
        self.workspace.mkdir()
        current = Runtime(self.workspace, discovery=False)
        current.draft({"text": "Replacement B only", "reply": None, "pending": None})
        before = {path.name: self.rows(path) for path in (current.store.db_path, current.bridge.receipts)}
        before_draft = current.draft()
        drain = Mock(side_effect=AssertionError("Retired runtime attempted native drain"))
        self.runtime.native_dispatcher = SimpleNamespace(drain=drain, callback_url=None)
        try:
            for path in ("/health", "/api/state", "/api/summary", "/api/team", "/api/draft"):
                self.assertEqual(self.request(path)[0], 403, path)
            for path, payload in (("/api/prompt", {"text": "Must stay retired", "idempotency_key": "retired"}),
                    ("/api/control", {"action": "pause"}), ("/api/draft", {"text": "A only", "reply": None, "pending": None})):
                self.assertEqual(self.request(path, payload)[0], 403, path)
            for operation in (self.runtime.draft, self.runtime.state, self.runtime.summary, self.runtime.drain_native_peers,
                    self.runtime.store.snapshot, lambda: self.runtime.store.new_goal("Retired", idempotency_key="retired-store"),
                    lambda: self.runtime.control("pause"), self.runtime.bridge.team):
                with self.assertRaises((BridgeError, ValueError)) as failed:
                    operation()
                self.assertEqual(getattr(failed.exception, "status", None), 403)
            drain.assert_not_called()
            self.assertEqual(before, {path.name: self.rows(path) for path in (current.store.db_path, current.bridge.receipts)})
            self.assertEqual(current.draft(), before_draft)
            self.assertEqual(current.store.snapshot()["goals"], [])
            self.assertFalse(current.bridge.paused())
            self.assertEqual(self.rows(retired / ".avalokita/private/harmony.sqlite3"), old_rows)
            self.assertEqual(json.loads((retired / ".avalokita/private/draft.json").read_text(encoding="utf-8"))["text"], "Original A only")
        finally:
            # Restore only the QA-owned lexical path so the existing synthetic
            # Sharebus fixture's normal shutdown cannot touch B's namespace.
            self.workspace.rename(replacement)
            retired.rename(self.workspace)


if __name__ == "__main__":
    unittest.main()
