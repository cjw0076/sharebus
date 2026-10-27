from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import daemon
from daemon import BusHTTPServer
from store import BusError, BusStore


class FakeAdapter:
    starts = []
    prompts = []
    instance_count = 0

    def __init__(self, provider, workspace, agent_id, token_file, on_event=None, *, subscription_only=False):
        self.subscription_only = subscription_only
        self.id = agent_id
        self.on_event = on_event
        self.session_id = None
        self.pid = 123
        type(self).instance_count += 1

    def start(self, resume_id=None):
        self.session_id = resume_id or "native-" + self.id
        type(self).starts.append(resume_id)
        return self.session_id

    def turn(self, prompt, timeout=240):
        type(self).prompts.append(prompt)
        return SimpleNamespace(text="Native reply", session_id=self.session_id, success=True, needs_human=False, error=None)

    def interrupt(self):
        return None

    def close(self):
        return None


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-http-")
        self.root = Path(self.directory.name)
        self.server = BusHTTPServer(self.root, providers="none", enable_native=False, discovery=[])
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.token = self.server.store.operator_token

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.close()
        for target in self.root.rglob("*"):
            if target.is_file():
                target.chmod(0o600)
        self.directory.cleanup()

    def request(self, path, body=None, token="default", headers=None):
        prepared = {"X-Sharebus-Request": "1"}
        if token == "default":
            token = self.token
        if token:
            prepared["Authorization"] = "Bearer " + token
        payload = None
        if body is not None:
            prepared["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        prepared.update(headers or {})
        request = Request(self.server.url + path, data=payload, headers=prepared)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response), response.headers
        except HTTPError as exc:
            return exc.code, json.load(exc), exc.headers

    def test_data_require_auth_and_secrets_never_in_state(self):
        self.assertEqual(self.request("/api/state", token=None)[0], 401)
        self.assertEqual(self.request("/api/events?token=" + self.token, token=None)[0], 401)
        status, state, _ = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(self.token, json.dumps(state))
        self.assertEqual(state["workspace"]["path"], str(self.root.resolve()))

    def test_host_origin_csrf_are_rejected(self):
        body = {"to": "user", "body": "hello", "kind": "task"}
        self.assertEqual(self.request("/api/message", body, headers={"Origin": "https://hostile.example"})[0], 403)
        self.assertEqual(self.request("/api/message", body, headers={"X-Sharebus-Request": "0"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("/api/message", body, headers={"Content-Type": "text/plain"})[0], 415)

    def test_cookie_exchange_and_role_attachment(self):
        code, _, headers = self.request("/api/auth", {"token": self.token}, token=None)
        self.assertEqual(code, 200)
        cookie = headers.get("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual(self.request("/api/state", token=None, headers={"Cookie": cookie})[0], 200)
        code, registered, _ = self.request("/api/agent/register", {"provider": "codex", "label": "CEO", "role": "CEO", "native_session_id": "gui-session"})
        self.assertEqual(code, 200)
        self.assertEqual(registered["agent"]["mode"], "external_attached")
        state = self.request("/api/state", token=registered["token"])[1]
        self.assertEqual(state["own_agent"]["role"], "CEO")
        self.assertFalse(state["own_agent"]["wake_supported"])

    def test_actor_cannot_spoof_human_or_control(self):
        a = self.request("/api/agent/register", {"provider": "codex", "label": "A"})[1]
        b = self.request("/api/agent/register", {"provider": "claude", "label": "B"})[1]
        self.assertEqual(self.request("/api/control", {"action": "pause"}, token=a["token"])[0], 403)
        self.assertEqual(self.request("/api/message", {"to": b["agent"]["id"], "body": "task", "kind": "task", "sender": "user"}, token=a["token"])[0], 409)
        human = self.request("/api/message", {"to": a["agent"]["id"], "body": "Work", "kind": "task"})[1]
        self.request("/api/agent/inbox", {}, token=a["token"])
        code, delegated, _ = self.request("/api/message", {"to": b["agent"]["id"], "body": "Help", "kind": "task", "sender": "user", "root_id": "fake"}, token=a["token"])
        self.assertEqual(code, 200)
        self.assertEqual(delegated["event"]["sender"], a["agent"]["id"])
        self.assertEqual(delegated["root_id"], human["root_id"])

    def test_exports_copy_into_authenticated_owner_namespace(self):
        a = self.request("/api/agent/register", {"provider": "codex", "label": "A"})[1]
        b = self.request("/api/agent/register", {"provider": "claude", "label": "B"})[1]
        code, result, _ = self.request("/api/artifact/publish", {"text": "Chosen output", "filename": "memo.txt", "owner": b["agent"]["id"]}, token=a["token"])
        self.assertEqual(code, 200)
        self.assertEqual(result["artifact"]["owner"], a["agent"]["id"])
        copied = self.request("/api/artifact/copy", {"artifact_id": result["artifact"]["id"]}, token=b["token"])[1]
        self.assertEqual(copied["artifact"]["owner"], b["agent"]["id"])
        self.assertEqual(self.request("/api/artifact/publish", {"source": ".sharebus/private/operator.token"}, token=a["token"])[0], 400)
        self.assertEqual(len(self.request("/api/artifacts")[1]["artifacts"]), 2)

    def test_managed_persistent_instance_resumes_id_after_restart(self):
        FakeAdapter.starts = []
        FakeAdapter.prompts = []
        FakeAdapter.instance_count = 0
        self.server.supervisor.adapter_factory = FakeAdapter
        agent, _ = self.server.store.register("codex", "Managed", mode="managed_new")
        self.server.supervisor.start()
        for body in ("First turn", "Second turn"):
            self.request("/api/message", {"to": agent["id"], "body": body, "kind": "task"})
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if len(FakeAdapter.prompts) >= (1 if body == "First turn" else 2) and all(job["state"] == "completed" for job in self.server.store.jobs()):
                    break
                time.sleep(0.02)
            else:
                self.fail("Synthetic native job did not complete")
        native = self.server.store.agent(agent["id"])["native_session_id"]
        self.assertEqual(FakeAdapter.instance_count, 1)
        self.assertTrue(self.server.supervisor._adapters[agent["id"]].subscription_only)
        self.assertEqual(FakeAdapter.starts, [None])
        self.assertEqual(len(FakeAdapter.prompts), 2)
        self.server.supervisor.restart(agent["id"])
        self.request("/api/message", {"to": agent["id"], "body": "After native restart", "kind": "task"})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(FakeAdapter.prompts) < 3:
            time.sleep(0.02)
        self.assertEqual(FakeAdapter.starts, [None, native])
        self.assertEqual(self.server.store.agent(agent["id"])["native_session_id"], native)

    def test_per_agent_pause_stop_do_not_shut_down_workspace(self):
        a = self.request("/api/agent/register", {"provider": "codex", "label": "A"})[1]["agent"]
        b = self.request("/api/agent/register", {"provider": "claude", "label": "B"})[1]["agent"]
        self.request("/api/control", {"action": "pause", "agent": a["id"]})
        state = self.request("/api/state")[1]
        self.assertFalse(state["paused"])
        self.assertEqual(self.server.store.agent(a["id"])["state"], "paused")
        self.request("/api/control", {"action": "stop", "agent": a["id"]})
        self.assertEqual(self.request("/api/state")[0], 200)
        self.assertEqual(self.server.store.agent(a["id"])["state"], "stopped")
        self.assertEqual(self.server.store.agent(b["id"])["state"], "idle")

    def test_native_input_and_stop_need_exact_owned_correlation(self):
        attached = self.request("/api/agent/register", {"provider": "claude", "role": "CEO", "native_session_id": "existing-native"})[1]
        duplicate = self.request("/api/agent/register", {"provider": "claude", "native_session_id": "existing-native", "role": "IMPERSONATOR"})
        self.assertEqual(duplicate[0], 409)
        self.assertNotIn(attached["token"], json.dumps(duplicate[1]))
        bound = self.request("/api/agent/register", {"native_session_id": "existing-native"}, token=attached["token"])[1]
        self.assertEqual(bound["agent"]["id"], attached["agent"]["id"])
        self.assertEqual(bound["agent"]["role"], "CEO")
        code, origin, _ = self.request("/api/native/input", {"provider": "claude", "native_session_id": "existing-native", "body": "Human entered this in the native app"})
        self.assertEqual(code, 200)
        self.assertEqual(self.server.store.agent(attached["agent"]["id"])["active_root"], origin["root_id"])
        without = self.request("/api/native/result", {"native_session_id": "existing-native", "last_assistant_message": "Uncorrelated output"}, token=attached["token"])[1]
        self.assertFalse(without["completed"])
        wrong = self.request("/api/native/result", {"native_session_id": "existing-native", "last_assistant_message": "Wrong output", "event_id": "wrong", "job_id": origin["origin_job_id"]}, token=attached["token"])
        self.assertEqual(wrong[0], 409)
        done = self.request("/api/native/result", {"native_session_id": "existing-native", "last_assistant_message": "Correlated output", "event_id": origin["event"]["id"], "job_id": origin["origin_job_id"]}, token=attached["token"])[1]
        self.assertTrue(done["completed"])
        self.assertEqual(self.server.store.jobs()[0]["state"], "completed")

    def test_attachment_cannot_claim_another_workspace(self):
        response = self.request("/api/agent/register", {"provider": "claude", "native_session_id": "native", "workspace": str(self.root.parent)})
        self.assertEqual(response[0], 403)

    def test_heartbeat_cannot_undo_human_pause_stop_or_native_identity(self):
        attached = self.request("/api/agent/register", {"provider": "claude", "native_session_id": "native-owner"})[1]
        agent_id = attached["agent"]["id"]
        self.request("/api/message", {"to": agent_id, "body": "Queued human task", "kind": "task"})
        for action, expected in (("pause", "paused"), ("stop", "stopped")):
            self.request("/api/control", {"action": action, "agent": agent_id})
            code, result, _ = self.request("/api/agent/heartbeat", {"state": "idle"}, token=attached["token"])
            self.assertEqual(code, 200)
            self.assertEqual(result["agent"]["state"], expected)
            inbox = self.request("/api/agent/inbox", {}, token=attached["token"])[1]
            self.assertEqual(inbox["jobs"], [])
        self.assertEqual(self.request("/api/agent/heartbeat", {"native_session_id": "other"}, token=attached["token"])[0], 403)
        self.assertEqual(self.request("/api/agent/register", {"native_session_id": "other"}, token=attached["token"])[0], 403)

    def test_restart_checks_active_job_beyond_public_recent_window(self):
        attached = self.request("/api/agent/register", {"provider": "claude"})[1]
        agent_id = attached["agent"]["id"]
        self.server.store.send("user", agent_id, "Old running work", "task")
        running = self.server.store.claim(agent_id)
        for index in range(101):
            self.server.store.send("user", agent_id, f"New queued work {index}", "task")
        self.assertNotIn(running["id"], {job["id"] for job in self.server.store.jobs()})
        with self.assertRaises(BusError):
            self.server.supervisor.restart(agent_id)
        self.assertEqual(self.server.store.agent(agent_id)["state"], "running")

    def test_uncorrelated_native_stop_can_require_human_without_claiming_completion(self):
        attached = self.request("/api/agent/register", {"provider": "claude", "native_session_id": "native-review"})[1]
        origin = self.request("/api/native/input", {"provider": "claude", "native_session_id": "native-review", "body": "Human input"})[1]
        response = self.request("/api/native/result", {"native_session_id": "native-review", "last_assistant_message": "Output without input identity", "requires_review_if_uncorrelated": True}, token=attached["token"])[1]
        self.assertFalse(response["completed"])
        self.assertTrue(response["needs_human"])
        self.assertEqual(self.server.store.agent(attached["agent"]["id"])["state"], "needs_human")
        self.assertEqual(next(job for job in self.server.store.jobs() if job["id"] == origin["origin_job_id"])["state"], "needs_human")

    def test_human_recovery_is_single_use_provider_bound_and_secret_free_in_public_data(self):
        attached = self.request("/api/agent/register", {"provider": "claude", "role": "CEO", "native_session_id": "native-recover"})[1]
        issued = self.request("/api/agent/recovery", {"agent_id": attached["agent"]["id"]})[1]
        self.assertEqual(self.request("/api/agent/recovery", {"agent_id": attached["agent"]["id"]}, token=attached["token"])[0], 403)
        code = issued["recovery_code"]
        self.assertNotIn(code, json.dumps(self.request("/api/state")[1]))
        self.assertNotIn(code, json.dumps(self.request("/api/events")[1]))
        self.assertEqual(self.request("/api/agent/register", {"provider": "codex", "recovery_code": code})[0], 403)
        self.assertEqual(self.request("/api/agent/register", {"provider": "claude", "native_session_id": "other", "recovery_code": code})[0], 403)
        response = self.request("/api/agent/register", {"provider": "claude", "native_session_id": "native-recover", "role": "IMPERSONATOR", "recovery_code": code})[1]
        self.assertEqual(response["token"], attached["token"])
        self.assertEqual(response["agent"]["role"], "CEO")
        self.assertEqual(self.request("/api/agent/register", {"provider": "claude", "recovery_code": code})[0], 403)

    def test_expired_recovery_cannot_restore_a_native_credential(self):
        attached = self.request("/api/agent/register", {"provider": "claude", "native_session_id": "native-expired"})[1]
        issued = self.request("/api/agent/recovery", {"agent_id": attached["agent"]["id"]})[1]
        with self.server.store.connection() as db:
            db.execute("UPDATE recovery_codes SET expires_at=0")
        self.assertEqual(self.request("/api/agent/register", {"provider": "claude", "recovery_code": issued["recovery_code"]})[0], 403)

    def test_native_question_waits_for_real_human_answer_then_continues_same_agent(self):
        native = self.request("/api/agent/register", {"provider": "antigravity", "native_session_id": "existing-native"})[1]
        agent_id = native["agent"]["id"]
        self.server.store.update_agent(agent_id, mode="native_attached")
        first = self.request("/api/message", {"to": agent_id, "body": "First input", "kind": "task"})[1]
        old = self.server.store.claim(agent_id)
        self.server.store.await_native(old["id"], "Delivered")
        second = self.request("/api/message", {"to": agent_id, "body": "Second queued input", "kind": "task"})[1]
        question = self.request("/api/message", {"to": "user", "body": "Which option?", "kind": "question"}, token=native["token"])[1]["event"]
        self.assertEqual(question["metadata"]["waiting_job_id"], old["id"])
        question = self.request("/api/message", {"to": "user", "body": "And which audience?", "kind": "question"}, token=native["token"])[1]["event"]
        self.assertEqual(question["metadata"]["waiting_job_id"], old["id"])
        self.assertEqual(self.server.store.agent(agent_id)["state"], "waiting_for_user")
        self.assertIsNone(self.server.store.claim(agent_id))
        self.request("/api/agent/heartbeat", {"state": "idle"}, token=native["token"])
        self.assertEqual(self.server.store.agent(agent_id)["state"], "waiting_for_user")
        peer = self.request("/api/agent/register", {"provider": "claude"})[1]
        self.server.store.send("user", peer["agent"]["id"], "Peer work", "task")
        self.server.store.claim(peer["agent"]["id"])
        self.assertEqual(self.request("/api/message", {"to": agent_id, "body": "Peer suggestion", "kind": "answer", "in_reply_to": question["id"]}, token=peer["token"])[0], 200)
        self.assertIsNone(self.server.store.claim(agent_id))
        for target, reference in ((peer["agent"]["id"], question["id"]), (agent_id, first["event"]["id"])):
            self.assertEqual(self.request("/api/message", {"to": target, "body": "Wrong answer binding", "kind": "answer", "in_reply_to": reference})[0], 409)
        answer = self.request("/api/message", {"to": agent_id, "body": "Use option A", "kind": "answer", "in_reply_to": question["id"]})[1]
        continuation = self.server.store.claim(agent_id)
        self.assertEqual(continuation["event_id"], answer["event"]["id"])
        self.assertNotEqual(continuation["root_id"], old["root_id"])
        late = self.request("/api/native/result", {"native_session_id": "existing-native", "last_assistant_message": "Old late result", "job_id": old["id"], "event_id": old["event_id"]}, token=native["token"])
        self.assertEqual(late[0], 409)
        self.assertEqual(self.server.store.agent(agent_id)["active_root"], continuation["root_id"])
        self.server.store.await_native(old["id"], "Late delivery receipt")
        self.server.store.await_native(continuation["id"], "Human answer delivered")
        done = self.request("/api/native/result", {"native_session_id": "existing-native", "last_assistant_message": "Finished after human answer", "job_id": continuation["id"], "event_id": continuation["event_id"]}, token=native["token"])[1]
        self.assertTrue(done["completed"])
        self.assertEqual(self.server.store.agent(agent_id)["native_session_id"], "existing-native")
        self.assertEqual(self.server.store.claim(agent_id)["event_id"], second["event"]["id"])
        states = {job["id"]: job["state"] for job in self.server.store.jobs()}
        self.assertEqual(states[old["id"]], "input_received")

    def test_native_result_respects_pause_and_stop_until_explicit_human_resume(self):
        for action in ("pause", "stop", "global"):
            with self.subTest(action=action):
                native_id = "native-" + action
                attached = self.request("/api/agent/register", {"provider": "claude", "native_session_id": native_id})[1]
                agent_id = attached["agent"]["id"]
                self.server.store.send("user", agent_id, "Work", "task")
                job = self.server.store.claim(agent_id)
                self.server.store.await_native(job["id"], "Delivered")
                control = {"action": "pause" if action == "global" else action, "agent": "all" if action == "global" else agent_id}
                self.request("/api/control", control)
                payload = {"native_session_id": native_id, "last_assistant_message": "Actual result", "job_id": job["id"], "event_id": job["event_id"]}
                self.assertEqual(self.request("/api/native/result", payload, token=attached["token"])[0], 409)
                self.assertEqual(next(item for item in self.server.store.jobs() if item["id"] == job["id"])["state"], "awaiting_native_reply")
                self.request("/api/control", {"action": "resume", "agent": control["agent"]})
                self.assertTrue(self.request("/api/native/result", payload, token=attached["token"])[1]["completed"])

    def test_bound_registration_validates_metadata_shapes(self):
        attached = self.request("/api/agent/register", {"provider": "claude"})[1]
        for invalid in ({"capabilities": {"unexpected": "object"}}, {"capabilities": [17]}, {"label": 17}, {"role": "x" * 81}):
            self.assertEqual(self.request("/api/agent/register", invalid, token=attached["token"])[0], 400)

    def test_native_preparing_race_never_runs_turn_after_human_pause(self):
        agent, _ = self.server.store.register("antigravity", "Attached", mode="native_attached", native_session_id="native-pause")
        self.server.store.send("user", agent["id"], "Work", "task")
        job = self.server.store.claim(agent["id"])
        self.server.supervisor.adapter_factory = FakeAdapter
        original = self.server.store.await_native
        before = len(FakeAdapter.prompts)
        def pause_before_prepare(*args, **kwargs):
            self.server.store.update_agent(agent["id"], state="paused")
            self.server.supervisor.interrupt(agent["id"])
            return original(*args, **kwargs)
        with patch.object(self.server.store, "await_native", pause_before_prepare):
            self.server.supervisor._run(agent["id"], job)
        self.assertEqual(len(FakeAdapter.prompts), before)
        self.assertEqual(self.server.store.agent(agent["id"])["state"], "paused")

    def test_discovery_does_not_block_http_readiness(self):
        entered, release = threading.Event(), threading.Event()
        def slow_discovery(workspace):
            entered.set()
            release.wait(15)
            return []
        other_root = self.root / "other-room"
        other_root.mkdir()
        other = None
        try:
            with patch("discovery.discover", slow_discovery):
                other = BusHTTPServer(other_root, enable_native=False)
                other_thread = threading.Thread(target=other.serve_forever, daemon=True)
                other_thread.start()
                self.assertTrue(entered.wait(5))
                with urlopen(other.url + "/health", timeout=3) as response:
                    self.assertTrue(json.load(response)["ok"])
                self.assertEqual(other.state()["discovery_state"], "pending")
                release.set()
        finally:
            release.set()
            if other is not None:
                other.shutdown()
                other_thread.join(timeout=2)
                other.close()

    @unittest.skipUnless(os.name == "nt", "NTFS junction static confinement")
    def test_static_directory_junction_and_index_link_cannot_expose_private_files(self):
        web = self.root / "web"
        assets = web / "assets"
        assets.mkdir(parents=True)
        self.server.web = web
        (web / "index.html").write_text("Public room", encoding="utf-8")
        junction = assets / "selected"
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(junction), str(self.server.store.private)], capture_output=True, timeout=10)
        if result.returncode:
            self.skipTest("NTFS junction creation unavailable")
        try:
            response = self.request("/assets/selected/operator.token", token=None)
            self.assertEqual(response[0], 404)
            self.assertNotIn(self.token, json.dumps(response[1]))
            (web / "index.html").unlink()
            try:
                (web / "index.html").symlink_to(self.server.store.private / "operator.token")
            except OSError:
                return
            self.assertEqual(self.request("/", token=None)[0], 404)
        finally:
            os.rmdir(junction)


class DaemonGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-guard-")
        self.root = Path(self.directory.name).resolve()
        self.store = BusStore(self.root)
        self.children = []

    def tearDown(self):
        # These are only Python fixture processes created by this test class.
        for process in self.children:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)
        self.directory.cleanup()

    def metadata(self, name, value):
        path = self.store.private / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path.read_bytes()

    def run_main(self, server):
        with patch.object(sys, "argv", ["daemon.py", "--workspace", str(self.root), "--agents", "none"]), patch.object(daemon, "BusHTTPServer", return_value=server) as factory:
            daemon.main()
        return factory

    def fake_server(self):
        nonce = "new-server-nonce-1234567890"
        return SimpleNamespace(nonce=nonce, close=lambda: None, serve_forever=lambda **_: None,
                               runtime=lambda: {"workspace": str(self.root), "workspace_id": self.store.workspace_id,
                                                "url": "http://127.0.0.1:1234", "pid": os.getpid(), "nonce": nonce})

    def test_reused_live_pid_is_not_an_owner_and_stale_bytes_are_archived(self):
        old_lock = self.metadata("daemon.lock", {"pid": os.getpid(), "nonce": "old-lock-nonce"})
        old_runtime = self.metadata("runtime.json", {"workspace": str(self.root), "workspace_id": self.store.workspace_id,
                                                    "url": "http://127.0.0.1:1234", "pid": os.getpid(),
                                                    "nonce": "old-runtime-nonce-1234567890"})
        with patch.object(daemon, "_pid_exists", side_effect=AssertionError("PID presence is not ownership")), patch.object(daemon, "HTTPConnection") as connection:
            connection.return_value.request.side_effect = ConnectionRefusedError
            factory = self.run_main(self.fake_server())
        factory.assert_called_once()
        history = self.store.private / "daemon-history"
        self.assertIn(old_lock, [path.read_bytes() for path in history.glob("*-daemon.lock")])
        self.assertIn(old_runtime, [path.read_bytes() for path in history.glob("*-runtime.json")])
        self.assertFalse((self.store.private / "daemon.lock").exists())
        self.assertFalse((self.store.private / "runtime.json").exists())
        self.assertEqual((self.store.private / "daemon.guard").read_bytes(), b"0")
        self.assertIn("guard_acquired", (self.store.private / "startup.log").read_text())

    def test_os_lease_prevents_duplicate_start_and_is_reusable_after_release(self):
        with daemon.workspace_guard(self.store):
            with patch.object(sys, "argv", ["daemon.py", "--workspace", str(self.root)]), patch.object(daemon, "BusHTTPServer") as factory, self.assertRaises(BusError):
                daemon.main()
            factory.assert_not_called()
        with daemon.workspace_guard(self.store):
            self.assertTrue((self.store.private / "daemon.guard").is_file())
        self.assertIn("broker failure: BusError", (self.store.private / "daemon.log").read_text())

    def test_verified_legacy_http_owner_preserved_despite_different_old_lock_nonce(self):
        server = BusHTTPServer(self.root, providers="none", enable_native=False, discovery=[])
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        lock = self.metadata("daemon.lock", {"pid": os.getpid(), "nonce": "separate-legacy-lock-nonce"})
        runtime = self.metadata("runtime.json", server.runtime())
        try:
            with patch.object(sys, "argv", ["daemon.py", "--workspace", str(self.root)]), patch.object(daemon, "BusHTTPServer") as factory, patch.object(daemon, "_pid_exists", side_effect=AssertionError("No PID ownership inference")), self.assertRaises(BusError):
                daemon.main()
            factory.assert_not_called()
            self.assertEqual((self.store.private / "daemon.lock").read_bytes(), lock)
            self.assertEqual((self.store.private / "runtime.json").read_bytes(), runtime)
            self.assertFalse((self.store.private / "daemon-history").exists())
        finally:
            server.shutdown()
            thread.join(2)
            server.close()

    def test_legacy_wrong_nonce_or_workspace_is_not_adopted_and_never_gets_a_token(self):
        runtime = {"workspace": str(self.root), "workspace_id": self.store.workspace_id,
                   "url": "http://127.0.0.1:1234", "pid": os.getpid(), "nonce": "actual-private-nonce-1234567890"}
        self.metadata("runtime.json", runtime)
        for changed in ({"workspace_id": "foreign-workspace"}, {"pid": os.getpid() + 1}):
            health = {"ok": True, **runtime, **changed}
            with patch.object(daemon, "HTTPConnection") as connection:
                connection.return_value.getresponse.return_value.read.return_value = json.dumps(health).encode()
                self.assertFalse(daemon._legacy_owner_alive(self.store))
                connection.return_value.request.assert_called_once_with("GET", "/health")

    def test_same_workspace_live_pid_with_changed_nonce_is_not_replaced(self):
        runtime = self.metadata("runtime.json", {"workspace": str(self.root), "workspace_id": self.store.workspace_id,
                                                "url": "http://127.0.0.1:1234", "pid": os.getpid(),
                                                "nonce": "stored-private-nonce-1234567890"})
        health = {"ok": True, "workspace_id": self.store.workspace_id, "pid": os.getpid(), "nonce": "different-private-nonce-1234567890"}
        with patch.object(sys, "argv", ["daemon.py", "--workspace", str(self.root)]), patch.object(daemon, "BusHTTPServer") as factory, patch.object(daemon, "HTTPConnection") as connection, self.assertRaises(BusError):
            connection.return_value.getresponse.return_value.read.return_value = json.dumps(health).encode()
            daemon.main()
        factory.assert_not_called()
        self.assertEqual((self.store.private / "runtime.json").read_bytes(), runtime)
        self.assertFalse((self.store.private / "daemon-history").exists())

    def test_uncertain_legacy_listener_timeout_does_not_replace_metadata(self):
        runtime = self.metadata("runtime.json", {"workspace": str(self.root), "workspace_id": self.store.workspace_id,
                                                "url": "http://127.0.0.1:1234", "pid": os.getpid(),
                                                "nonce": "actual-private-nonce-1234567890"})
        with patch.object(sys, "argv", ["daemon.py", "--workspace", str(self.root)]), patch.object(daemon, "BusHTTPServer") as factory, patch.object(daemon, "HTTPConnection") as connection, self.assertRaises(BusError):
            connection.return_value.request.side_effect = TimeoutError
            daemon.main()
        connection.assert_called_once_with("127.0.0.1", 1234, timeout=5)
        factory.assert_not_called()
        self.assertEqual((self.store.private / "runtime.json").read_bytes(), runtime)
        self.assertFalse((self.store.private / "daemon-history").exists())

    def test_external_legacy_runtime_url_is_never_contacted(self):
        self.metadata("runtime.json", {"workspace": str(self.root), "workspace_id": self.store.workspace_id,
                                      "url": "http://foreign.example:1234", "pid": os.getpid(),
                                      "nonce": "actual-private-nonce-1234567890"})
        with patch.object(daemon, "HTTPConnection") as connection:
            self.assertFalse(daemon._legacy_owner_alive(self.store))
            connection.assert_not_called()

    def test_hardlinked_guard_metadata_or_ancestor_reparse_is_rejected(self):
        for name, operation in (("daemon.guard", lambda: daemon.workspace_guard(self.store).__enter__()),
                                ("daemon.lock", lambda: daemon._archive_daemon_metadata(self.store, "daemon.lock"))):
            path = self.store.private / name
            path.write_bytes(b"0")
            alias = self.root / "hardlink-alias"
            os.link(path, alias)
            with self.assertRaises(BusError):
                operation()
            self.assertEqual(alias.read_bytes(), b"0")
            alias.unlink()
            path.unlink()
        original = Path.lstat

        def reparse_snapshot(path):
            info = original(path)
            if path == self.root.parent:
                values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                values["st_file_attributes"] = getattr(info, "st_file_attributes", 0) | 0x400
                return SimpleNamespace(**values)
            return info

        with patch.object(Path, "lstat", reparse_snapshot), self.assertRaises(BusError), daemon.workspace_guard(self.store):
            self.fail("Linked ancestors must be rejected before acquiring a lease")

    def worker(self, index):
        script = """import sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from daemon import workspace_guard
from store import BusStore, BusError
root=Path(sys.argv[2]); index=sys.argv[3]
store=BusStore(root)
(root/('ready-'+index)).write_text('ready')
deadline=time.monotonic()+15
while not (root/'go').exists():
    if time.monotonic()>deadline: raise RuntimeError('fixture barrier timeout')
    time.sleep(0.01)
try:
    with workspace_guard(store):
        (root/('outcome-'+index)).write_text('held')
        while not (root/'release').exists():
            if time.monotonic()>deadline: raise RuntimeError('fixture release timeout')
            time.sleep(0.01)
except BusError:
    (root/('outcome-'+index)).write_text('busy')
"""
        process = subprocess.Popen([sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), str(self.root), str(index)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.children.append(process)
        return process

    def wait_for(self, paths):
        deadline = time.monotonic() + 12
        while not all(path.exists() for path in paths):
            if time.monotonic() >= deadline:
                self.fail("Isolated guard fixture did not reach its barrier")
            time.sleep(0.01)

    def test_two_concurrent_processes_have_exactly_one_lease_owner(self):
        children = [self.worker(index) for index in range(2)]
        self.wait_for([self.root / ("ready-" + str(index)) for index in range(2)])
        (self.root / "go").write_text("go")
        outcomes = [self.root / ("outcome-" + str(index)) for index in range(2)]
        try:
            self.wait_for(outcomes)
            self.assertEqual(sorted(path.read_text() for path in outcomes), ["busy", "held"])
        finally:
            (self.root / "release").write_text("release")
        for child in children:
            _, errors = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, errors.decode(errors="replace"))

    def test_process_crash_releases_lease_without_deleting_guard_inode(self):
        child = self.worker(0)
        self.wait_for([self.root / "ready-0"])
        (self.root / "go").write_text("go")
        self.wait_for([self.root / "outcome-0"])
        self.assertEqual((self.root / "outcome-0").read_text(), "held")
        self.metadata("daemon.lock", {"pid": child.pid, "nonce": "crashed-owner"})
        guard = self.store.private / "daemon.guard"
        inode = guard.stat().st_ino
        child.terminate()  # Explicitly the fixture process created above.
        child.communicate(timeout=10)
        with daemon.workspace_guard(self.store):
            self.assertEqual(guard.stat().st_ino, inode)
        self.assertEqual(guard.read_bytes(), b"0")


if __name__ == "__main__":
    unittest.main()
