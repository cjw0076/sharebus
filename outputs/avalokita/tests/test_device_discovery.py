"""Process observation tests; never resume or invoke a provider model."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import bridge  # Add only the existing Sharebus module search directory.
import device_discovery as discovery


CLAUDE_IMAGE = str(Path.home() / ".local/bin/claude.exe")


def process(pid=41, *, name="claude.exe", image=CLAUDE_IMAGE, parent=1, started=1000, owner="same_user"):
    return {"pid": pid, "parent_pid": parent, "name": name, "executable": image,
            "started_at": started, "owner_state": owner}


def no_sessions(*_):
    return [], {"state": "unavailable", "observed_at": None, "error_code": None}


def scan(rows, workspace=None):
    return discovery.scan_device(workspace or Path.cwd(), process_reader=lambda: rows,
        resolver=lambda provider: CLAUDE_IMAGE if provider == "claude" else "C:\\installed\\" + provider + ".exe", session_reader=no_sessions)


class DeviceDiscoveryTests(unittest.TestCase):
    def test_codex_chatgpt_image_classification_and_fake_basename(self):
        with patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files"}):
            real = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1016.6573.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
            fake = r"C:\Temp\OpenAI.Codex_26.1016.6573.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
            data = {row["provider"]: row for row in scan([process(name="ChatGPT.exe", image=real), process(42, name="ChatGPT.exe", image=fake)])}
        self.assertTrue(data["codex"]["running"])
        self.assertEqual(data["codex"]["process_count"], 1)
        self.assertFalse(data["codex"]["verified"])
        self.assertFalse(data["chatgpt"]["running"])
        self.assertEqual(data["chatgpt"]["candidate_count"], 1)
        self.assertEqual(data["codex"]["native_sessions"], [])

    def test_real_desktop_paths_group_helpers_without_granting_native_authority(self):
        with patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\human\AppData\Local"}):
            claude = r"C:\Program Files\WindowsApps\Claude_2.42304.2.0_x64__pzs8sxrjxfjjc\app\claude.exe"
            agy = r"C:\Users\human\AppData\Local\Programs\Antigravity\Antigravity.exe"
            data = {row["provider"]: row for row in scan([process(image=claude), process(42, image=claude, parent=41, started=1001),
                                                        process(44, image=agy, name="Antigravity.exe")])}
        self.assertEqual(data["claude"]["app_count"], 1)
        self.assertTrue(data["claude"]["processes"][1]["helper"])
        self.assertTrue(data["antigravity"]["running"])
        self.assertTrue(all(not row["verified"] and row["capabilities"] == [] for row in data.values()))

    def test_owner_denied_or_other_user_is_aggregate_only_never_a_public_candidate(self):
        rows = scan([process(owner="denied"), process(42, owner="other_user")])
        claude = next(row for row in rows if row["provider"] == "claude")
        self.assertFalse(claude["running"])
        self.assertEqual(claude["candidate_count"], 0)
        self.assertEqual(claude["processes"], [])
        self.assertEqual(claude["device_inaccessible_count"], 2)
        cache = discovery.DeviceDiscovery(Path.cwd(), scanner=lambda _: rows)
        cache._scan()
        self.assertEqual(cache.snapshot()[1]["inaccessible_count"], 2)
        self.assertEqual(cache.snapshot()[1]["error_code"], "process_identity_unavailable")

    def test_fake_path_cli_is_never_executed_for_native_metadata(self):
        image = r"C:\Temp\claude.exe"
        with patch.object(discovery.subprocess, "run", side_effect=AssertionError("Untrusted metadata executable ran")):
            rows = discovery.scan_device(Path.cwd(), process_reader=lambda: [process(image=image)], resolver=lambda _: image)
        claude = next(row for row in rows if row["provider"] == "claude")
        self.assertFalse(claude["running"])
        self.assertEqual(claude["candidate_count"], 1)
        self.assertEqual(claude["native_sessions"], [])

    def test_pid_reuse_changes_identity_and_never_groups_newer_parent(self):
        before = next(row for row in scan([process()]) if row["provider"] == "claude")
        after = next(row for row in scan([process(started=2000), process(42, parent=41, started=1500)]) if row["provider"] == "claude")
        self.assertNotEqual(before["processes"][0]["identity"], after["processes"][0]["identity"])
        self.assertFalse(after["processes"][1]["helper"])
        self.assertEqual(after["app_count"], 2)

    def test_public_cache_refresh_lifecycle_exit_and_error_are_distinct(self):
        samples = [scan([process()]), scan([]), discovery.DiscoveryError("process_scan_timeout")]
        def scanner(_):
            value = samples.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        cache = discovery.DeviceDiscovery(Path.cwd(), scanner=scanner)
        self.assertEqual(cache.snapshot()[1]["state"], "pending")
        cache._scan()
        rows, ready = cache.snapshot()
        self.assertTrue(next(row for row in rows if row["provider"] == "claude")["running"])
        rows[0]["provider"] = "mutated-outside-cache"
        self.assertEqual(cache.snapshot()[0][0]["provider"], "codex")
        cache._scan()
        self.assertFalse(next(row for row in cache.snapshot()[0] if row["provider"] == "claude")["running"])
        cache._scan()
        rows, failed = cache.snapshot()
        self.assertEqual(failed["state"], "error")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["error_code"], "process_scan_timeout")
        self.assertIsNotNone(failed["last_success_at"])
        self.assertEqual(ready["state"], "ready")

    def test_no_successful_scan_is_not_reported_as_empty_ready(self):
        cache = discovery.DeviceDiscovery(Path.cwd(), scanner=lambda _: (_ for _ in ()).throw(TimeoutError()))
        cache._scan()
        rows, status = cache.snapshot()
        self.assertEqual(rows, [])
        self.assertEqual(status["state"], "error")
        self.assertFalse(status["stale"])
        self.assertIsNone(status["last_success_at"])

    def test_snapshot_is_cheap_and_server_owned_thread_stops(self):
        calls = []
        cache = discovery.DeviceDiscovery(Path.cwd(), scanner=lambda _: calls.append(True) or scan([]), interval=0.1)
        self.assertEqual(calls, [])
        cache.start()
        deadline = time.monotonic() + 3
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(calls), 2)
        for _ in range(100):
            cache.snapshot()
        cache.stop()
        stopped = len(calls)
        self.assertFalse(cache._thread.is_alive())
        time.sleep(0.15)
        self.assertEqual(len(calls), stopped)
        disabled = discovery.DeviceDiscovery(Path.cwd(), enabled=False, scanner=lambda _: self.fail("Disabled scanner ran"))
        disabled.start()
        disabled.refresh()
        self.assertEqual(disabled.snapshot()[1]["state"], "disabled")

    def test_supported_claude_metadata_only_exact_workspace_and_live_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a", Path(directory) / "b"
            a.mkdir()
            b.mkdir()
            data = [
                {"sessionId": "own", "cwd": str(a), "pid": 41, "kind": "interactive", "status": "idle", "name": "PRIVATE TITLE"},
                {"sessionId": "foreign", "cwd": str(b), "pid": 41},
                {"sessionId": "unowned", "cwd": str(a), "pid": 99},
                {"sessionId": "unknown-cwd", "pid": 41},
            ]
            rows = next(row for row in scan([process()]) if row["provider"] == "claude")["processes"]
            with patch.object(discovery.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(data))) as run:
                sessions, status = discovery._claude_sessions(CLAUDE_IMAGE, a, rows)
            self.assertEqual([row["native_session_id"] for row in sessions], ["own"])
            self.assertEqual(status["state"], "ready")
            self.assertNotIn("PRIVATE TITLE", json.dumps(sessions))
            self.assertEqual(run.call_args.args[0], [CLAUDE_IMAGE, "agents", "--json"])
            self.assertEqual(run.call_args.kwargs["timeout"], discovery.SESSION_TIMEOUT)
            self.assertFalse(sessions[0]["capability_verified"])

    def test_metadata_timeout_preserves_last_good_with_explicit_staleness(self):
        rows = scan([process()])
        claude = next(row for row in rows if row["provider"] == "claude")
        claude["native_sessions"] = [{"native_session_id": "selected-metadata", "capability_verified": False}]
        claude["session_scan"]["state"] = "ready"
        failure = deepcopy(rows)
        failed = next(row for row in failure if row["provider"] == "claude")
        failed["native_sessions"] = []
        failed["session_scan"].update(state="error", error_code="session_scan_timeout")
        samples = iter([rows, failure])
        cache = discovery.DeviceDiscovery(Path.cwd(), scanner=lambda _: next(samples))
        cache._scan()
        cache._scan()
        shown = next(row for row in cache.snapshot()[0] if row["provider"] == "claude")
        self.assertEqual(shown["native_sessions"][0]["native_session_id"], "selected-metadata")
        self.assertTrue(shown["session_scan"]["stale"])
        with patch.object(discovery.subprocess, "run", side_effect=subprocess.TimeoutExpired("claude", 3)):
            sessions, status = discovery._claude_sessions(CLAUDE_IMAGE, Path.cwd(), shown["processes"])
        self.assertEqual(sessions, [])
        self.assertEqual(status["error_code"], "session_scan_timeout")

    @unittest.skipUnless(os.name == "nt", "Windows same-handle observation")
    def test_actual_owned_python_process_start_and_exit(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            rows = discovery._windows_processes({Path(sys.executable).name.casefold()})
            own = next(row for row in rows if row["pid"] == child.pid)
            self.assertEqual(own["owner_state"], "same_user")
            self.assertEqual(own["parent_pid"], os.getpid())
            self.assertIsNotNone(own["executable"])
            self.assertGreater(own["started_at"], time.time() - 30)
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertNotIn(child.pid, {row["pid"] for row in discovery._windows_processes({Path(sys.executable).name.casefold()})})


if __name__ == "__main__":
    unittest.main()
