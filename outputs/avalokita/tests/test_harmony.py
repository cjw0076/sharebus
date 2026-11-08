"""Local synthetic kernel evidence; no native delivery or model calls."""
import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("avalokita_harmony_test", Path(__file__).resolve().parents[1] / "harmony.py")
harmony = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harmony)
HarmonyStore, HarmonyError = harmony.HarmonyStore, harmony.HarmonyError


class HarmonyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.store = HarmonyStore(self.workspace)
        for actor in ("a", "b"):
            self.store.register_pane(actor, "test", "existing role", "native-" + actor, ["text"], verified=True)
        self.root = self.store.new_goal("Human selected task", 100)["root_id"]

    def claim(self, actor="a", resources=None, **kwargs):
        return self.store.claim(actor, resources or ["file.txt"], root_id=self.root, **kwargs)

    def test_twenty_concurrent_claims_have_one_winner(self):
        barrier = threading.Barrier(20)

        def attempt(index):
            barrier.wait(timeout=10)
            try:
                return self.claim(actor="a" if index % 2 else "b")
            except HarmonyError as error:
                self.assertEqual(error.status, 409)
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_trusted_completed_observation_after_stop_expiry_does_not_grant_authority(self):
        self.store.set_goal_state(self.root, "stopped")
        self.store.set_paused(True)
        with self.store._connection(write=True) as db:
            db.execute("UPDATE goals SET deadline=0 WHERE id=?", (self.root,))
        before = self.store.get_goal(self.root)
        event = self.store.import_selected_result("a", "Completed selected result", root_id=self.root,
            native_session_id="native-a", source_event_id="exact-event", source_job_id="exact-job", source_created_at=time.time())
        self.assertEqual(event["body"], "Completed selected result")
        self.assertEqual(event["hop_budget"], 0)
        self.assertEqual(event["deadline"], 0)
        from jsonschema import Draft202012Validator, FormatChecker
        schema = json.loads((Path(__file__).resolve().parents[1] / "protocol" / "envelope-v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(event)
        self.assertEqual(self.store.get_goal(self.root), before)
        self.assertEqual(self.store.import_selected_result("a", "Completed selected result", root_id=self.root,
            native_session_id="native-a", source_event_id="exact-event", source_job_id="exact-job", source_created_at=time.time())["id"], event["id"])
        with self.assertRaises(HarmonyError):
            self.store.publish("a", "result", "Forged bypass", root_id=self.root,
                               metadata={"observed_completed_result": True, "source": "sharebus_selected_output"})
        with self.assertRaises(HarmonyError):
            self.store.import_selected_result("a", "Wrong identity", root_id=self.root,
                native_session_id="different-native", source_event_id="different-event", source_job_id="exact-job", source_created_at=time.time())

    def test_scope_overlap_case_symbol_and_sibling(self):
        lease = self.claim(resources=["src/Thing.py::method"])
        for scope in (["SRC/thing.py"], ["src"], ["src/Thing.py::other"]):
            with self.assertRaises(HarmonyError):
                self.claim("b", scope)
        self.claim("b", ["src2/Thing.py"])
        self.store.release("a", lease["lease_id"], lease["fence"])

    def test_expiry_and_handoff_have_monotonic_fences(self):
        first = self.claim(ttl=.01)
        with patch.object(harmony.time, "time", return_value=time.time() + 1):
            second = self.claim("b")
        self.assertGreater(second["fence"], first["fence"])
        with self.assertRaises(HarmonyError):
            self.store.renew("a", first["lease_id"], first["fence"])
        moved = self.store.handoff("b", second["lease_id"], second["fence"], "a")
        self.assertGreater(moved["fence"], second["fence"])
        self.assertEqual(moved["root_id"], self.root)
        for actor, fence in (("b", second["fence"]), ("a", second["fence"])):
            with self.assertRaises(HarmonyError):
                self.store.release(actor, moved["lease_id"], fence)
        self.store.release("a", moved["lease_id"], moved["fence"])

    def test_file_commit_compare_and_swap_and_stale_owner(self):
        lease = self.claim()
        result = self.store.commit_if_current("a", lease["lease_id"], lease["fence"], "file.txt", None, "first")
        self.assertEqual(result["sha256"], hashlib.sha256(b"first").hexdigest())
        with self.assertRaises(HarmonyError):
            self.store.commit_if_current("a", lease["lease_id"], lease["fence"], "file.txt", None, "overwrite")
        moved = self.store.handoff("a", lease["lease_id"], lease["fence"], "b")
        with self.assertRaises(HarmonyError):
            self.store.commit_if_current("a", lease["lease_id"], lease["fence"], "file.txt", result["sha256"], "stale")
        self.store.commit_if_current("b", moved["lease_id"], moved["fence"], "file.txt", result["sha256"], "second")
        self.assertEqual((self.workspace / "file.txt").read_text(), "second")

    def test_commit_holds_database_write_lock_until_replace(self):
        lease = self.claim()
        reached_replace, allow_replace, handoff_started, handoff_done = (threading.Event() for _ in range(4))
        original_replace = os.replace

        def held_replace(source, target):
            reached_replace.set()
            if not allow_replace.wait(10):
                raise RuntimeError("test synchronization timed out")
            return original_replace(source, target)

        def transfer():
            handoff_started.set()
            result = self.store.handoff("a", lease["lease_id"], lease["fence"], "b")
            handoff_done.set()
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            with patch.object(harmony.os, "replace", side_effect=held_replace):
                save = pool.submit(self.store.commit_if_current, "a", lease["lease_id"], lease["fence"], "file.txt", None, "saved")
                try:
                    self.assertTrue(reached_replace.wait(5))
                    moving = pool.submit(transfer)
                    self.assertTrue(handoff_started.wait(5))
                    self.assertFalse(handoff_done.wait(.15), "ownership changed before the protected write")
                finally:
                    allow_replace.set()
                save.result(5)
                moved = moving.result(5)
        self.assertTrue(handoff_done.is_set())
        with self.assertRaises(HarmonyError):
            self.store.commit_if_current("a", lease["lease_id"], lease["fence"], "file.txt", hashlib.sha256(b"saved").hexdigest(), "stale")
        self.assertGreater(moved["fence"], lease["fence"])

    def test_root_binding_pause_deadline_budget_and_stop(self):
        with self.assertRaises(HarmonyError):
            self.store.claim("a", ["unbound.txt"])
        with self.assertRaises(HarmonyError):
            self.store.claim("a", ["unknown.txt"], root_id="peer-minted")
        lease = self.claim()
        (self.workspace / "context.txt").write_text("selected", encoding="utf-8")
        self.store.set_paused(True)
        operations = [
            lambda: self.claim(resources=["other.txt"]),
            lambda: self.store.renew("a", lease["lease_id"], lease["fence"]),
            lambda: self.store.handoff("a", lease["lease_id"], lease["fence"], "b"),
            lambda: self.store.release("a", lease["lease_id"], lease["fence"]),
            lambda: self.store.commit_if_current("a", lease["lease_id"], lease["fence"], "file.txt", None, "blocked"),
            lambda: self.store.publish_context("a", ["context.txt"], root_id=self.root),
            lambda: self.store.publish("a", "status", "paused"),
        ]
        for operation in operations:
            with self.assertRaises(HarmonyError):
                operation()
        self.assertTrue(self.store.snapshot()["paused"])
        self.assertEqual(len(self.store.capture("human")["messages"]), 1)
        self.store.set_paused(False)
        self.store.set_goal_state(self.root, "paused")
        with self.assertRaises(HarmonyError):
            self.store.renew("a", lease["lease_id"], lease["fence"])
        self.store.set_goal_state(self.root, "active")
        self.store.renew("a", lease["lease_id"], lease["fence"])
        short = self.store.new_goal("short", ttl=1)["root_id"]
        with patch.object(harmony.time, "time", return_value=time.time() + 2):
            with self.assertRaises(HarmonyError):
                self.store.claim("a", ["later.txt"], root_id=short)
        tiny = self.store.new_goal("one message", budget=1)["root_id"]
        self.store.publish("a", "message", "used", root_id=tiny)
        with self.assertRaises(HarmonyError):
            self.store.claim("a", ["budget.txt"], root_id=tiny)
        self.store.set_goal_state(self.root, "stopped")
        with self.assertRaises(HarmonyError):
            self.store.set_goal_state(self.root, "active")

    def test_durable_envelope_idempotency_and_capture_cursor(self):
        event = self.store.publish("a", "proposal", "selected result", root_id=self.root,
                                   recipient="b", metadata={"idempotency_key": "once", "correlation_id": "job-1"})
        restarted = HarmonyStore(self.workspace)
        again = restarted.publish("a", "proposal", "selected result", root_id=self.root,
                                  recipient="b", metadata={"idempotency_key": "once", "correlation_id": "job-1"})
        self.assertEqual(event, again)
        self.assertEqual(event["native_session_id"], "native-a")
        self.assertEqual(event["workspace_id"], restarted.workspace_id)
        self.assertEqual(event["correlation_id"], "job-1")
        self.assertEqual(event["hop_budget"], 99)
        self.assertEqual(restarted.snapshot()["goals"][0]["used"], 1)
        with self.assertRaises(HarmonyError):
            restarted.publish("a", "proposal", "different", root_id=self.root, metadata={"idempotency_key": "once"})
        second = restarted.publish("a", "result", "done", root_id=self.root)
        capture = restarted.capture("a", limit=1)
        self.assertTrue(capture["has_more"])
        self.assertEqual(restarted.capture("a", after=capture["next_cursor"])["messages"], [second])
        self.assertEqual(restarted.capture("b")["messages"], [])

    def test_peer_cannot_spoof_identity_or_publish_private_data(self):
        for metadata in ({"actor": "human"}, {"nested": {"reasoning": "private"}}, {"cookie": "value"}):
            with self.assertRaises(HarmonyError):
                self.store.publish("a", "status", "output", metadata=metadata)
        for body in ("Authorization: Bearer SECRET", "-----BEGIN PRIVATE KEY-----", "sk-" + "x" * 30):
            with self.assertRaises(HarmonyError):
                self.store.publish("a", "status", body)
        self.store.register_pane("unverified", "test", "role", "pending", [], verified=False)
        with self.assertRaises(HarmonyError):
            self.store.publish("unverified", "status", "pretend")
        with self.assertRaises(HarmonyError):
            self.store.register_pane("a", "test", "replacement", "different-native", [], verified=True)
        kept = self.store.register_pane("a", "test", "replacement", "native-a", [], verified=True)
        self.assertEqual(kept["role"], "existing role")

    def test_multiline_human_goal_and_exact_root_reads(self):
        root = self.store.new_goal("First line\nSecond line\tselected")
        self.store.publish("a", "message", "different goal", root_id=root["root_id"])
        expected = self.store.publish("a", "message", "original goal", root_id=self.root)
        self.assertEqual(self.store.capture("a", root_id=self.root)["messages"], [expected])
        (self.workspace / "context.txt").write_text("selected", encoding="utf-8")
        context = self.store.publish_context("a", ["context.txt"], root_id=self.root)
        with self.assertRaises(HarmonyError):
            self.store.read_context(context["snapshot_id"], root_id=root["root_id"])

    def test_exact_lookups_and_restart_preserve_identity_and_fences(self):
        lease = self.claim()
        event = self.store.publish("a", "result", "selected", root_id=self.root)
        restarted = HarmonyStore(self.workspace)
        self.assertEqual(restarted.workspace_id, self.store.workspace_id)
        self.assertEqual(restarted.get_goal(self.root)["root_id"], self.root)
        self.assertEqual(restarted.get_lease(lease["lease_id"])["fence"], lease["fence"])
        self.assertEqual(restarted.capture("a")["messages"][0]["sequence"], event["sequence"])
        self.assertEqual(restarted.snapshot()["panes"][0]["native_session_id"], "native-a")
        other = self.store.new_goal("other human task")["root_id"]
        for operation in (
            lambda: restarted.renew("a", lease["lease_id"], lease["fence"], root_id=other),
            lambda: restarted.handoff("a", lease["lease_id"], lease["fence"], "b", root_id=other),
            lambda: restarted.release("a", lease["lease_id"], lease["fence"], root_id=other),
            lambda: restarted.commit_if_current("a", lease["lease_id"], lease["fence"], "file.txt", None, "blocked", root_id=other),
        ):
            with self.assertRaises(HarmonyError):
                operation()
        for lookup in (restarted.get_goal, restarted.get_lease):
            with self.assertRaises(HarmonyError) as error:
                lookup("unknown")
            self.assertEqual(error.exception.status, 404)
        restarted.release("a", lease["lease_id"], lease["fence"], root_id=self.root)
        next_lease = restarted.claim("b", ["file.txt"], root_id=self.root)
        self.assertGreater(next_lease["fence"], lease["fence"])

    def test_envelope_schema_required_properties_match_stored_event(self):
        directory = Path(__file__).resolve().parents[1] / "protocol"
        for path in directory.glob("*.json"):
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        schema = json.loads((directory / "envelope-v1.schema.json").read_text(encoding="utf-8"))
        event = self.store.publish("a", "proposal", "selected", root_id=self.root)
        self.assertEqual(set(schema["required"]), set(event))
        for metadata in ({"idempotency_key": []}, {"correlation_id": {"spoof": True}}):
            with self.assertRaises(HarmonyError):
                self.store.publish("a", "status", "output", metadata=metadata)

    def test_exact_authority_lookup_is_independent_of_ui_pagination(self):
        oldest = self.claim()
        for index in range(51):
            self.store.new_goal("History goal " + str(index))
        for index in range(201):
            self.claim(resources=["history-" + str(index) + ".txt"])
        snapshot = self.store.snapshot()
        self.assertNotIn(self.root, {goal["root_id"] for goal in snapshot["goals"]})
        self.assertNotIn(oldest["lease_id"], {lease["lease_id"] for lease in snapshot["leases"]})
        self.assertEqual(self.store.get_goal(self.root)["state"], "active")
        self.assertEqual(self.store.get_lease(oldest["lease_id"])["root_id"], self.root)

    def test_selected_context_hash_staleness_generation_and_limits(self):
        path = self.workspace / "context.txt"
        path.write_text("context one", encoding="utf-8")
        context = self.store.publish_context("a", ["context.txt"], root_id=self.root)
        self.assertTrue(context["fresh"])
        self.assertTrue(context["is_latest"])
        self.assertEqual(context["root_id"], self.root)
        second = self.store.publish_context("b", ["context.txt"], root_id=self.root)
        self.assertGreater(second["generation"], context["generation"])
        self.assertFalse(self.store.read_context(context["snapshot_id"])["is_latest"])
        path.write_text("context two", encoding="utf-8")
        stale = self.store.read_context(context["snapshot_id"])
        self.assertTrue(stale["requires_refresh"])
        self.assertEqual(stale["stale_paths"], ["context.txt"])
        self.assertEqual(stale["files"][0]["content"], "context one")
        path.write_bytes(b"x" * (harmony.MAX_CONTEXT_FILE_BYTES + 1))
        with self.assertRaises(HarmonyError):
            self.store.publish_context("a", ["context.txt"], root_id=self.root)
        with self.assertRaises(HarmonyError):
            self.store.publish_context("a", ["context.txt"])

    def test_traversal_secret_aliases_and_hardlinks_rejected(self):
        outside = self.workspace.parent / "outside.txt"
        for name in ("../outside.txt", str(outside), ".env", ".env.local", "src/.git/config",
                     "tokens.json", "secret.key", "file.txt:stream", "file.txt. ", "CON"):
            with self.subTest(name=name), self.assertRaises(HarmonyError):
                self.claim(resources=[name])
        original = self.workspace / "original.txt"
        original.write_text("content", encoding="utf-8")
        alias = self.workspace / "alias.txt"
        os.link(original, alias)
        with self.assertRaises(HarmonyError):
            self.claim(resources=["alias.txt"])
        with self.assertRaises(HarmonyError):
            self.store.publish_context("a", ["original.txt"], root_id=self.root)

    def test_symlink_and_windows_junction_rejected(self):
        target = self.workspace / "real"
        target.mkdir()
        link = self.workspace / "alias"
        if os.name == "nt":
            import subprocess
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        else:
            link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(HarmonyError):
            self.claim(resources=["alias/file.txt"])

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 aliases")
    def test_windows_short_aliases_share_lease_keys_and_deny_private_context(self):
        import ctypes

        def short_name(path):
            buffer = ctypes.create_unicode_buffer(32768)
            size = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
            self.assertGreater(size, 0, "GetShortPathNameW failed")
            return Path(buffer.value).name

        filename = "ArchitectureLongFilename.txt"
        path = self.workspace / filename
        path.write_text("selected", encoding="utf-8")
        alias = short_name(path)
        if alias.casefold() == filename.casefold():
            self.skipTest("8.3 name generation is disabled for temporary volume")
        self.assertEqual(self.store._path(alias)[1], self.store._path(filename)[1])
        self.claim(resources=[filename])
        with self.assertRaises(HarmonyError):
            self.claim("b", [alias])
        folder = self.workspace / "LongDirectoryForShortAlias"
        folder.mkdir()
        folder_alias = short_name(folder)
        self.assertEqual(self.store._path(folder_alias + "/new/child.txt")[1],
                         self.store._path(folder.name + "/new/child.txt")[1])
        self.claim(resources=[folder.name])
        with self.assertRaises(HarmonyError):
            self.claim("b", [folder_alias + "/new/child.txt"])
        for private_name in (".avalokita", "credentials", ".env.production"):
            private_path = self.workspace / private_name
            if private_name == ".env.production":
                private_path.write_text("not publishable", encoding="utf-8")
                selected = short_name(private_path)
            else:
                private_path.mkdir(exist_ok=True)
                (private_path / "private.txt").write_text("not publishable", encoding="utf-8")
                selected = short_name(private_path) + "/private.txt"
            with self.subTest(private=private_name), self.assertRaises(HarmonyError):
                self.store.publish_context("a", [selected], root_id=self.root)


if __name__ == "__main__":
    unittest.main()
