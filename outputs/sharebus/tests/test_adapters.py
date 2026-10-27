"""Native-wire contract checks using persistent child processes, without models."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adapters
import discovery
from adapters import NativeAdapter


WIRE_CLIENT = r'''
import json, os, sys, time
from pathlib import Path
provider, state_path, input_path, resume = sys.argv[1:]
state_path, input_path = Path(state_path), Path(input_path)
session = 'native-session-001'
memory = state_path.read_text() if state_path.exists() else ''
turn = 0
def emit(value):
    print(json.dumps(value), flush=True)
def record(value):
    with input_path.open('a') as f: f.write(json.dumps(value)+'\n')
def completed(text, denied=False, failed=False):
    if provider == 'codex':
        emit({'method':'item/completed','params':{'threadId':session,'turnId':str(turn),'item':{'id':'answer','type':'agentMessage','text':text}}})
        emit({'method':'turn/completed','params':{'threadId':session,'turn':{'id':str(turn),'status':'failed' if failed else 'completed'}}})
    elif provider == 'claude':
        emit({'type':'assistant','session_id':session,'message':{'content':[{'type':'thinking','thinking':'PRIVATE_THOUGHT'},{'type':'text','text':text}]}})
        emit({'type':'result','session_id':session,'subtype':'success','is_error':failed,'permission_denials':[{'tool_name':'Bash'}] if denied else [],'result':text})
    else:
        emit({'event':'step_update','step_update':{'conversation_id':session,'step_type':'thinking','text_delta':'PRIVATE_THOUGHT'}})
        emit({'event':'result','result':{'conversation_id':session,'status':'ERROR' if failed else 'SUCCESS','response':text,'denied_actions':['terminal'] if denied else []}})
for line in sys.stdin:
    value=json.loads(line)
    record(value)
    method=value.get('method')
    if method=='initialize':
        emit({'id':value['id'],'result':{'userAgent':'fake'}})
        continue
    if method=='account/read':
        emit({'id':value['id'],'result':{'account':{'type':os.environ.get('SHAREBUS_FAKE_AUTH','chatgpt'),'email':'PRIVATE_EMAIL','accessToken':'PRIVATE_TOKEN'}}})
        continue
    if method in ('thread/start','thread/resume'):
        time.sleep(float(os.environ.get('SHAREBUS_FAKE_STARTUP_DELAY', '0')))
        if method=='thread/resume' and value['params']['threadId']=='missing':
            emit({'id':value['id'],'error':{'code':-32000,'message':'missing'}})
        else:
            emit({'method':'thread/started','params':{'thread':{'id':session}}})
            emit({'id':value['id'],'result':{'thread':{'id':session}}})
        continue
    if method=='initialized': continue
    if method=='turn/interrupt':
        emit({'id':value['id'],'result':{}})
        continue
    if provider=='codex':
        if method!='turn/start': continue
        prompt=value['params']['input'][0]['text']
    else:
        prompt=value.get('message',{}).get('content','')
        if not prompt: continue
        if resume=='mismatch':
            emit({'type':'system','session_id':'different-native-session'})
            continue
        if provider=='claude': emit({'type':'system','subtype':'init','session_id':session})
    turn+=1
    if prompt=='timeout':
        time.sleep(10)
        continue
    if prompt=='oversized':
        emit({'ignored':'x'*1048577})
        continue
    if prompt=='early-burst':
        for i in range(8):
            emit({'method':'item/agentMessage/delta','params':{'threadId':session,'turnId':str(turn),'delta':'public'}})
    if prompt.startswith('remember:'):
        memory=prompt.split(':',1)[1]
        state_path.write_text(memory)
        text='remembered'
    else: text=memory or 'ready'
    if provider=='codex':
        # Events can race ahead of the turn/start RPC response.
        emit({'method':'item/agentMessage/delta','params':{'threadId':session,'turnId':str(turn),'itemId':'answer','delta':text}})
        emit({'id':value['id'],'result':{'turn':{'id':str(turn),'status':'inProgress'}}})
        emit({'method':'item/reasoning/summaryTextDelta','params':{'threadId':session,'turnId':str(turn),'delta':'PRIVATE_THOUGHT'}})
        if prompt in ('needs-permission','ask-question','elicitation','wrong-approval','cancel-approval'):
            params={'threadId':session,'turnId':str(turn),'command':'private-sensitive-command'}
            method='item/commandExecution/requestApproval'
            if prompt=='ask-question':
                method='item/tool/requestUserInput'
                params['questions']=[{'id':'q1','header':'Choice','question':'Which workspace?','options':[{'label':'A','description':'Use A','private':'PRIVATE_OPTION'}]}]
            elif prompt=='elicitation':
                method='mcpServer/elicitation/request'
                params.update({'mode':'form','message':'Select a name','requestedSchema':{'type':'object','properties':{'name':{'type':'string'}}}})
            elif prompt=='wrong-approval': params['threadId']='foreign-session'
            emit({'id':9001,'method':method,'params':params})
            if prompt=='cancel-approval':
                emit({'method':'serverRequest/resolved','params':{'threadId':session,'requestId':9001}})
            response=json.loads(sys.stdin.readline())
            record(response)
    elif provider=='claude' and prompt in ('ask-approval','ask-question'):
        tool='AskUserQuestion' if prompt=='ask-question' else 'Bash'
        data={'questions':[{'question':'Which workspace?','header':'Choice','options':[{'label':'A','description':'Use A'}],'multiSelect':False}]} if tool=='AskUserQuestion' else {'command':'private-sensitive-command'}
        emit({'type':'control_request','request_id':'native-request-1','request':{'subtype':'can_use_tool','tool_name':tool,'input':data}})
        response=json.loads(sys.stdin.readline())
        record(response)
    if prompt=='exit-after-write': sys.exit(0)
    print('PRIVATE_STDERR_TOKEN',file=sys.stderr,flush=True)
    completed(text, denied=prompt=='needs-permission', failed=prompt=='fail')
'''


@pytest.fixture
def wire(tmp_path, monkeypatch):
    script = tmp_path / "wire_client.py"
    script.write_text(WIRE_CLIENT, encoding="utf-8")
    state = tmp_path / "native-memory.txt"
    inputs = tmp_path / "native-input.jsonl"
    monkeypatch.setattr(adapters, "discover_native_sessions", lambda provider: [])

    def make(provider="codex", events=None, **options):
        adapter = NativeAdapter(provider, tmp_path, "test-agent", tmp_path / "agent.token", events, **options)
        monkeypatch.setattr(adapter, "_command", lambda resume: [sys.executable, str(script), provider, str(state), str(inputs), resume or ""])
        return adapter

    return make, inputs


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_two_turns_keep_native_process_context_and_idle_stdin(wire, provider):
    make, inputs = wire
    events = []
    adapter = make(provider, events.append)
    try:
        adapter.start()
        first_pid = adapter.pid
        assert first_pid
        initial = inputs.read_text() if inputs.exists() else ""
        time.sleep(0.08)
        assert (inputs.read_text() if inputs.exists() else "") == initial
        assert adapter.turn("remember:orange").success
        second = adapter.turn("what did I say?")
        assert second.success and second.text == "orange"
        assert adapter.pid == first_pid
        assert second.session_id == "native-session-001"
        assert "PRIVATE_THOUGHT" not in second.text
        assert [event["session_id"] for event in events if event["type"] == "session"] == ["native-session-001"]
        recorded = [json.loads(line) for line in inputs.read_text().splitlines()]
        turns = [item for item in recorded if item.get("method") == "turn/start" or item.get("type") == "user" or item.get("event") == "user"]
        assert len(turns) == 2
    finally:
        adapter.close()


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_restart_resumes_exact_native_conversation(wire, provider):
    make, _ = wire
    first = make(provider)
    first.start()
    assert first.turn("remember:violet").success
    native_id = first.session_id
    first_pid = first.pid
    first.close()
    resumed = make(provider)
    try:
        resumed.start(native_id)
        result = resumed.turn("remembered value?")
        assert result.success and result.text == "violet"
        assert result.session_id == native_id
        assert resumed.pid != first_pid
    finally:
        resumed.close()


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_permission_denial_is_needs_human_even_with_success_envelope(wire, provider):
    make, inputs = wire
    adapter = make(provider)
    try:
        result = adapter.turn("needs-permission")
        assert not result.success and result.needs_human
        if provider == "codex":
            assert result.delivery_unknown and result.status == "needs_human"
            responses = [json.loads(line) for line in inputs.read_text().splitlines()]
            assert not any(item.get("id") == 9001 and "result" in item for item in responses)
        else:
            assert "permission" in result.error.lower()
    finally:
        adapter.close()


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_failed_native_turn_cannot_be_reported_completed(wire, provider):
    make, _ = wire
    adapter = make(provider)
    try:
        result = adapter.turn("fail")
        assert not result.success
        assert result.error
    finally:
        adapter.close()


def test_codex_missing_resume_never_creates_fallback_thread(wire):
    make, inputs = wire
    adapter = make("codex")
    with pytest.raises(RuntimeError, match="rejected thread/resume"):
        adapter.start("missing")
    messages = [json.loads(line) for line in inputs.read_text().splitlines()]
    assert [item.get("method") for item in messages] == ["initialize", "initialized", "thread/resume"]
    assert adapter.pid is None


def test_codex_startup_has_configurable_deadline_and_closes_timed_out_transport(wire, monkeypatch):
    make, _ = wire
    monkeypatch.setenv("SHAREBUS_STARTUP_TIMEOUT", "0.03")
    monkeypatch.setenv("SHAREBUS_FAKE_STARTUP_DELAY", "0.10")
    short = make("codex")
    try:
        with pytest.raises(TimeoutError):
            short.start()
    finally:
        short.close()
    assert short.pid is None
    monkeypatch.setenv("SHAREBUS_STARTUP_TIMEOUT", "10")
    long = make("codex")
    try:
        assert long.start() == "native-session-001"
    finally:
        long.close()


def test_stream_resume_mismatch_is_rejected(wire):
    make, _ = wire
    adapter = make("claude")
    adapter.start("mismatch")
    result = adapter.turn("continue")
    assert not result.success and "different session ID" in result.error
    assert adapter.session_id is None
    assert adapter.pid is None


def test_busy_existing_native_session_is_not_cloned(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "discover_native_sessions", lambda _: [{"native_session_id": "ceo-existing", "status": "idle"}])
    adapter = NativeAdapter("claude", tmp_path, "test", tmp_path / "token")
    with pytest.raises(RuntimeError, match="active owner"):
        adapter.start("ceo-existing")
    assert adapter.pid is None


def test_timeout_closes_only_owned_child(wire):
    make, _ = wire
    adapter = make("antigravity")
    result = adapter.turn("timeout", timeout=0.05)
    assert not result.success and "timed out" in result.error
    assert adapter.pid is None


def test_interrupt_fences_result_and_preserves_native_id(wire):
    make, _ = wire
    adapter = make("claude")
    assert adapter.turn("remember:blue").success
    results = []
    runner = threading.Thread(target=lambda: results.append(adapter.turn("timeout", timeout=10)))
    runner.start()
    time.sleep(0.05)
    adapter.interrupt()
    runner.join(timeout=4)
    assert not runner.is_alive()
    assert not results[0].success
    assert adapter.session_id == "native-session-001"
    assert adapter.pid is None


def test_normal_claude_command_keeps_configured_tools_and_test_mode_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "resolve_executable", lambda _: sys.executable)
    monkeypatch.delenv("SHAREBUS_NATIVE_TOOLS", raising=False)
    adapter = NativeAdapter("claude", tmp_path, "test", tmp_path / "token")
    command = adapter._command(None)
    assert "--tools" not in command and "--strict-mcp-config" not in command
    assert "--no-session-persistence" not in command
    assert "--dangerously-skip-permissions" not in command
    assert "--permission-prompts" in command
    assert command[command.index("--permission-prompts") + 1] == "host"
    monkeypatch.setenv("SHAREBUS_NATIVE_TOOLS", "relay")
    restricted = adapter._command(None)
    assert restricted[restricted.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in restricted


def test_codex_config_uses_exact_bus_tools_without_bypass(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "resolve_executable", lambda _: sys.executable)
    adapter = NativeAdapter("codex", tmp_path, "test", tmp_path / "token")
    command = adapter._command("existing")
    assert command[1:4] == ["app-server", "--listen", "stdio://"]
    assert not any("dangerously" in argument or "ephemeral" in argument for argument in command)
    assert 'mcp_servers.sharebus.tools.sharebus_send.approval_mode="approve"' in command
    assert "mcp_servers.sharebus.required=true" in command
    assert "existing" not in command  # Resume is an explicit protocol request.


def test_codex_filtered_mcp_environment_preserves_managed_handoff(tmp_path, monkeypatch):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from daemon import BusHTTPServer

    monkeypatch.setattr(adapters, "resolve_executable", lambda _: sys.executable)
    server = BusHTTPServer(tmp_path, providers="none", enable_native=False, discovery=[])
    (tmp_path / ".sharebus/private/runtime.json").write_text(json.dumps(server.runtime()), encoding="utf-8")
    sender, token = server.store.register("codex", "CTO", mode="managed_new")
    recipient, _ = server.store.register("claude", "CMO")
    root = server.store.send("user", sender["id"], "Delegate the bounded task", "task")
    assert server.store.claim(sender["id"])
    token_file = tmp_path / ".sharebus/private/credentials" / (sender["id"] + ".token")
    adapter = NativeAdapter("codex", tmp_path, sender["id"], token_file)
    command = adapter._command(None)
    config = dict(argument.split("=", 1) for index, argument in enumerate(command) if index and command[index - 1] == "-c")
    forwarded = json.loads(config["mcp_servers.sharebus.env_vars"])
    assert set(forwarded) == {"SHAREBUS_MANAGED", "SHAREBUS_WORKSPACE", "SHAREBUS_AGENT_ID", "SHAREBUS_AGENT_TOKEN_FILE"}
    bindings = {"SHAREBUS_MANAGED": "1", "SHAREBUS_WORKSPACE": str(tmp_path),
                "SHAREBUS_AGENT_ID": sender["id"], "SHAREBUS_AGENT_TOKEN_FILE": str(token_file)}
    # Model Codex's default environment filtering, then apply the production whitelist.
    environment = {name: value for name, value in os.environ.items() if not name.startswith("SHAREBUS_")}
    environment.update({name: bindings[name] for name in forwarded})
    assert token not in command
    parameters = StdioServerParameters(command=json.loads(config["mcp_servers.sharebus.command"]),
        args=json.loads(config["mcp_servers.sharebus.args"]), env=environment)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()

    async def exercise():
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                joined = await session.call_tool("sharebus_join", {})
                assert not joined.isError
                identity = joined.structuredContent or json.loads(joined.content[0].text)
                assert identity["managed"] and identity["agent"]["id"] == sender["id"]
                assert "session_token" not in identity and token not in json.dumps(identity)
                sent = await session.call_tool("sharebus_send", {"to": recipient["id"], "body": "Investigate the reference", "kind": "task"})
                assert not sent.isError
                delegated = sent.structuredContent or json.loads(sent.content[0].text)
                assert delegated["root_id"] == root["root_id"]
                assert delegated["event"]["sender"] == sender["id"]
                assert len(server.store.agents()) == 2

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.close()


def test_discovery_never_claims_process_is_workspace_session(monkeypatch):
    monkeypatch.setattr(discovery, "resolve_executable", lambda provider: "/installed/" + provider)
    monkeypatch.setattr(discovery, "_running_processes", lambda: [{"pid": 101, "name": "Cursor.exe", "executable": "cursor.exe"}])
    monkeypatch.setattr(discovery, "discover_native_sessions", lambda *args: [])
    rows = discovery.discover()
    cursor = next(item for item in rows if item["provider"] == "cursor")
    assert cursor["installed"] and cursor["running"]
    assert not cursor["workspace_verified"] and not cursor["capability_verified"]
    assert not cursor["managed_supported"]
    assert all(not item["desktop_capabilities_verified"] for item in rows)


def test_claude_metadata_discovery_filters_workspace_without_transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "resolve_executable", lambda _: "claude.exe")
    monkeypatch.delenv("SHAREBUS_DISCOVERY_TIMEOUT", raising=False)
    calls = []
    def run(command, **kwargs):
        assert kwargs["timeout"] == 30
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps([
            {"sessionId": "role-ceo", "name": "CEO", "cwd": str(tmp_path), "pid": 55, "status": "idle", "kind": "background", "waitingFor": None, "private": "do not export"},
            {"sessionId": "other-session", "cwd": str(tmp_path / "other"), "status": "busy"},
        ]), "")
    monkeypatch.setattr(discovery.subprocess, "run", run)
    rows = discovery.discover_native_sessions("claude", tmp_path)
    assert calls == [["claude.exe", "agents", "--json"]]
    assert len(rows) == 1 and rows[0]["native_session_id"] == "role-ceo"
    assert rows[0]["requires_attach"] and "private" not in rows[0]


def test_unsupported_provider_does_not_create_native_process(tmp_path):
    with pytest.raises(ValueError, match="no verified native session adapter"):
        NativeAdapter("cursor", tmp_path, "test", tmp_path / "token")


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
def test_only_selected_public_output_is_observed_and_raw_logs_are_not_created(wire, provider):
    make, _ = wire
    events = []
    adapter = make(provider, events.append)
    try:
        result = adapter.turn("public result")
        assert result.success and result.input_attempted and result.accepted
        assert result.status == "completed" and not result.delivery_unknown
        if provider == "codex":
            assert result.native_turn_id == "1"
        else:
            assert result.native_turn_id is None  # Do not invent an ID from a message UUID.
        assert any(item["type"] == "input_attempt" for item in events)
        assert any(item["type"] == "assistant_text" and item["text"] == "ready" for item in events)
        serialized = json.dumps(events)
        assert "PRIVATE_" not in serialized
        assert set().union(*(set(event) for event in events)) <= {
            "type", "provider", "session_id", "turn_id", "status", "text", "reason"}
        assert not list(adapter._logs.glob("*stdout*"))
        assert not list(adapter._logs.glob("*stderr*"))
        assert not list(adapter._logs.glob("*native.log"))
    finally:
        adapter.close()


def test_failed_durable_input_receipt_prevents_native_write(wire):
    make, inputs = wire
    def observer(event):
        if event["type"] == "input_attempt":
            raise ValueError("PRIVATE_RECEIPT_ERROR")
    adapter = make("codex", observer)
    try:
        result = adapter.turn("must not reach stdin")
        assert not result.success and not result.input_attempted and not result.delivery_unknown
        assert result.status == "not_sent" and "PRIVATE_" not in result.error
        assert not any(json.loads(line).get("method") == "turn/start" for line in inputs.read_text().splitlines())
    finally:
        adapter.close()


@pytest.mark.parametrize("prompt", ["timeout", "exit-after-write", "oversized", "early-burst"])
def test_post_write_failure_is_unknown_and_same_adapter_never_replays(wire, monkeypatch, prompt):
    make, inputs = wire
    adapter = make("codex")
    if prompt == "early-burst":
        monkeypatch.setattr(adapters, "MAX_PENDING", 2)
    try:
        adapter.start()
        result = adapter.turn(prompt, timeout=0.2 if prompt == "timeout" else 3)
        assert not result.success and result.input_attempted and result.delivery_unknown
        assert result.status == "delivery_unknown"
        before = inputs.read_text()
        refused = adapter.turn("do not replay")
        assert refused.delivery_unknown and not refused.input_attempted
        assert inputs.read_text() == before
        assert len([json.loads(line) for line in before.splitlines() if json.loads(line).get("method") == "turn/start"]) == 1
    finally:
        adapter.close()


def test_bounded_queue_stops_without_dropping_silently(tmp_path, monkeypatch):
    import io
    import queue
    monkeypatch.setattr(adapters, "QUEUE_TIMEOUT", 0.01)
    adapter = NativeAdapter("codex", tmp_path, "test", tmp_path / "token")
    messages = queue.Queue(maxsize=1)
    state = {}
    line = json.dumps({"id": 1, "result": {}}) + "\n"
    adapter._reader(None, messages, io.StringIO(line * 3), True, state)
    assert messages.qsize() == 1
    assert "queue exceeded" in state["error"] and state["eof"]


@pytest.mark.parametrize("timeout", [float("inf"), float("nan"), 0, -1, 3601])
def test_turn_timeout_is_finite_and_bounded(wire, timeout):
    make, inputs = wire
    adapter = make()
    with pytest.raises(ValueError):
        adapter.turn("never launch", timeout=timeout)
    assert not inputs.exists() and adapter.pid is None


def test_input_and_output_are_bounded(wire, monkeypatch):
    make, _ = wire
    adapter = make("codex")
    with pytest.raises(ValueError):
        adapter.turn("x" * (adapters.MAX_PROMPT_CHARS + 1))
    monkeypatch.setattr(adapters, "MAX_OUTPUT_CHARS", 8)
    try:
        result = adapter.turn("remember:" + "x" * 30)
        assert result.delivery_unknown and "size limit" in result.error
    finally:
        adapter.close()


@pytest.mark.parametrize("provider,prompt,response", [
    ("codex", "needs-permission", {"decision": "accept"}),
    ("codex", "ask-question", {"answers": {"q1": {"answers": ["A"]}}}),
    ("codex", "elicitation", {"action": "accept", "content": {"name": "A"}}),
    ("claude", "ask-approval", {"behavior": "allow"}),
    ("claude", "ask-question", {"behavior": "allow", "answers": {"Which workspace?": "A"}}),
])
def test_exact_human_approval_is_routed_once_without_a_new_turn(wire, provider, prompt, response):
    make, inputs = wire
    views = []
    def approval(view):
        views.append(view)
        assert adapter.pending_approvals() == [view]
        with pytest.raises(ValueError):
            adapter.respond_approval("wrong-request", response)
        if prompt == "ask-question" and provider == "codex":
            assert view["details"]["questions"][0]["options"] == [{"label": "A", "description": "Use A"}]
            with pytest.raises(ValueError):
                adapter.respond_approval(view["approval_id"], {"answers": {}})
        adapter.respond_approval(view["approval_id"], response)
        with pytest.raises(ValueError):
            adapter.respond_approval(view["approval_id"], response)
    adapter = make(provider, on_approval=approval)
    try:
        result = adapter.turn(prompt)
        assert result.success and not result.needs_human and not result.delivery_unknown
        assert len(views) == 1 and not adapter.pending_approvals()
        recorded = [json.loads(line) for line in inputs.read_text().splitlines()]
        native_replies = [row for row in recorded if row.get("id") == 9001 or row.get("type") == "control_response"]
        assert len(native_replies) == 1
        if provider == "codex":
            assert native_replies[0] == {"id": 9001, "result": response}
        else:
            assert native_replies[0]["response"]["request_id"] == "native-request-1"
            assert native_replies[0]["response"]["response"]["updatedInput"].get("command", "private-sensitive-command") == "private-sensitive-command"
        assert len([row for row in recorded if row.get("method") == "turn/start" or row.get("type") == "user"]) == 1
    finally:
        adapter.close()


def test_unanswered_or_foreign_approval_is_not_a_denial_or_empty_answer(wire):
    make, inputs = wire
    adapter = make("codex", on_approval=lambda view: None)
    try:
        adapter.start()
        result = adapter.turn("needs-permission", timeout=0.2)
        assert result.needs_human and result.delivery_unknown and result.status == "needs_human"
        assert not adapter.pending_approvals()
        assert not any(json.loads(line).get("id") == 9001 for line in inputs.read_text().splitlines())
    finally:
        adapter.close()


def test_native_approval_requires_exact_session_and_turn(wire):
    make, _ = wire
    views = []
    adapter = make("codex", on_approval=views.append)
    try:
        result = adapter.turn("wrong-approval")
        assert result.needs_human and result.delivery_unknown
        assert not views
    finally:
        adapter.close()


@pytest.mark.parametrize("auth_type", ["apiKey", "amazonBedrock", "chatgpt"])
def test_subscription_only_codex_checks_native_account_before_model_input(wire, monkeypatch, auth_type):
    make, inputs = wire
    events = []
    monkeypatch.setenv("SHAREBUS_FAKE_AUTH", auth_type)
    adapter = make("codex", events.append, subscription_only=True)
    try:
        result = adapter.turn("native subscription only")
        assert result.success is (auth_type == "chatgpt")
        recorded = [json.loads(line) for line in inputs.read_text().splitlines()]
        account_index = next(i for i, item in enumerate(recorded) if item.get("method") == "account/read")
        turns = [i for i, item in enumerate(recorded) if item.get("method") == "turn/start"]
        assert (turns and turns[0] > account_index) if auth_type == "chatgpt" else not turns
        assert "PRIVATE_" not in json.dumps(events)
        if auth_type != "chatgpt":
            assert not result.input_attempted and not result.delivery_unknown and result.needs_human
    finally:
        adapter.close()


@pytest.mark.parametrize("method", ["api_key", "api_key_helper", "third_party", "claude.ai", "oauth_token"])
def test_claude_subscription_guard_uses_only_auth_metadata(tmp_path, monkeypatch, method):
    captured = []
    def run(command, **kwargs):
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"loggedIn": True, "authMethod": method,
            "email": "PRIVATE_EMAIL", "token": "PRIVATE_TOKEN"}), "PRIVATE_STDERR")
    monkeypatch.setattr(adapters.subprocess, "run", run)
    adapter = NativeAdapter("claude", tmp_path, "test", tmp_path / "token", subscription_only=True)
    if method in {"claude.ai", "oauth_token"}:
        adapter._check_subscription("verified-claude.exe", {})
    else:
        with pytest.raises(adapters.NativeNeedsHuman) as result:
            adapter._check_subscription("verified-claude.exe", {})
        assert "PRIVATE_" not in str(result.value)
    assert captured == [["verified-claude.exe", "auth", "status"]]
    assert not list(adapter._logs.iterdir())


def test_write_timeout_terminates_only_the_owned_transport(tmp_path, monkeypatch):
    finished = threading.Event()
    class Input:
        def write(self, value):
            finished.wait(5)
            raise OSError("closed")
        def flush(self):
            pass
        def close(self):
            finished.set()
    class Owned:
        stdin = Input()
        pid = 123
        terminated = 0
        def poll(self):
            return 0 if finished.is_set() else None
        def terminate(self):
            self.terminated += 1
            finished.set()
        def wait(self, timeout):
            assert finished.wait(timeout)
    adapter = NativeAdapter("antigravity", tmp_path, "test", tmp_path / "token")
    adapter._process = owned = Owned()
    monkeypatch.setattr(adapters, "WRITE_TIMEOUT", 0.02)
    began = time.monotonic()
    result = adapter.turn("will not be read", timeout=1)
    assert result.delivery_unknown and result.input_attempted
    assert owned.terminated == 1 and time.monotonic() - began < 1


def test_native_cleared_approval_cannot_receive_a_late_human_answer(wire):
    make, inputs = wire
    adapter = make("codex", on_approval=lambda view: None)
    try:
        result = adapter.turn("cancel-approval", timeout=2)
        assert result.needs_human and result.delivery_unknown
        assert "no longer active" in result.error
        assert not any(json.loads(line).get("id") == 9001 for line in inputs.read_text().splitlines())
    finally:
        adapter.close()


def test_agy_workspace_mcp_uses_inherited_identity_and_preserves_existing_config(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "resolve_executable", lambda _: sys.executable)
    adapter = NativeAdapter("antigravity", tmp_path, "test-agent", tmp_path / "private-token")
    command = adapter._command(None)
    config_path = tmp_path / ".agents/mcp_config.json"
    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["sharebus"]["command"] == str(Path(sys.executable).resolve())
    serialized = json.dumps(config)
    assert "test-agent" not in serialized and "private-token" not in serialized and '"env"' not in serialized
    assert "--log-file" not in command
    original = config_path.read_bytes()
    adapter._command("observed-exact-id")
    assert config_path.read_bytes() == original
    config_path.write_text('{"mcpServers":{"other":{"command":"original"}}}')
    original = config_path.read_bytes()
    with pytest.raises(RuntimeError, match="compatible Sharebus"):
        adapter._command(None)
    assert config_path.read_bytes() == original


def test_native_config_write_refuses_replaced_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "resolve_executable", lambda _: sys.executable)
    adapter = NativeAdapter("antigravity", tmp_path, "test-agent", tmp_path / "token")
    previous = tmp_path.with_name(tmp_path.name + "-previous")
    tmp_path.rename(previous)
    tmp_path.mkdir()
    with pytest.raises(RuntimeError, match="directory has changed"):
        adapter._command(None)
    assert not (tmp_path / ".agents").exists()


def test_agy_config_hardlink_is_not_followed_or_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(adapters, "resolve_executable", lambda _: sys.executable)
    adapter = NativeAdapter("antigravity", tmp_path, "test-agent", tmp_path / "token")
    original = tmp_path / "original.json"
    original.write_text("preserve this original")
    (tmp_path / ".agents").mkdir()
    os.link(original, tmp_path / ".agents/mcp_config.json")
    with pytest.raises(RuntimeError, match="ordinary file"):
        adapter._command(None)
    assert original.read_text() == "preserve this original"


def test_subscription_child_environment_drops_api_and_ambient_harness_identity(wire, monkeypatch):
    make, _ = wire
    captured = []
    popen = adapters.subprocess.Popen
    def launch(*args, **kwargs):
        captured.append(kwargs["env"])
        return popen(*args, **kwargs)
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE_API_KEY")
    monkeypatch.setenv("GEMINI_API_KEY", "PRIVATE_GEMINI_KEY")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("AGY_ADC_AUTH", "true")
    monkeypatch.setenv("CODEX_THREAD_ID", "ambient-parent-thread")
    monkeypatch.setattr(adapters.subprocess, "Popen", launch)
    adapter = make("codex", subscription_only=True)
    try:
        assert adapter.turn("safe native subscription").success
        assert len(captured) == 1
        assert not {"OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "AGY_ADC_AUTH", "CODEX_THREAD_ID"} & set(captured[0])
        assert captured[0]["SHAREBUS_AGENT_ID"] == adapter.agent_id
        assert os.environ["OPENAI_API_KEY"] == "PRIVATE_API_KEY"
    finally:
        adapter.close()


@pytest.mark.parametrize("request_turn", ["old-turn", "acknowledged-turn"])
def test_approval_before_start_ack_is_bound_only_after_authoritative_ack(tmp_path, request_turn):
    events, views, writes = [], [], []
    def present(view):
        views.append(view)
        assert adapter._turn_id == "acknowledged-turn"
        assert any(event.get("type") == "turn" and event.get("turn_id") == "acknowledged-turn" for event in events)
        adapter.respond_approval(view["approval_id"], {"decision": "accept"})
    adapter = NativeAdapter("codex", tmp_path, "test", tmp_path / "token", events.append, on_approval=present)
    adapter.session_id = "exact-thread"
    adapter._input_attempted = True
    adapter._deadline = time.monotonic() + 2
    adapter._write = lambda message, **kwargs: writes.append(message)
    for event in [
        {"id": 7001, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "exact-thread", "turnId": request_turn, "command": "bounded test command"}},
        {"id": 1, "result": {"turn": {"id": "acknowledged-turn"}}},
        {"method": "turn/completed", "params": {"threadId": "exact-thread",
            "turn": {"id": "acknowledged-turn", "status": "completed"}}},
    ]:
        adapter._messages.put(event)
    if request_turn == "old-turn":
        with pytest.raises(adapters.NativeNeedsHuman, match="current session and turn"):
            adapter._codex_turn("new input", 2)
        assert not views and not any(message.get("id") == 7001 for message in writes)
    else:
        assert adapter._codex_turn("new input", 2).success
        assert len(views) == 1
        assert [message for message in writes if message.get("id") == 7001] == [{"id": 7001, "result": {"decision": "accept"}}]
    assert adapter._turn_id == "acknowledged-turn"
