"""Actual MCP subprocess checks against a real, native-model-free workspace daemon."""

from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from daemon import BusHTTPServer
import hooks_bridge
from hooks_bridge import forward
from install import apply, codex_update, install_auto, prepare, server_entry
from mcp_server import BusClient, BusError, TOOLS


@asynccontextmanager
async def connection(workspace: Path, provider="codex", managed: dict | None = None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("SHAREBUS_")}
    environment.update(managed or {})
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(HERE / "mcp_server.py"), "--provider", provider, "--workspace", str(workspace)], env=environment)
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield session


async def invoke(session, name: str, arguments: dict | None = None, *, meta: dict | None = None):
    result = await session.call_tool(name, arguments or {}, meta=meta)
    if result.isError:
        raise AssertionError("Unexpected MCP tool error (body omitted).")
    return result.structuredContent or json.loads(result.content[0].text)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sharebus-mcp-")
        self.root = Path(self.temporary.name).resolve()
        self.server = BusHTTPServer(self.root, providers="none", enable_native=False, discovery=[])
        (self.root / ".sharebus/private/runtime.json").write_text(json.dumps(self.server.runtime()), encoding="utf-8")
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.close()
        self.temporary.cleanup()

    async def test_discovery_external_identity_and_private_error(self):
        async with connection(self.root) as session:
            listed = await session.list_tools()
            self.assertEqual({tool.name for tool in listed.tools}, set(TOOLS))
            missing = await session.call_tool("sharebus_status", {})
            self.assertTrue(missing.isError)
            first = await invoke(session, "sharebus_join", {"label": "CTO", "role": "CTO", "native_session_id": "native-cto"})
            second = await invoke(session, "sharebus_join", {"label": "CEO", "role": "CEO", "native_session_id": "native-ceo"})
            self.assertNotEqual(first["session_token"], second["session_token"])
            for joined in (first, second):
                state = await invoke(session, "sharebus_status", {"session_token": joined["session_token"]})
                self.assertEqual(state["own_agent"]["id"], joined["agent"]["id"])
                self.assertEqual(state["own_agent"]["role"], joined["agent"]["role"])
                self.assertNotIn(joined["session_token"], json.dumps(state))
            rejected = await session.call_tool("sharebus_status", {"session_token": "private-invalid-token-do-not-log"})
            self.assertTrue(rejected.isError)
            self.assertNotIn("private-invalid-token-do-not-log", json.dumps(rejected.model_dump()))

    async def test_managed_identity_token_stays_private_and_provider_is_bound(self):
        agent, token = self.server.store.register("claude", "designer", mode="managed_new")
        bindings = {"SHAREBUS_WORKSPACE": str(self.root), "SHAREBUS_AGENT_ID": agent["id"],
                    "SHAREBUS_AGENT_TOKEN_FILE": str(self.root / ".sharebus/private/credentials" / (agent["id"] + ".token"))}
        async with connection(self.root, "claude", bindings) as session:
            joined = await invoke(session, "sharebus_join")
            self.assertTrue(joined["managed"])
            self.assertNotIn("session_token", joined)
            self.assertNotIn(token, json.dumps(joined))
            state = await invoke(session, "sharebus_status")
            self.assertEqual(state["own_agent"]["id"], agent["id"])
            self.assertNotIn(token, json.dumps(state))
            switched = await session.call_tool("sharebus_status", {"workspace": str(self.root.parent)})
            self.assertTrue(switched.isError)
        async with connection(self.root, "codex") as session:
            mismatched = await session.call_tool("sharebus_status", {"session_token": token})
            self.assertTrue(mismatched.isError)

    async def test_real_sdk_request_metadata_binds_only_existing_exact_native_hook(self):
        from auto_enroll import bootstrap_hook
        native = "codex-host-main-thread"
        self.server.store.configure_auto(enabled=True)
        payload = {"cwd": str(self.root), "session_id": native, "hook_event_name": "SessionStart"}
        context = bootstrap_hook("codex", payload, workspace=self.root)
        self.assertIsNotNone(context)
        async with connection(self.root) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            self.assertIn("context_file", tools["sharebus_status"].inputSchema["properties"])
            self.assertNotIn("ctx", tools["sharebus_status"].inputSchema["properties"])
            meta = {"threadId": native, "sessionId": "mcp-transport-not-a-native-thread"}
            # This is a real SDK _meta frame, not a tool argument or fake context.
            joined_result = await session.call_tool("sharebus_join", {"role": "user-assigned-role", "native_session_id": native}, meta=meta)
            self.assertFalse(joined_result.isError, joined_result.content[0].text)
            joined = joined_result.structuredContent or json.loads(joined_result.content[0].text)
            self.assertTrue(joined["connected"])
            self.assertNotIn("session_token", joined)
            state = await invoke(session, "sharebus_status", meta=meta)
            self.assertEqual(state["own_agent"]["native_session_id"], native)
            self.assertEqual(state["own_agent"]["role"], "user-assigned-role")
            again = await invoke(session, "sharebus_join", meta={**meta, "sessionId": "next-transport"})
            self.assertEqual(again["agent"]["id"], joined["agent"]["id"])
            self.assertEqual(again["agent"]["role"], "user-assigned-role")
            for arguments, metadata in (({}, {"sessionId": native}), ({"threadId": native}, None),
                                         ({"context_file": str(context)}, {"threadId": "another-thread"}),
                                         ({"native_session_id": "another-thread"}, meta)):
                name = "sharebus_join" if "native_session_id" in arguments else "sharebus_status"
                self.assertTrue((await session.call_tool(name, arguments, meta=metadata)).isError)
            self.assertEqual(len(self.server.store.agents()), 1)
            self.assertEqual(self.server.store.events()["events"], [])

    async def test_metadata_alone_cannot_bootstrap_or_bind_inherited_foreign_workspace(self):
        from auto_enroll import bootstrap_hook
        native = "native-only-in-this-room"
        self.server.store.configure_auto(enabled=True)
        async with connection(self.root) as session:
            missing = await session.call_tool("sharebus_status", {}, meta={"threadId": native})
            self.assertTrue(missing.isError)
            self.assertEqual(self.server.store.agents(), [])
        context = bootstrap_hook("codex", {"cwd": str(self.root), "session_id": native, "hook_event_name": "SessionStart"}, workspace=self.root)
        foreign = self.root / "other-workspace"
        foreign.mkdir()
        async with connection(self.root, managed={"SHAREBUS_WORKSPACE": str(foreign)}) as session:
            status = await invoke(session, "sharebus_status", meta={"threadId": native, "sessionId": "foreign-global-transport"})
            self.assertEqual(status["workspace"]["path"], str(self.root))
            self.assertEqual(status["own_agent"]["native_session_id"], native)
            self.assertFalse((foreign / ".sharebus").exists())
            wrong = await session.call_tool("sharebus_status", {"workspace": str(foreign), "context_file": str(context)},
                                            meta={"threadId": native})
            self.assertTrue(wrong.isError)

    async def test_private_context_mcp_claim_publish_peer_send_and_ack(self):
        from auto_enroll import bootstrap_hook
        self.server.store.configure_auto(enabled=True)
        context = bootstrap_hook("claude", {"cwd": str(self.root), "session_id": "claude-native-context",
                                             "hook_event_name": "SessionStart"}, workspace=self.root)
        async with connection(self.root, "claude") as session:
            arguments = {"context_file": str(context), "workspace": str(self.root)}
            joined = await invoke(session, "sharebus_join", arguments)
            peer, _ = self.server.store.register("codex", "peer", mode="external_attached", native_session_id="codex-peer")
            human = self.server.store.send("user", joined["agent"]["id"], "actual current user task", "task")
            read = await invoke(session, "sharebus_read", arguments)
            job = read["inbox"]["jobs"][0]
            sent = await invoke(session, "sharebus_send", {**arguments, "to": peer["id"], "body": "scoped handoff", "kind": "task"})
            self.assertEqual(sent["root_id"], human["root_id"])
            published = await invoke(session, "sharebus_publish", {**arguments, "text": "selected output", "filename": "chosen.txt"})
            self.assertEqual(published["artifact"]["owner"], joined["agent"]["id"])
            await invoke(session, "sharebus_read", {**arguments, "after": read["last_seq"], "ack": [job["id"]]})
            self.assertEqual(next(row for row in self.server.store.jobs() if row["id"] == job["id"])["state"], "completed")
            private = json.loads(Path(context).read_text(encoding="utf-8"))
            public = json.dumps({"joined": joined, "read": read, "sent": sent, "artifact": published})
            for name in ("code", "claim_id", "token"):
                if private.get(name):
                    self.assertNotIn(private[name], public)

    async def test_human_root_peer_handoff_ack_and_question(self):
        async with connection(self.root, "codex") as codex, connection(self.root, "claude") as claude:
            first = await invoke(codex, "sharebus_join", {"label": "CTO"})
            second = await invoke(claude, "sharebus_join", {"label": "CMO"})
            one, two = {"session_token": first["session_token"]}, {"session_token": second["session_token"]}
            unauthorized = await codex.call_tool("sharebus_send", {**one, "to": second["agent"]["id"], "body": "new autonomous root", "kind": "task"})
            self.assertTrue(unauthorized.isError)
            human = self.server.store.send("user", first["agent"]["id"], "사용자 목표", "task")
            inbox = await invoke(codex, "sharebus_read", one)
            self.assertEqual(len(inbox["inbox"]["jobs"]), 1)
            delegated = await invoke(codex, "sharebus_send", {**one, "to": second["agent"]["id"], "body": "레퍼런스를 조사해줘", "kind": "task"})
            self.assertEqual(delegated["root_id"], human["root_id"])
            received = await invoke(claude, "sharebus_read", two)
            job = received["inbox"]["jobs"][0]
            await invoke(claude, "sharebus_read", {**two, "after": received["last_seq"], "ack": [job["id"]]})
            done = next(item for item in self.server.store.jobs() if item["id"] == job["id"])
            self.assertEqual(done["state"], "completed")
            question = await invoke(codex, "sharebus_ask_user", {**one, "question": "어느 디자인을 선택할까요?"})
            self.assertEqual(question["event"]["recipient"], "user")
            self.assertEqual(question["jobs"], [])

    async def test_immutable_artifact_copy_and_path_boundary(self):
        async with connection(self.root, "codex") as codex, connection(self.root, "claude") as claude:
            first = await invoke(codex, "sharebus_join")
            second = await invoke(claude, "sharebus_join")
            one, two = {"session_token": first["session_token"]}, {"session_token": second["session_token"]}
            (self.root / "chosen.txt").write_text("선택한 생성물", encoding="utf-8")
            published = await invoke(codex, "sharebus_publish", {**one, "source": "chosen.txt"})
            artifact = published["artifact"]
            (self.root / "chosen.txt").write_text("수정됨", encoding="utf-8")
            copied = await invoke(claude, "sharebus_copy", {**two, "artifact_id": artifact["id"], "filename": "reference.txt"})
            self.assertEqual(copied["artifact"]["sha256"], artifact["sha256"])
            self.assertNotEqual(copied["artifact"]["owner"], artifact["owner"])
            self.assertEqual((self.root / copied["artifact"]["relative_path"]).read_text("utf-8"), "선택한 생성물")
            for source in ("../outside", ".sharebus/private/operator.token", "C:/Windows/win.ini"):
                result = await codex.call_tool("sharebus_publish", {**one, "source": source})
                self.assertTrue(result.isError)
            ambiguous = await codex.call_tool("sharebus_publish", {**one, "source": "chosen.txt", "text": "both"})
            self.assertTrue(ambiguous.isError)

    def test_runtime_refuses_foreign_url_before_any_network(self):
        runtime = self.server.runtime()
        runtime["url"] = "http://example.com:80"
        (self.root / ".sharebus/private/runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
        with self.assertRaises(BusError):
            BusClient("codex", self.root).request(self.root, "/api/state", "private")

    def test_reused_loopback_port_receives_neither_credentials_nor_message_body(self):
        observed = []
        runtime = self.server.runtime()

        class ReusedPort(BaseHTTPRequestHandler):
            def do_GET(inner):
                observed.append((inner.path, inner.headers.get("Authorization")))
                payload = json.dumps({"ok": True, "workspace_id": runtime["workspace_id"],
                                      "pid": runtime["pid"], "nonce": "wrong-process-nonce"}).encode()
                inner.send_response(200)
                inner.send_header("Content-Length", str(len(payload)))
                inner.end_headers()
                inner.wfile.write(payload)

            def do_POST(inner):
                observed.append((inner.path, inner.headers.get("Authorization")))
                inner.send_response(500)
                inner.end_headers()

            def log_message(inner, *_):
                pass

        listener = ThreadingHTTPServer(("127.0.0.1", 0), ReusedPort)
        thread = threading.Thread(target=lambda: listener.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        runtime["url"] = f"http://127.0.0.1:{listener.server_port}"
        (self.root / ".sharebus/private/runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
        try:
            with self.assertRaises(BusError):
                BusClient("codex", self.root).request(self.root, "/api/message", "secret-test-token", {"body": "private test input"})
            self.assertEqual(observed, [("/health", None)])
        finally:
            listener.shutdown()
            thread.join(timeout=5)
            listener.server_close()

    async def test_duplicate_join_requires_private_token_or_human_single_use_recovery(self):
        async with connection(self.root, "codex") as session:
            joined = await invoke(session, "sharebus_join", {"role": "CTO", "native_session_id": "native-cto"})
            duplicate = await session.call_tool("sharebus_join", {"native_session_id": "native-cto"})
            self.assertTrue(duplicate.isError)
            self.assertIn("one-time recovery code", duplicate.content[0].text)
            self.assertEqual(len(self.server.store.agents()), 1)
            refreshed = await invoke(session, "sharebus_join", {"native_session_id": "native-cto", "session_token": joined["session_token"]})
            self.assertEqual(refreshed["agent"]["id"], joined["agent"]["id"])
            client = BusClient("codex", self.root)
            recovery = client.request(self.root, "/api/agent/recovery", self.server.store.operator_token, {"agent_id": joined["agent"]["id"]})
            recovered = await invoke(session, "sharebus_join", {"native_session_id": "native-cto", "recovery_code": recovery["recovery_code"]})
            self.assertEqual(recovered["agent"]["id"], joined["agent"]["id"])
            self.assertEqual(recovered["session_token"], joined["session_token"])
            self.assertEqual(len(self.server.store.agents()), 1)
            replay = await session.call_tool("sharebus_join", {"native_session_id": "native-cto", "recovery_code": recovery["recovery_code"]})
            self.assertTrue(replay.isError)
            state = await invoke(session, "sharebus_status", {"session_token": recovered["session_token"]})
            self.assertNotIn(recovery["recovery_code"], json.dumps(state))

    async def test_explicit_native_mcp_completion_validates_binding_and_allows_second_job(self):
        native_id = "00000000-0000-4000-8000-000000000456"
        agent, token = self.server.store.register("antigravity", "CTO", mode="native_attached", native_session_id=native_id)
        async with connection(self.root, "antigravity") as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            self.assertEqual(set(tools), set(TOOLS))
            self.assertTrue({"job_id", "event_id", "native_session_id"} <= set(tools["sharebus_send"].inputSchema["properties"]))
            for number in (1, 2):
                human = self.server.store.send("user", agent["id"], f"native task {number}", "task")
                job = self.server.store.claim(agent["id"])
                self.assertIsNotNone(job, "The next native job must become claimable after completion.")
                self.server.store.await_native(job["id"], "synthetic native transport receipt")
                completion = {"session_token": token, "to": "user", "kind": "result", "body": f"native result {number}",
                              "job_id": job["id"], "event_id": human["event"]["id"], "native_session_id": native_id}
                if number == 1:
                    partial = {**completion}
                    partial.pop("event_id")
                    self.assertTrue((await session.call_tool("sharebus_send", partial)).isError)
                    self.assertTrue((await session.call_tool("sharebus_send", {**completion, "native_session_id": "foreign-native"})).isError)
                    self.assertTrue((await session.call_tool("sharebus_send", {**completion, "event_id": "foreign-event"})).isError)
                    self.server.store.set_paused(True)
                    self.assertTrue((await session.call_tool("sharebus_send", completion)).isError)
                    self.server.store.set_paused(False)
                    self.assertEqual(self.server.store.jobs()[0]["state"], "awaiting_native_reply")
                result = await invoke(session, "sharebus_send", completion)
                self.assertTrue(result["completed"])
                self.assertEqual(result["root_id"], human["root_id"])
                record = next(item for item in self.server.store.jobs() if item["id"] == job["id"])
                self.assertEqual(record["state"], "completed")
                self.assertEqual(self.server.store.agent(agent["id"])["state"], "idle")
                self.assertTrue((await session.call_tool("sharebus_send", completion)).isError)

    def test_generic_native_hook_never_infers_human_authority(self):
        agent, _ = self.server.store.register("codex", "CTO", mode="external_attached")
        self.server.store.update_agent(agent["id"], native_session_id="native-cto")
        payload = {"cwd": str(self.root), "session_id": "native-cto", "hook_event_name": "UserPromptSubmit", "prompt": "디자인을 개선해주세요"}
        self.assertFalse(forward("codex", self.root, payload))
        (self.root / ".sharebus/enabled").write_text("enabled", encoding="utf-8")
        self.assertFalse(forward("codex", self.root, {**payload, "session_id": "someone-else"}))
        self.assertFalse(forward("codex", self.root, {**payload, "hook_event_name": "PostToolUse"}))
        self.assertFalse(forward("codex", self.root, {**payload, "prompt": "[Sharebus forwarded request; native permissions still apply]\npeer task"}))
        with patch.dict(os.environ, {"SHAREBUS_AGENT_ID": "managed"}):
            self.assertFalse(forward("codex", self.root, payload))
        for prompt in (payload["prompt"], "peer request without a prefix", "scheduled background task", "[SHAREBUS_AUTO_WAKE] queued work"):
            self.assertFalse(forward("codex", self.root, {**payload, "prompt": prompt}))
        self.assertEqual(self.server.store.events()["events"], [])
        self.assertEqual(self.server.store.jobs(), [])
        self.assertIsNone(self.server.store.agent(agent["id"])["active_root"])

    def test_stop_from_different_turn_cannot_complete_new_input(self):
        _, human, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "turn_id": "new-turn",
                   "hook_event_name": "UserPromptSubmit",
                   "prompt": f"[SHAREBUS_EVENT:{human['event']['id']};JOB:{job['id']}]\n<incoming_message>새 작업</incoming_message>"}
        self.assertTrue(forward("claude", self.root, payload))
        stale_stop = {**payload, "hook_event_name": "Stop", "turn_id": "old-turn", "last_assistant_message": "이전 결과"}
        self.assert_native_stop_ignored(stale_stop)
        self.assertEqual(self.server.store.jobs()[0]["state"], "awaiting_native_reply")
        self.assertTrue(forward("claude", self.root, {**stale_stop, "turn_id": "new-turn", "last_assistant_message": "새 작업 결과"}))
        self.assertEqual(self.server.store.jobs()[0]["state"], "completed")

    def native_pending(self):
        agent, _ = self.server.store.register("claude", "CTO", mode="native_attached", native_session_id="native-target")
        human = self.server.store.send("user", agent["id"], "native task", "task")
        job = self.server.store.claim(agent["id"])
        self.server.store.await_native(job["id"], "verified test transport")
        (self.root / ".sharebus/enabled").write_text("enabled", encoding="utf-8")
        return agent, human, job

    def assert_native_stop_ignored(self, payload, provider="claude"):
        before_events = self.server.store.events()
        before_jobs = self.server.store.jobs()
        original_request = BusClient.request
        with patch.object(BusClient, "request", autospec=True, side_effect=original_request) as request, \
                patch("hooks_bridge._read_private", wraps=hooks_bridge._read_private) as read:
            self.assertFalse(forward(provider, self.root, payload))
            self.assertNotIn("/api/native/result", [call.args[2] for call in request.call_args_list])
            self.assertTrue(all((len(call.args) < 5 or call.args[4] is None) and call.kwargs.get("payload") is None
                                for call in request.call_args_list))
            self.assertFalse(any(Path(call.args[0]).parent == self.root / ".sharebus/private/credentials"
                                 for call in read.call_args_list))
        self.assertEqual(self.server.store.events(), before_events)
        self.assertEqual(self.server.store.jobs(), before_jobs)
        return request.call_count

    def test_plain_business_stop_without_active_work_is_private(self):
        self.server.store.register("claude", "Business CTO", mode="external_attached", native_session_id="business-native")
        (self.root / ".sharebus/enabled").write_text("enabled", encoding="utf-8")
        payload = {"cwd": str(self.root), "session_id": "business-native", "hook_event_name": "Stop",
                   "last_assistant_message": "private business work that was never selected for sharing"}
        self.assertEqual(self.assert_native_stop_ignored(payload), 0)
        self.assertFalse((self.root / ".sharebus/private/hook-context").exists())

    def test_uncorrelated_stop_during_queued_work_is_private(self):
        agent, _ = self.server.store.register("claude", "CTO", mode="native_attached", native_session_id="native-target")
        self.server.store.send("user", agent["id"], "separate queued task", "task")
        (self.root / ".sharebus/enabled").write_text("enabled", encoding="utf-8")
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "Stop",
                   "last_assistant_message": "private output from another business turn"}
        self.assertEqual(self.assert_native_stop_ignored(payload), 0)
        self.assertEqual(self.server.store.jobs()[0]["state"], "queued")

    def test_context_completion_requires_exact_string_turn_and_owned_pending_ids(self):
        agent, human, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "UserPromptSubmit", "turn_id": "current-turn",
                   "prompt": f"[SHAREBUS_EVENT:{human['event']['id']};JOB:{job['id']}]\n<incoming_message>selected task</incoming_message>"}
        self.assertTrue(forward("claude", self.root, payload))
        context_file = self.root / ".sharebus/private/hook-context" / (agent["id"] + ".json")
        context = json.loads(context_file.read_text(encoding="utf-8"))
        variants = [({key: value for key, value in context.items() if key != "turn_id"}, "current-turn"),
                    ({**context, "turn_id": None}, None), ({**context, "turn_id": ""}, ""),
                    ({**context, "turn_id": " "}, " "), ({**context, "turn_id": True}, True),
                    ({**context, "turn_id": 7}, 7), (context, None), (context, "different-turn"),
                    ({**context, "native_session_id": "foreign-native"}, "current-turn"),
                    ({**context, "event_id": "00000000-0000-0000-0000-000000000000"}, "current-turn"),
                    ({**context, "job_id": "00000000-0000-0000-0000-000000000000"}, "current-turn")]
        for index, (saved, turn_id) in enumerate(variants):
            with self.subTest(case=index):
                context_file.write_text(json.dumps(saved), encoding="utf-8")
                self.assert_native_stop_ignored({**payload, "hook_event_name": "Stop", "turn_id": turn_id,
                                                 "last_assistant_message": "private unrelated assistant text"})
        self.assertEqual(self.server.store.jobs()[0]["state"], "awaiting_native_reply")

    def test_completed_context_cannot_publish_or_alter_newer_pending_work(self):
        agent, human, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "UserPromptSubmit", "turn_id": "prior-turn",
                   "prompt": f"[SHAREBUS_EVENT:{human['event']['id']};JOB:{job['id']}]\n<incoming_message>prior selected task</incoming_message>"}
        self.assertTrue(forward("claude", self.root, payload))
        self.server.store.native_result(agent["id"], "native-target", "explicit selected completion", job["id"], human["event"]["id"])
        self.server.store.send("user", agent["id"], "newer selected task", "task")
        newer = self.server.store.claim(agent["id"])
        self.server.store.await_native(newer["id"], "verified newer fixture transport")
        self.assert_native_stop_ignored({**payload, "hook_event_name": "Stop",
                                         "last_assistant_message": "private output after the completed prior job"})
        states = {row["id"]: row["state"] for row in self.server.store.jobs()}
        self.assertEqual(states[job["id"]], "completed")
        self.assertEqual(states[newer["id"]], "awaiting_native_reply")

    def test_empty_native_result_marker_is_ignored(self):
        _, human, job = self.native_pending()
        self.assert_native_stop_ignored({"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "Stop",
            "last_assistant_message": f"<!-- SHAREBUS_RESULT:{human['event']['id']};JOB:{job['id']} -->"})

    def test_forwarded_native_prompt_and_stop_complete_exact_pending_without_new_root(self):
        agent, human, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "UserPromptSubmit", "turn_id": "selected-turn",
                   "prompt": "[Sharebus forwarded request; native permissions still apply]\n"
                             + f"[SHAREBUS_EVENT:{human['event']['id']};JOB:{job['id']}]\n<incoming_message>native task</incoming_message>"}
        before = self.server.store.events()["last_seq"]
        self.assertTrue(forward("claude", self.root, payload))
        self.assertEqual(self.server.store.events()["last_seq"], before)
        self.assertTrue(forward("claude", self.root, {**payload, "hook_event_name": "Stop", "last_assistant_message": "native result"}))
        finished = next(item for item in self.server.store.jobs() if item["id"] == job["id"])
        self.assertEqual(finished["state"], "completed")
        result = self.server.store.events()["events"][-1]
        self.assertEqual(result["root_id"], human["root_id"])
        self.assertEqual(result["sender"], agent["id"])

    def test_hidden_native_result_marker_is_verified_and_removed(self):
        _, human, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "Stop",
                   "last_assistant_message": f"native result\n<!-- SHAREBUS_RESULT:{human['event']['id']};JOB:{job['id']} -->"}
        self.assertTrue(forward("claude", self.root, payload))
        self.assertEqual(self.server.store.jobs()[0]["state"], "completed")
        result = self.server.store.events()["events"][-1]
        self.assertEqual(result["body"], "native result")
        self.assertNotIn("SHAREBUS_RESULT", json.dumps(result))

    def test_native_marker_inside_untrusted_body_does_not_bind_a_turn(self):
        agent, human, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "UserPromptSubmit",
                   "prompt": "[Sharebus forwarded request; native permissions still apply]\n<incoming_message>"
                             + f"[SHAREBUS_EVENT:{human['event']['id']};JOB:{job['id']}]</incoming_message>"}
        before = self.server.store.events()["last_seq"]
        self.assertFalse(forward("claude", self.root, payload))
        self.assertEqual(self.server.store.events()["last_seq"], before)
        self.assertFalse((self.root / ".sharebus/private/hook-context" / (agent["id"] + ".json")).exists())

    def test_uncorrelated_native_stop_leaves_pending_work_and_output_private(self):
        _, _, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "Stop",
                   "last_assistant_message": "unmapped native reply"}
        self.assert_native_stop_ignored(payload)
        record = next(item for item in self.server.store.jobs() if item["id"] == job["id"])
        self.assertEqual(record["state"], "awaiting_native_reply")

    def test_foreign_native_result_marker_cannot_finish_or_cancel_owned_pending_job(self):
        _, human, job = self.native_pending()
        payload = {"cwd": str(self.root), "session_id": "native-target", "hook_event_name": "Stop",
                   "last_assistant_message": f"stale result\n<!-- SHAREBUS_RESULT:{human['event']['id']};JOB:00000000-0000-0000-0000-000000000000 -->"}
        self.assert_native_stop_ignored(payload)
        record = next(item for item in self.server.store.jobs() if item["id"] == job["id"])
        self.assertEqual(record["state"], "awaiting_native_reply")

    def test_installed_claude_exec_hook_handles_utf8_with_cp949_parent_and_unicode_workspace(self):
        root = self.root / "한글 회의 공간"
        root.mkdir()
        server = BusHTTPServer(root, providers="none", enable_native=False, discovery=[])
        (root / ".sharebus/private/runtime.json").write_text(json.dumps(server.runtime()), encoding="utf-8")
        (root / ".sharebus/enabled").write_text("enabled", encoding="utf-8")
        agent, _ = server.store.register("claude", "CTO", role="CTO", mode="external_attached", native_session_id="native-utf8")
        server.store.configure_auto(enabled=True)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        home = self.root / "fake home"
        home.mkdir()
        plan = prepare(home, Path(sys.executable), hooks_workspace=root, auto_hooks=True)
        config = json.loads(plan[home / ".claude/settings.json"])
        handler = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]
        self.assertEqual(handler["onFailure"], "continue")
        self.assertTrue(Path(handler["command"]).is_file())
        self.assertIsInstance(handler["args"], list)
        payload = {"cwd": str(root), "session_id": "native-utf8", "hook_event_name": "UserPromptSubmit",
                   "prompt": "한글 입력과 그림 🙂", "host_metadata": "x" * 150000}
        environment = {key: value for key, value in os.environ.items() if not key.startswith("SHAREBUS_")}
        environment.update({"PYTHONIOENCODING": "cp949", "PYTHONUTF8": "0"})
        try:
            result = subprocess.run([handler["command"], *handler["args"]],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True,
                timeout=30, env=environment, cwd=self.root)
            self.assertEqual(result.returncode, 0, "Installed hook process failed (private output omitted).")
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("private", context)
            self.assertIn("SHAREBUS.md", context)
            self.assertNotIn(payload["prompt"], context)
            self.assertEqual(server.store.events()["events"], [])
            self.assertEqual(server.store.agent(agent["id"])["state"], "idle")
            self.assertEqual(server.store.agent(agent["id"])["role"], "CTO")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.close()

    def test_hook_failure_writes_only_private_fixed_diagnostic(self):
        (self.root / ".sharebus/enabled").write_text("enabled", encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("SHAREBUS_")}
        environment.update({"PYTHONIOENCODING": "cp949", "PYTHONUTF8": "0"})
        result = subprocess.run([sys.executable, str(HERE / "hooks_bridge.py"), "--provider", "claude", "--workspace", str(self.root)],
            input=b"\xff private-input-do-not-log", capture_output=True, timeout=10, env=environment)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"private-input-do-not-log", result.stdout + result.stderr)
        log = self.root / ".sharebus/private/hook-errors.jsonl"
        diagnostic = json.loads(log.read_text("utf-8"))
        self.assertEqual(set(diagnostic), {"time", "provider", "event", "error"})
        self.assertEqual(diagnostic["error"], "optional_observation_failed")
        self.assertNotIn("private-input-do-not-log", log.read_text("utf-8"))

    def test_global_antigravity_hook_records_only_explicit_workspace_metadata(self):
        (self.root / ".sharebus/enabled").write_text("enabled", encoding="utf-8")
        native_id = "00000000-0000-4000-8000-000000000123"
        payload = {"conversationId": native_id, "workspacePaths": [str(self.root)],
                   "transcriptPath": str(Path.home() / ".gemini/antigravity/brain" / native_id / "transcript.jsonl"),
                   "modelName": "synthetic metadata fixture", "privatePrompt": "must not publish user input"}
        environment = {key: value for key, value in os.environ.items() if not key.startswith("SHAREBUS_")}
        result = subprocess.run([sys.executable, str(HERE / "hooks_bridge.py"), "--provider", "antigravity", "--event", "SessionStart"],
            input=json.dumps(payload).encode("utf-8"), capture_output=True, timeout=10, env=environment)
        self.assertEqual(result.returncode, 0, "Metadata hook failed (private output omitted).")
        record = json.loads((self.root / ".sharebus/private/antigravity-bridge/sessions" / (native_id + ".json")).read_text("utf-8"))
        self.assertEqual(record["native_session_id"], native_id)
        self.assertEqual(record["application"], "antigravity_app")
        self.assertNotIn("privatePrompt", record)
        self.assertEqual(self.server.store.agents(), [])
        self.assertEqual(self.server.store.events()["events"], [])


