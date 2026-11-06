import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from server import Runtime, Server
from mcp_fixture import mcp_exchange
import hosted as hosted_module
import sidecar_registry as registry


def set_up_server(case, *, automatic_service):
    case.temp = tempfile.TemporaryDirectory(prefix="avalokita-server-")
    case.addCleanup(case.temp.cleanup)
    root = Path(case.temp.name)
    case.workspace = root / "workspace"
    case.workspace.mkdir()
    home = root / "home"
    home.mkdir()
    temporary = root / "tmp"
    temporary.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
                                      "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8"}}
    environment.update(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / "roaming"),
                       LOCALAPPDATA=str(home / "local"), XDG_CONFIG_HOME=str(home / "config"),
                       XDG_DATA_HOME=str(home / "data"), TEMP=str(temporary), TMP=str(temporary),
                       AVALOKITA_WORKSPACE=str(case.workspace))
    isolated_environment = patch.dict(os.environ, environment, clear=True)
    isolated_environment.start()
    case.addCleanup(isolated_environment.stop)
    case.registry = root / "registry"
    isolated_registry = patch.object(registry, "REGISTRY", case.registry)
    isolated_registry.start()
    case.addCleanup(isolated_registry.stop)
    actual_register = registry.register_workspace

    def register_workspace(workspace, *, runtime, workspace_guard):
        return actual_register(workspace, runtime=runtime, registry_path=case.registry,
                               workspace_guard=workspace_guard)

    register_locally = patch.object(hosted_module, "register_workspace", register_workspace)
    register_locally.start()
    case.addCleanup(register_locally.stop)
    case.provider_starts = []

    def forbidden_adapter(*args, **kwargs):
        case.provider_starts.append(True)
        raise AssertionError("Blank HTTP fixture attempted a provider process")

    # An injected list opts into first-prompt onboarding, even when empty.
    # Offline HTTP fixtures disable discovery; automatic startup keeps that
    # normal onboarding path enabled and is tested independently below.
    case.runtime = Runtime(case.workspace, discovery=False, native_ipc=False,
                           hosted_factory=forbidden_adapter, hosted_discovery=[] if automatic_service else None)
    case.service_ready = threading.Event()
    actual_status = case.runtime.hosted._service_status

    def observe_status(state, *args, **kwargs):
        actual_status(state, *args, **kwargs)
        if state == "ready":
            case.service_ready.set()

    observe_ready = patch.object(case.runtime.hosted, "_service_status", observe_status)
    observe_ready.start()
    case.addCleanup(observe_ready.stop)
    if not automatic_service:
        # These tests exercise HTTP behavior with an intentionally offline bus.
        # Normal blank startup has its own unmocked integration cases below.
        keep_offline = patch.object(case.runtime.hosted, "start_service", return_value=None)
        keep_offline.start()
        case.addCleanup(keep_offline.stop)
    case.server = Server(case.workspace, runtime=case.runtime)
    case.thread = threading.Thread(target=case.server.serve_forever, daemon=True)
    case.thread.start()


def tear_down_server(case):
    case.server.shutdown()
    case.server.server_close()
    case.thread.join(timeout=4)
    case.assertFalse(case.thread.is_alive())
    case.assertEqual(case.runtime._active_work, 0)
    case.assertEqual(case.provider_starts, [])


