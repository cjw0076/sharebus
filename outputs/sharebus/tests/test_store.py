from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store import BusError, BusStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-store-")
        self.root = Path(self.directory.name)
        self.store = BusStore(self.root)
        self.a, self.a_token = self.store.register("codex", "CTO", mode="managed_new", role="CTO")
        self.b, self.b_token = self.store.register("claude", "CMO", mode="managed_new", role="CMO")

    def tearDown(self):
        for target in self.root.rglob("*"):
            if target.is_file():
                target.chmod(0o600)
        self.directory.cleanup()

    def test_capability_derives_owner_and_public_manifest_has_no_secret(self):
        self.assertEqual(self.store.authenticate(self.a_token), self.a["id"])
        self.assertEqual(self.store.authenticate(self.store.operator_token), "user")
        with self.assertRaises(BusError):
            self.store.authenticate(self.a["id"])
        manifest = self.root / self.a["folder"] / "manifest.json"
        raw = manifest.read_text(encoding="utf-8")
        self.assertNotIn(self.a_token, raw)
        self.assertNotIn("token_hash", raw)
        self.assertEqual(json.loads(raw)["role"], "CTO")

    def test_human_fanout_jobs_share_root_and_agents_cannot_reset_budget(self):
        initial = self.store.send("user", "all", "Please collaborate", "task", budget=2)
        self.assertEqual(len(initial["jobs"]), 2)
        first = self.store.claim(self.a["id"])
        self.assertEqual(first["remaining_turns"], 1)
        delegated = self.store.send(self.a["id"], self.b["id"], "Investigate this", "question", budget=200)
        self.assertEqual(initial["root_id"], delegated["root_id"])
        second = self.store.claim(self.b["id"])
        self.assertEqual(second["remaining_turns"], 0)
        self.store.finish(second["id"], "completed")
        self.assertIsNone(self.store.claim(self.b["id"]))
        self.assertEqual(self.store.jobs()[0]["state"], "needs_human")
        self.assertEqual(len({job["root_id"] for job in self.store.jobs()}), 1)

    def test_agent_without_active_root_cannot_create_task(self):
        with self.assertRaises(BusError):
            self.store.send(self.a["id"], self.b["id"], "Unbounded task", "task")
        passive = self.store.send(self.a["id"], "all", "Ready", "progress")
        self.assertEqual(passive["jobs"], [])
        self.assertIsNone(passive["root_id"])

    def test_passive_results_do_not_wake(self):
        initial = self.store.send("user", self.a["id"], "Task", "task")
        job = self.store.claim(self.a["id"])
        result = self.store.send(self.a["id"], "all", "Progress", "result")
        self.assertEqual(result["jobs"], [])
        self.store.finish(job["id"], "completed", text="Done")
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.events()["events"][-1]["root_id"], initial["root_id"])

    def test_pause_blocks_claim_and_running_is_serialized(self):
        self.store.send("user", self.a["id"], "One", "task")
        self.store.send("user", self.a["id"], "Two", "task")
        self.store.set_paused(True)
        self.assertIsNone(self.store.claim(self.a["id"]))
        self.store.set_paused(False)
        first = self.store.claim(self.a["id"])
        self.assertIsNone(self.store.claim(self.a["id"]))
        self.store.finish(first["id"], "completed")
        self.assertIsNotNone(self.store.claim(self.a["id"]))

    def test_native_receipt_preserves_human_control_and_completed_job_ownership(self):
        for control in ("paused", "stopped", "global"):
            with self.subTest(control=control):
                agent, _ = self.store.register("hermes", control)
                self.store.send("user", agent["id"], "Native work", "task")
                job = self.store.claim(agent["id"])
                self.store.await_native(job["id"], "Preparing")
                if control == "global":
                    self.store.set_paused(True)
                else:
                    self.store.update_agent(agent["id"], state=control)
                before = self.store.events()["last_seq"]
                with self.assertRaises(BusError):
                    self.store.await_native(job["id"], "Late receipt")
                self.assertEqual(self.store.events()["last_seq"], before)
                self.assertEqual(self.store.agent(agent["id"])["state"], "awaiting_native_reply" if control == "global" else control)
                self.store.set_paused(False)
        self.store.send("user", self.a["id"], "Old input", "task")
        old = self.store.claim(self.a["id"])
        self.store.finish(old["id"], "completed")
        self.store.send("user", self.a["id"], "New input", "task")
        new = self.store.claim(self.a["id"])
        self.store.await_native(old["id"], "Old completed receipt")
        self.assertEqual(self.store.agent(self.a["id"])["active_root"], new["root_id"])
        self.assertEqual(self.store.agent(self.a["id"])["state"], "running")
        self.store.update_agent(self.a["id"], state="paused")
        self.store.await_native(old["id"], "Old receipt after pause")
        self.assertEqual(self.store.agent(self.a["id"])["state"], "paused")

    def test_crash_recovery_preserves_conversation_without_replay(self):
        self.store.update_agent(self.a["id"], native_session_id="native-real-id")
        self.store.send("user", self.a["id"], "Potential side effect", "task")
        self.store.send("user", self.a["id"], "Queued next", "task")
        running = self.store.claim(self.a["id"])
        restarted = BusStore(self.root)
        self.assertEqual(restarted.recover(), 1)
        self.assertEqual(restarted.agent(self.a["id"])["native_session_id"], "native-real-id")
        states = {job["id"]: job["state"] for job in restarted.jobs()}
        self.assertEqual(states[running["id"]], "needs_human")
        self.assertIn("queued", states.values())
        self.assertIsNone(restarted.claim(self.a["id"]))

    def test_provider_multiple_roles_is_ambiguous(self):
        self.store.register("codex", "CEO", mode="external_attached", role="CEO")
        with self.assertRaises(BusError) as caught:
            self.store.send("user", "codex", "Do not guess a role", "task")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store.send("user", self.a["id"], "Exact role", "task")["jobs"]), 1)

    def test_publish_snapshot_and_copy_preserve_ownership(self):
        source = self.root / "chosen.txt"
        source.write_text("initial", encoding="utf-8")
        artifact = self.store.publish(self.a["id"], source="chosen.txt")
        source.write_text("changed", encoding="utf-8")
        _, snapshot = self.store.artifact_file(artifact["id"])
        self.assertEqual(snapshot.read_text(encoding="utf-8"), "initial")
        copied = self.store.publish(self.b["id"], _copy_id=artifact["id"])
        self.assertEqual(copied["owner"], self.b["id"])
        self.assertIn(self.b["id"], copied["relative_path"])
        self.assertEqual(copied["sha256"], artifact["sha256"])
        self.assertEqual(self.store.events()["events"][-1]["metadata"]["copied_from"], artifact["id"])

    def test_artifact_private_traversal_ads_unc_and_names_are_rejected(self):
        for source in ("../private", ".sharebus/private/operator.token", ".SHAREBUS/private/operator.token", ".sharebus./private/operator.token", ".sharebus /private/operator.token", "C:\\outside.txt", "\\\\server\\file", "note.txt:stream", "/absolute", "a/../note.txt", "CON", "file\x00.txt"):
            with self.subTest(source=source), self.assertRaises(BusError):
                self.store.publish(self.a["id"], source=source)
        for filename in ("../escape", "CON", "name.", "a:b", "a\\b"):
            with self.subTest(filename=filename), self.assertRaises(BusError):
                self.store.publish(self.a["id"], text="text", filename=filename)

    def test_symlink_source_is_rejected_when_supported(self):
        source = self.root / "source.txt"
        source.write_text("selected", encoding="utf-8")
        link = self.root / "alias.txt"
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest("Symlink creation needs Windows developer mode or privileges")
        with self.assertRaises(BusError):
            self.store.publish(self.a["id"], source="alias.txt")

    def test_external_ack_cannot_complete_another_agent_job(self):
        external, _ = self.store.register("hermes", "Research", mode="external_attached")
        self.store.send("user", external["id"], "Read", "task")
        inbox = self.store.external_inbox(external["id"])
        self.assertEqual(len(inbox["jobs"]), 1)
        other, _ = self.store.register("cursor", "Other")
        with self.assertRaises(BusError):
            self.store.external_inbox(other["id"], acknowledgements=[inbox["jobs"][0]["id"]])
        self.store.external_inbox(external["id"], acknowledgements=[inbox["jobs"][0]["id"]])
        self.assertEqual(self.store.jobs()[0]["state"], "completed")

    def test_simultaneous_claims_never_run_two_turns_for_one_agent(self):
        self.store.send("user", self.a["id"], "One", "task")
        self.store.send("user", self.a["id"], "Two", "task")
        with ThreadPoolExecutor(max_workers=4) as pool:
            claimed = list(pool.map(lambda _: self.store.claim(self.a["id"]), range(4)))
        self.assertEqual(sum(job is not None for job in claimed), 1)
        self.assertEqual(sum(job["state"] == "running" for job in self.store.jobs()), 1)

    def test_hardlink_cannot_alias_private_input_or_export(self):
        synthetic = self.store.private / "synthetic-secret.txt"
        synthetic.write_text("synthetic private bytes", encoding="utf-8")
        alias = self.root / "ordinary-note.txt"
        try:
            os.link(synthetic, alias)
        except OSError:
            self.skipTest("Filesystem hardlinks unavailable")
        with self.assertRaises(BusError):
            self.store.publish(self.a["id"], source="ordinary-note.txt")
        artifact = self.store.publish(self.a["id"], text="Chosen output", filename="selected.txt")
        _, exported = self.store.artifact_file(artifact["id"])
        export_alias = self.root / "export-alias.txt"
        os.link(exported, export_alias)
        with self.assertRaises(BusError):
            self.store.artifact_file(artifact["id"])

    def test_simultaneous_native_registration_has_one_owner_without_orphans(self):
        def register(index):
            try:
                agent, _ = self.store.register("claude", f"Role {index}", native_session_id="same-native-id")
                return agent["id"]
            except BusError as exc:
                self.assertEqual(exc.status, 409)
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.agents()), 3)
        self.assertEqual(len(list(self.store.credentials.glob("*.token"))), 3)

    @unittest.skipUnless(os.name == "nt", "NTFS junction test")
    def test_ntfs_junction_source_is_rejected_without_symlink_privilege(self):
        selected = self.root / "selected"
        selected.mkdir()
        (selected / "note.txt").write_text("chosen", encoding="utf-8")
        junction = self.root / "junction"
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(junction), str(selected)],
                                capture_output=True, timeout=10)
        if result.returncode:
            self.skipTest("NTFS junction creation unavailable")
        try:
            with self.assertRaises(BusError):
                self.store.publish(self.a["id"], source="junction/note.txt")
        finally:
            os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