class InstallerTests(unittest.TestCase):
    def test_codex_queue_mode_removes_only_owned_waiters_and_preserves_manifest_choice(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-queue-install-") as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            codex = workspace / ".codex/hooks.json"
            global_codex = home / ".codex/hooks.json"
            claude = workspace / ".claude/settings.local.json"
            first = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True,
                            watch_timeout=1, codex_native_queue=False)
            apply(first, root / "first-backups")

            def handlers(config, event):
                return [hook for group in config["hooks"].get(event, []) for hook in group["hooks"]]

            config = json.loads(codex.read_text(encoding="utf-8"))
            watcher = next(hook for hook in handlers(config, "Stop") if "auto_watch.py" in str(hook))
            cancel = next(hook for hook in handlers(config, "Interrupt") if "auto_watch.py" in str(hook))
            observer = next(hook for hook in handlers(config, "Stop") if "hooks_bridge.py" in str(hook))
            custom_stop = {"type": "command", "command": "keep-custom-stop"}
            custom_interrupt = {"type": "command", "command": "keep-custom-interrupt"}
            other_workspace = root / "other-workspace"
            foreign = {key: value.replace(str(workspace), str(other_workspace)) if isinstance(value, str) else value
                       for key, value in watcher.items()}
            for group in config["hooks"]["Stop"]:
                if watcher in group["hooks"]:
                    group["matcher"] = "preserve-matcher"
                    group["hooks"].append(custom_stop)
            config["hooks"]["Stop"].append({"hooks": [foreign]})
            config["hooks"]["Interrupt"][0]["hooks"].append(custom_interrupt)
            config["custom_settings"] = {"keep": True}
            codex.write_text(json.dumps(config), encoding="utf-8")
            global_config = json.loads(global_codex.read_text(encoding="utf-8"))
            global_config["hooks"]["Stop"] = [{"hooks": [watcher, custom_stop]}]
            global_codex.write_text(json.dumps(global_config), encoding="utf-8")
            claude_before = claude.read_text(encoding="utf-8")
            plan = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, codex_native_queue=True)
            selected = json.loads(plan[codex])
            self.assertNotIn(watcher, handlers(selected, "Stop"))
            self.assertNotIn(cancel, handlers(selected, "Interrupt"))
            self.assertIn(observer, handlers(selected, "Stop"))
            self.assertIn(foreign, handlers(selected, "Stop"))
            self.assertIn(custom_stop, handlers(selected, "Stop"))
            self.assertIn(custom_interrupt, handlers(selected, "Interrupt"))
            self.assertIn({"matcher": "preserve-matcher", "hooks": [custom_stop]}, selected["hooks"]["Stop"])
            self.assertEqual(selected["custom_settings"], config["custom_settings"])
            global_selected = json.loads(plan[global_codex])
            self.assertNotIn(watcher, handlers(global_selected, "Stop"))
            self.assertIn(custom_stop, handlers(global_selected, "Stop"))
            self.assertIn("SessionStart", global_selected["hooks"])
            self.assertIn("UserPromptSubmit", global_selected["hooks"])
            self.assertEqual(plan[claude], claude_before)
            self.assertTrue(any(hook.get("asyncRewake") for hook in handlers(json.loads(plan[claude]), "Stop")))
            apply(plan, root / "queue-backups")
            self.assertGreater(len(list((root / "queue-backups").glob("*.bak"))), 0)
            with patch("install.subprocess.run") as run, patch("install.subprocess.Popen") as popen:
                receipt = install_auto(workspace, home=home, python=Path(sys.executable), codex_native_queue=None)
                run.assert_not_called()
                popen.assert_not_called()
            self.assertIs(receipt["codex_native_queue"], True)
            self.assertEqual(receipt["codex_delivery"], "native_queue")
            self.assertFalse(receipt["applied"])
            self.assertEqual(receipt["native_messages_sent"], 0)
            unchanged = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)
            self.assertEqual(json.loads(unchanged[codex]), selected)
            restored = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, codex_native_queue=False)
            restored_config = json.loads(restored[codex])
            self.assertIn(watcher, handlers(restored_config, "Stop"))
            self.assertIn(cancel, handlers(restored_config, "Interrupt"))
            self.assertIn(foreign, handlers(restored_config, "Stop"))
            self.assertEqual(restored[claude], claude_before)
            apply(restored, root / "fallback-backups")
            self.assertIs(install_auto(workspace, home=home, python=Path(sys.executable))["codex_native_queue"], False)

    def test_queue_selection_is_typed_and_manifest_update_uses_cas(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-queue-cas-") as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            for invalid in (1, "true", {}):
                with self.assertRaises(ValueError):
                    prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, codex_native_queue=invalid)
            self.assertFalse((workspace / ".sharebus").exists())
            initial = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)
            apply(initial, root / "backups")
            codex = workspace / ".codex/hooks.json"
            before = codex.read_bytes()
            manifest = workspace / ".sharebus/hooks-install.json"
            plan = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, codex_native_queue=True)
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["watch_timeout_seconds"] = 2
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                apply(plan, root / "refused-backups")
            self.assertEqual(codex.read_bytes(), before)
            value["codex_native_queue"] = "true"
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)

    def test_retained_mit_source_and_license_match_pinned_provenance(self):
        directory = HERE / "third_party"
        manifest = json.loads((directory / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["sources"]), 2)
        for source in manifest["sources"]:
            self.assertEqual(source["license"], "MIT")
            self.assertIn(source["commit"], source["raw_url"])
            self.assertIn(source["commit"], source["license_raw_url"])
            self.assertEqual(hashlib.sha256((directory / source["preserved_path"]).read_bytes()).hexdigest(), source["sha256"])
            license_file = directory / source["license_path"]
            self.assertEqual(hashlib.sha256(license_file.read_bytes()).hexdigest(), source["license_sha256"])
            self.assertIn("MIT License", license_file.read_text(encoding="utf-8"))

    def test_package_import_exports_cli_without_startup_or_workspace_effects(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-package-import-") as directory:
            environment = {key: value for key, value in os.environ.items() if not key.startswith("SHAREBUS_")}
            environment["PYTHONPATH"] = str(HERE.parent)
            result = subprocess.run([sys.executable, "-c", "import sharebus; assert callable(sharebus.Client); print('ok')"],
                                    cwd=directory, env=environment, capture_output=True, timeout=15, check=False)
            self.assertEqual(result.returncode, 0, "Package import failed (private output omitted).")
            self.assertEqual(result.stdout.strip(), b"ok")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_transport_update_preserves_user_tool_approvals_environment_and_flags(self):
        text = ('[mcp_servers.sharebus]\ncommand = "old"\nenabled = false\nstartup_timeout_sec = 90\n'
                '[mcp_servers.sharebus.env]\nUSER_SETTING = "keep"\n'
                '[mcp_servers.sharebus.tools.sharebus_send]\napproval_mode = "prompt"\n'
                '[mcp_servers.other]\ncommand = "keep-other"\n')
        before = tomllib.loads(text)
        entry = server_entry(Path(sys.executable), "codex")
        after = tomllib.loads(codex_update(text, entry, True))
        expected = {**before["mcp_servers"]["sharebus"], **entry}
        self.assertEqual(after["mcp_servers"]["sharebus"], expected)
        self.assertEqual(after["mcp_servers"]["other"], before["mcp_servers"]["other"])
        self.assertEqual(set(expected["tools"]), {"sharebus_send"})

    def test_auto_install_is_additive_bounded_and_does_not_grant_tool_permissions(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-auto-install-") as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            settings = home / ".claude/settings.json"
            settings.parent.mkdir()
            existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "keep-global"}]}]},
                        "permissions": {"allow": ["ExistingTool"]}}
            settings.write_text(json.dumps(existing), encoding="utf-8")
            gemini = home / ".gemini/antigravity-cli/settings.json"
            gemini.parent.mkdir(parents=True)
            gemini.write_text('{"permissions":{"allow":["ExistingTool"]}}', encoding="utf-8")
            original = gemini.read_bytes()
            plan = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)
            self.assertEqual(gemini.read_bytes(), original)
            self.assertNotIn(gemini, plan)
            self.assertFalse((workspace / ".sharebus").exists())
            global_hooks = json.loads(plan[settings])
            self.assertEqual(global_hooks["permissions"], existing["permissions"])
            self.assertEqual(global_hooks["hooks"]["SessionStart"][0], existing["hooks"]["SessionStart"][0])
            bootstrap = global_hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]
            self.assertIn("--bootstrap", bootstrap["args"])
            self.assertNotIn("--workspace", bootstrap["args"])
            for provider, path in (("claude", workspace / ".claude/settings.local.json"),
                                   ("codex", workspace / ".codex/hooks.json")):
                hooks = json.loads(plan[path])["hooks"]
                watchers = [handler for group in hooks["Stop"] for handler in group["hooks"] if "auto_watch.py" in str(handler)]
                self.assertEqual(len(watchers), 1)
                watcher = watchers[0]
                self.assertEqual(watcher["timeout"], 86415)
                self.assertIn("86400", str(watcher))
                self.assertEqual(watcher.get("asyncRewake", False), provider == "claude")
                self.assertNotIn("async", watcher)
                self.assertIn("Interrupt" if provider == "codex" else "SessionEnd", hooks)
            self.assertNotIn("tools", tomllib.loads(plan[home / ".codex/config.toml"])["mcp_servers"]["sharebus"])
            self.assertIn(workspace / "SHAREBUS.md", plan)
            self.assertIn(workspace / ".sharebus/team.json", plan)
            receipt = install_auto(workspace, home=home, python=Path(sys.executable))
            self.assertFalse(receipt["applied"])
            self.assertEqual(receipt["native_messages_sent"], 0)
            self.assertEqual(receipt["managed_workers_started"], 0)
            self.assertTrue(receipt["codex_hook_trust_review_required"])
            self.assertEqual(receipt["watch_timeout_seconds"], 86400)
            short = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, watch_timeout=1.25)
            short_hooks = json.loads(short[workspace / ".codex/hooks.json"])["hooks"]["Stop"]
            short_watch = next(handler for group in short_hooks for handler in group["hooks"] if "auto_watch.py" in str(handler))
            self.assertEqual(short_watch["timeout"], 17)
            self.assertIn("1.25", str(short_watch))
            apply(plan, root / "private-backups")
            repeated = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)
            for path in (settings, home / ".codex/hooks.json", workspace / ".claude/settings.local.json", workspace / ".codex/hooks.json"):
                self.assertEqual(json.loads(plan[path]), json.loads(repeated[path]))
            short = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, watch_timeout=1.25)
            apply(short, root / "short-backups")
            repeated_short = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)
            self.assertEqual(json.loads(repeated_short[workspace / ".sharebus/hooks-install.json"])["watch_timeout_seconds"], 1.25)
            for path in (workspace / ".claude/settings.local.json", workspace / ".codex/hooks.json"):
                stop_hooks = json.loads(repeated_short[path])["hooks"]["Stop"]
                watchers = [handler for group in stop_hooks for handler in group["hooks"] if "auto_watch.py" in str(handler)]
                self.assertEqual(len(watchers), 1)
                self.assertEqual(watchers[0]["timeout"], 17)

    def test_policy_plan_cas_rejects_edit_between_plan_and_installer_baseline(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-policy-cas-") as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            target = workspace / "SHAREBUS.md"
            target.write_text("before", encoding="utf-8")
            baseline = hashlib.sha256(target.read_bytes()).hexdigest()

            def policy_plan(*_args, **_kwargs):
                target.write_text("concurrent user edit", encoding="utf-8")
                return {"files": [{"path": str(target), "content": "prepared replacement", "current_hash": baseline}]}

            with patch("policy.install_policy", side_effect=policy_plan):
                plan = prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)
            with self.assertRaises(ValueError):
                apply(plan, root / "private-backups")
            self.assertEqual(target.read_text(encoding="utf-8"), "concurrent user edit")
            self.assertFalse((home / ".claude.json").exists())

    def test_prepare_is_readonly_apply_preserves_existing_and_keeps_private_backups(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-install-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            claude = home / ".claude.json"
            original = {"mcpServers": {"existing": {"command": "keep"}}, "other": {"value": 42}}
            claude.write_text(json.dumps(original), encoding="utf-8")
            codex = home / ".codex/config.toml"
            codex.parent.mkdir()
            codex.write_text('[model]\nname = "preserve"\n[mcp_servers.other]\ncommand = "keep"\n', encoding="utf-8")
            before = claude.read_bytes()
            plan = prepare(home, Path(sys.executable))
            self.assertEqual(claude.read_bytes(), before)
            self.assertFalse((home / ".agents/skills/sharebus/SKILL.md").exists())
            receipts = apply(plan, root / "private-backups")
            self.assertTrue(all(item["changed"] for item in receipts))
            after = json.loads(claude.read_text("utf-8"))
            after["mcpServers"].pop("sharebus")
            self.assertEqual(after, original)
            self.assertEqual(tomllib.loads(codex.read_text("utf-8"))["mcp_servers"]["other"]["command"], "keep")
            self.assertGreater(len(list((root / "private-backups").glob("*.bak"))), 0)
            self.assertFalse(any("@PYTHON@" in text or "@INSTALLER@" in text for text in plan.values()))

    def test_collision_refuses_without_replace_and_toml_preserves_others(self):
        text = '[mcp_servers.sharebus]\ncommand = "old"\n[mcp_servers.other]\ncommand = "keep"\n'
        entry = server_entry(Path(sys.executable), "codex")
        with self.assertRaises(ValueError):
            codex_update(text, entry, False)
        replaced = tomllib.loads(codex_update(text, entry, True))
        self.assertEqual(replaced["mcp_servers"]["other"]["command"], "keep")
        self.assertNotIn("tools", replaced["mcp_servers"]["sharebus"])
        self.assertEqual(replaced["mcp_servers"]["sharebus"]["command"], str(Path(sys.executable)))

    def test_apply_refuses_concurrent_configuration_update_before_any_writes(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-race-") as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            claude = home / ".claude.json"
            claude.write_text('{"other": "before"}', encoding="utf-8")
            plan = prepare(home, Path(sys.executable))
            claude.write_text('{"other": "concurrent update"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                apply(plan, root / "backup")
            self.assertEqual(json.loads(claude.read_text("utf-8"))["other"], "concurrent update")
            self.assertFalse((home / ".codex/config.toml").exists())

    def test_project_hooks_are_scoped_and_duplicate_free(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-hooks-") as directory:
            root = Path(directory)
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            first = prepare(home, Path(sys.executable), hooks_workspace=workspace)
            self.assertNotIn(home / ".codex/hooks.json", first)
            apply(first, root / "backup")
            second = prepare(home, Path(sys.executable), hooks_workspace=workspace)
            hooks = json.loads(second[workspace / ".codex/hooks.json"])
            self.assertEqual(len(hooks["hooks"]["UserPromptSubmit"]), 1)
            self.assertEqual(len(hooks["hooks"]["Stop"]), 1)

    def test_antigravity_sidecar_probe_install_preserves_plugins_and_other_hooks(self):
        with tempfile.TemporaryDirectory(prefix="sharebus-sidecar-") as directory:
            root = Path(directory)
            home = root / "home"
            config = home / ".gemini/config/config.json"
            config.parent.mkdir(parents=True)
            original = {"plugins": {"existing": {"enabled": True}}, "sidecars": {"other": {"enabled": False}}}
            config.write_text(json.dumps(original), encoding="utf-8")
            hooks = home / ".gemini/config/hooks.json"
            hooks.write_text('{"other-hook": {"SessionStart": []}}', encoding="utf-8")
            plan = prepare(home, Path(sys.executable), sidecar_probe_only=True)
            updated = json.loads(plan[config])
            self.assertEqual(updated["plugins"], original["plugins"])
            self.assertEqual(updated["sidecars"]["other"], original["sidecars"]["other"])
            self.assertTrue(updated["sidecars"]["sharebus"]["enabled"])
            sidecar = home / ".gemini/config/sidecars/sharebus/sidecar.json"
            worker = json.loads(plan[sidecar])
            self.assertEqual(worker["env"]["SHAREBUS_SIDECAR_PROBE_ONLY"], "1")
            self.assertEqual(worker["args"][-2:], ["--registry", str(sidecar.parent / "workspaces")])
            installed_hooks = json.loads(plan[hooks])
            self.assertIn("other-hook", installed_hooks)
            self.assertEqual(set(installed_hooks["sharebus-native-metadata"]), {"SessionStart", "PreInvocation", "Stop"})
            apply(plan, root / "backup")
            live = prepare(home, Path(sys.executable), sidecar_probe_only=False)
            self.assertEqual(json.loads(live[sidecar])["env"]["SHAREBUS_SIDECAR_PROBE_ONLY"], "0")
            apply(live, root / "backup2")
            repeated = prepare(home, Path(sys.executable))
            self.assertEqual(json.loads(repeated[sidecar])["env"]["SHAREBUS_SIDECAR_PROBE_ONLY"], "0")


if __name__ == "__main__":
    unittest.main()