class ServerTests(unittest.TestCase):
    def setUp(self):
        set_up_server(self, automatic_service=False)

    def tearDown(self):
        tear_down_server(self)

    def request(self, path, payload=None, headers=None, auth=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        own = {"X-Avalokita-Request": "1"}
        if auth:
            own["Authorization"] = "Bearer " + self.server.token
        if payload is not None:
            own["Content-Type"] = "application/json"
        own.update(headers or {})
        connection.request("POST" if payload is not None else "GET", path, json.dumps(payload) if payload is not None else None, own)
        response = connection.getresponse()
        status, response_headers, raw = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, json.loads(raw)

    def test_session_cannot_mint_operator_authority(self):
        self.assertEqual(self.request("/api/session", auth=False)[0], 401)
        self.assertEqual(self.request("/api/session", auth=False, headers={"Authorization": "Bearer fake-native-token"})[0], 401)
        status, headers, body = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertEqual(self.request("/api/state", auth=False, headers={"Cookie": cookie})[0], 200)
        self.assertNotIn(self.server.token, json.dumps(body))

    def test_host_origin_marker_and_no_cors(self):
        self.assertEqual(self.request("/api/state", headers={"Host": "localhost:" + str(self.server.server_port)})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"X-Avalokita-Request": ""})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_health_remembered_source_is_not_current_native_context(self):
        self.runtime.native_dispatcher = SimpleNamespace(source="remembered-private-id", routing={"observed": True}, _source_allowed=lambda source: True)
        with patch.dict(os.environ, {}, clear=True):
            health = self.request("/health", auth=False)[2]
        self.assertFalse(health["native_ipc"]["source_context_available"])
        self.assertTrue(health["native_ipc"]["routing_source_available"])
        self.assertEqual(health["native_ipc"]["routing_source"], "remembered_environment")
        self.assertNotIn("remembered-private-id", json.dumps(health))

    def test_offline_prompt_saved_once_and_pause_resume_preserve_authority(self):
        payload = {"text": "한국어\n멀티라인 질문", "idempotency_key": "one-human-input"}
        first = self.request("/api/prompt", payload)
        self.assertEqual(first[0], 200)
        self.assertEqual(first[2]["dispatch"]["state"], "waiting")
        self.assertEqual(self.request("/api/prompt", payload)[2]["goal"]["id"], first[2]["goal"]["id"])
        self.assertEqual(self.request("/api/prompt", {**payload, "text": "different"})[0], 409)
        self.assertEqual(self.request("/api/control", {"action": "pause"})[2]["paused"], True)
        self.assertEqual(self.request("/api/control", {"action": "resume"})[2]["paused"], False)
        state = self.request("/api/state")[2]
        self.assertEqual(len(state["goals"]), 1)
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual({key: state["goals"][0][key] for key in ("id", "budget", "used", "deadline")},
                         {key: first[2]["goal"][key] for key in ("id", "budget", "used", "deadline")})
        self.assertEqual(state["participants"], [])
        self.assertEqual(state["connection"]["state"], "offline")
        self.assertFalse((self.workspace / ".sharebus").exists())

    def test_first_prompt_without_usable_provider_is_rejected_before_bus_creation(self):
        self.runtime.hosted.initialize_on_prompt = True
        self.runtime.hosted.discovery = []
        with patch.object(self.runtime.hosted, "_ensure_bus",
                          side_effect=AssertionError("Unavailable provider attempted bus creation")) as ensure_bus:
            status, _, value = self.request("/api/prompt",
                {"text": "First input with no installed provider", "idempotency_key": "no-provider-first-input"})
        self.assertEqual(status, 409)
        self.assertIn("unavailable", value["error"]["message"])
        ensure_bus.assert_not_called()
        snapshot = self.runtime.store.snapshot()
        self.assertEqual(snapshot["goals"], [])
        self.assertEqual(snapshot["messages"], [])
        with self.runtime.bridge._db() as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0], 0)
        self.assertIsNone(self.runtime.hosted.server)
        self.assertFalse((self.workspace / ".sharebus").exists())
        self.assertEqual(self.provider_starts, [])

    def test_ready_cli_health_and_no_token_stdout(self):
        prompt = "영속성 확인 · 한국어 원본"
        self.runtime.prompt({"text": prompt, "idempotency_key": "unicode-cli"})
        ready = self.workspace / "ready.json"
        ready.write_text(json.dumps({"url": self.server.url, "pid": __import__("os").getpid(), "workspace": str(self.workspace), "instance_id": self.server.instance_id, "token": self.server.token}), encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if key != "PYTHONUTF8"}
        env["PYTHONIOENCODING"] = "cp949"
        cli = subprocess.run([sys.executable, str(HERE / "avalokita.py"), "--workspace", str(self.workspace), "--ready-file", str(ready), "state"], capture_output=True, text=True, encoding="utf-8", env=env, timeout=15)
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["workspace"]["path"], str(self.workspace))
        self.assertEqual(json.loads(cli.stdout)["messages"][0]["body"], prompt)
        self.assertNotIn(self.server.token, cli.stdout + cli.stderr)

    def test_optional_mcp_entry_lists_tools_without_model(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "local-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        result = mcp_exchange(HERE / "mcp_server.py", lines)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("avalokita_peer", result.stdout)

    def test_operator_private_draft_survives_restart_without_creating_work(self):
        self.assertEqual(self.request("/api/draft", auth=False)[0], 401)
        self.assertEqual(self.request("/api/draft", {"text": "no", "reply": None, "pending": None}, auth=False)[0], 401)
        value = {"text": "아직 보내지 않은 입력\n한글", "reply": {"id": "public-question", "label": "CEO"},
                 "pending": {"text": "Unknown outcome", "key": "same-human-key", "replyTo": None}}
        status, _, saved = self.request("/api/draft", value)
        self.assertEqual(status, 200)
        self.assertTrue(saved["saved"])
        self.assertGreater(saved["updated_at"], 0)
        self.assertEqual(Runtime(self.workspace, discovery=False).draft()["draft"], value)
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertFalse((self.workspace / ".sharebus").exists())
        self.assertEqual(self.request("/api/draft", {**value, "text": "x" * 65537})[0], 400)
        self.assertEqual(self.request("/api/draft", {**value, "actor": "peer"})[0], 400)
        self.assertEqual(self.request("/api/draft")[2]["draft"], value)

    def test_linked_private_draft_cannot_overwrite_another_file(self):
        original = self.workspace / "original.txt"
        original.write_text("preserve original", encoding="utf-8")
        path = self.runtime.bridge.receipts.parent / "draft.json"
        os.link(original, path)
        self.assertEqual(self.request("/api/draft", {"text": "replace", "reply": None, "pending": None})[0], 403)
        self.assertEqual(self.request("/api/draft")[0], 403)
        self.assertEqual(original.read_text(encoding="utf-8"), "preserve original")


class AutomaticServiceStartupTests(unittest.TestCase):
    def setUp(self):
        set_up_server(self, automatic_service=True)

    def tearDown(self):
        tear_down_server(self)

    def test_blank_startup_prepares_verified_service_without_creating_work(self):
        self.assertTrue(self.service_ready.wait(6), "Automatic service did not reach its observed ready event")
        bus = self.runtime.hosted.server
        self.assertTrue(bus.serving.is_set())
        self.assertTrue((self.workspace / ".sharebus").is_dir())
        runtime = registry.verified_runtime(self.workspace)
        self.assertEqual(runtime["nonce"], bus.nonce)
        expected_runtime = bus.runtime()
        identity = ("version", "workspace", "workspace_id", "url", "pid", "nonce")
        self.assertEqual({key: runtime[key] for key in identity},
                         {key: expected_runtime[key] for key in identity})
        entries = list(self.registry.glob("*.json"))
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].is_file())
        self.assertFalse(entries[0].is_symlink())
        self.assertEqual(entries[0].parent, self.registry)
        record = json.loads(entries[0].read_text(encoding="utf-8"))
        self.assertEqual({key: record[key] for key in ("version", "workspace", "workspace_id", "runtime_nonce")},
                         {"version": expected_runtime["version"], "workspace": str(self.workspace.resolve(strict=True)),
                          "workspace_id": expected_runtime["workspace_id"], "runtime_nonce": expected_runtime["nonce"]})
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        self.assertEqual(bus.store.jobs(), [])
        self.assertEqual(bus.store.agents(), [])
        self.assertEqual(self.provider_starts, [])

    def test_ready_summary_reads_cached_service_without_initializing_or_polling(self):
        self.assertTrue(self.service_ready.wait(6), "Automatic service did not reach its observed ready event")
        caller = threading.get_ident()

        def forbid_summary_call(original):
            def observe(*args, **kwargs):
                if threading.get_ident() == caller:
                    raise AssertionError("Cached summary attempted service initialization or HTTP")
                return original(*args, **kwargs)
            return observe

        with patch.object(self.runtime.hosted, "_ensure_bus", forbid_summary_call(self.runtime.hosted._ensure_bus)), \
                patch.object(hosted_module, "verified_runtime", forbid_summary_call(hosted_module.verified_runtime)), \
                patch.object(hosted_module, "register_workspace", forbid_summary_call(hosted_module.register_workspace)), \
                patch.object(self.runtime.bridge, "observe", forbid_summary_call(self.runtime.bridge.observe)):
            self.assertEqual(self.runtime.summary()["service"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
