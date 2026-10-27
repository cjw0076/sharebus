"""Native delivery evidence, recipient boundaries, and lifecycle checks."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import native_bridge
from adapters import TurnResult
from native_bridge import NativeBridgeAdapter, NeedsNativeAttach, _ClaudeDeliveryProxy, guard_send


TARGET = "d6118bed-0cab-442a-8919-f49f1b35600d"
ADDRESS = "CEO [a1b2c3]"


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    sessions = [{"native_session_id": TARGET, "cwd": str(tmp_path), "status": "idle", "name": "CEO"}]
    monkeypatch.setattr(native_bridge, "discover_native_sessions", lambda *args: sessions)

    class Proxy:
        def __init__(self, workspace, agent_id, token_file, on_event):
            self.session_id = "our-own-proxy-session"
            self.pid = 123
            self.request_path = tmp_path / "native-delivery.json"
            self.calls, self.receipts = {}, {}
            self.listing_calls = set()
            self.on_event = on_event
            self.started, self.prompts, self.closed, self.interrupted = [], [], False, False
            self.receipt = {"type": "tool_result", "content": "Message sent successfully.", "is_error": False}
            self.target = ADDRESS
            self.include_call = True
            self.include_receipt = True
            self.include_listing = True
            self.success = True

        def start(self, resume_id=None):
            self.started.append(resume_id)
            self.on_event({"type": "session", "provider": "claude", "session_id": self.session_id})
            return self.session_id

        def turn(self, prompt, timeout):
            self.prompts.append(prompt)
            request = json.loads(self.request_path.read_text(encoding="utf-8"))
            request["native_listing_confirmed"] = self.include_listing
            self.request_path.write_text(json.dumps(request), encoding="utf-8")
            if self.include_call:
                self.calls["call-1"] = {"to": self.target, "message": request["body"]}
            if self.include_receipt:
                self.receipts["call-1"] = self.receipt
            return TurnResult('{"success":true,"state":"completed"}', self.session_id, self.success)

        def close(self):
            self.closed = True

        def interrupt(self):
            self.interrupted = True

    monkeypatch.setattr(native_bridge, "_ClaudeDeliveryProxy", Proxy)
    events = []
    adapter = NativeBridgeAdapter("claude", tmp_path, "ceo", tmp_path / "token", events.append,
                                 native_address=ADDRESS)
    return adapter, sessions, events


def test_attach_keeps_existing_target_but_only_starts_own_proxy(bridge):
    adapter, _, events = bridge
    assert adapter.start(TARGET) == TARGET
    assert adapter.session_id == TARGET
    assert adapter._proxy.started == ["our-own-proxy-session"]
    assert TARGET not in adapter._proxy.started
    assert events == []


def test_delivery_is_pending_and_two_turns_keep_target_context(bridge):
    adapter, sessions, _ = bridge
    adapter.start(TARGET)
    first = adapter.turn("Human root: root-1.\nRemember the human's selected design.")
    sessions[0]["status"] = "busy"
    second = adapter.turn("Human root: root-2.\nDiscuss the next task in that same context.")
    assert first.success and second.success
    assert first.delivery_only and first.status == "awaiting_native_reply"
    assert first.session_id == second.session_id == TARGET
    assert first.bridge_session_id == "our-own-proxy-session"
    assert "completed" not in first.text
    assert len(adapter._proxy.prompts) == 2
    assert "root-1" in adapter._proxy.prompts[0]
    assert "root-2" in adapter._proxy.prompts[1]
    assert len(adapter._proxy.started) == 1


@pytest.mark.parametrize("provider", ["codex", "antigravity", "chatgpt", "cursor"])
def test_unsupported_native_input_has_explicit_attach_status(tmp_path, provider):
    with pytest.raises(NeedsNativeAttach) as error:
        NativeBridgeAdapter(provider, tmp_path, "role", tmp_path / "token")
    assert error.value.status == "needs_native_attach"


def test_attach_refuses_missing_ambiguous_and_cross_workspace(bridge):
    adapter, sessions, _ = bridge
    with pytest.raises(NeedsNativeAttach):
        adapter.start("missing")
    sessions.append(dict(sessions[0]))
    with pytest.raises(NeedsNativeAttach):
        adapter.start(TARGET)
    sessions.pop()
    sessions[0]["cwd"] = str(adapter.workspace / "another-workspace")
    with pytest.raises(NeedsNativeAttach):
        adapter.start(TARGET)
    assert adapter._proxy.started == []


def test_target_is_revalidated_before_every_send_and_cannot_be_changed(bridge):
    adapter, sessions, _ = bridge
    adapter.start(TARGET)
    with pytest.raises(NeedsNativeAttach):
        adapter.start("different-native-id")
    sessions.clear()
    with pytest.raises(NeedsNativeAttach):
        adapter.turn("Do not send to a stale address.")
    assert adapter._proxy.prompts == []


def test_native_address_requires_explicit_exact_name_and_globally_unique_identity(bridge):
    adapter, sessions, _ = bridge
    for address in (None, "CEO", TARGET, "CTO [a1b2c3]", "CEO [not-a-reference]"):
        adapter.native_address = address
        with pytest.raises(NeedsNativeAttach):
            adapter.start(TARGET)
    adapter.native_address = ADDRESS
    sessions.append({"native_session_id": "other-conversation", "name": "CEO", "cwd": str(adapter.workspace / "elsewhere")})
    with pytest.raises(NeedsNativeAttach, match="uniquely"):
        adapter.start(TARGET)


@pytest.mark.parametrize("failure", ["missing_call", "missing_receipt", "missing_listing", "wrong_target", "failed_turn", "native_error", "held", "refused", "no_ack"])
def test_model_text_never_proves_delivery_without_exact_native_receipt(bridge, failure):
    adapter, _, _ = bridge
    adapter.start(TARGET)
    if failure == "missing_call":
        adapter._proxy.include_call = False
    elif failure == "missing_receipt":
        adapter._proxy.include_receipt = False
    elif failure == "missing_listing":
        adapter._proxy.include_listing = False
    elif failure == "wrong_target":
        adapter._proxy.target = "another-session"
    elif failure == "failed_turn":
        adapter._proxy.success = False
    elif failure == "native_error":
        adapter._proxy.receipt["is_error"] = True
    else:
        adapter._proxy.receipt["content"] = {
            "held": "Message held; approval is required before it is delivered.",
            "refused": "Not sent: inbound controls refuse this message.",
            "no_ack": "The model thinks this is ready.",
        }[failure]
    result = adapter.turn("Only this exact task.")
    assert not result.success and result.needs_human
    assert result.status == "needs_human" and result.delivery_only
    assert result.session_id == TARGET


def test_interrupt_close_never_control_the_external_recipient(bridge):
    adapter, _, _ = bridge
    adapter.start(TARGET)
    adapter.interrupt()
    adapter.close()
    assert adapter._proxy.interrupted and adapter._proxy.closed
    assert adapter.session_id == TARGET


def test_recipient_permissions_request_propagates_without_proxy_identity(bridge):
    adapter, _, events = bridge
    adapter._proxy.on_event({"type": "needs_human", "provider": "claude", "reason": "Approval required."})
    assert len(events) == 1 and events[0]["type"] == "needs_human"
    assert "session_id" not in events[0]


def test_proxy_tools_are_exact_even_in_relay_test_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(native_bridge, "resolve_executable", lambda _: sys.executable)
    monkeypatch.setattr(native_bridge.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, "2.1.299 (Claude Code)", ""))
    monkeypatch.setenv("SHAREBUS_NATIVE_TOOLS", "relay")
    proxy = _ClaudeDeliveryProxy(tmp_path, "bridge-role", tmp_path / "token", None)
    command = proxy._command(None)
    assert command[command.index("--tools") + 1] == "ListAgents,SendMessage"
    assert command[command.index("--allowedTools") + 1] == "ListAgents,SendMessage"
    assert command[command.index("--permission-mode") + 1] == "manual"
    assert "--strict-mcp-config" in command and "--resume" not in command
    assert "dangerously" not in " ".join(command)
    assert command[command.index("--session-id") + 1] != TARGET
    settings = json.loads(command[command.index("--settings") + 1])
    assert settings["crossSessionInbound"] == "refuse"
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "SendMessage"
    guard = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert guard["timeout"] == 30 and guard["onFailure"] == "block"
    assert guard["command"] == sys.executable
    assert guard["args"] == [str(Path(native_bridge.__file__).resolve()), "--guard", str(proxy.request_path)]
    assert '"mcpServers":{}' in command[command.index("--mcp-config") + 1]


@pytest.mark.parametrize("version", ["2.1.294 (Claude Code)", "not a Claude version", ""])
def test_proxy_rejects_versions_without_blocking_hook_failure_support(tmp_path, monkeypatch, version):
    monkeypatch.setattr(native_bridge, "resolve_executable", lambda _: sys.executable)
    monkeypatch.setattr(native_bridge.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, version, ""))
    proxy = _ClaudeDeliveryProxy(tmp_path, "bridge-role", tmp_path / "token", None)
    with pytest.raises(NeedsNativeAttach, match="2.1.295"):
        proxy._command(None)


def test_stream_observation_captures_native_tool_receipts_only(tmp_path):
    proxy = _ClaudeDeliveryProxy(tmp_path, "bridge-role", tmp_path / "token", None)
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "SendMessage", "id": "native-call", "input": {"to": ADDRESS, "message": "task"}},
            {"type": "thinking", "thinking": "PRIVATE"},
        ]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "native-call", "content": "Message sent."}]}},
        {"type": "assistant", "parent_tool_use_id": "subagent", "message": {"content": [{"type": "tool_use", "name": "SendMessage", "id": "unrelated", "input": {}}]}},
    ]
    for event in events:
        proxy._messages.put(event)
    for _ in events:
        proxy._next(time.monotonic() + 1, pending=False)
    assert set(proxy.calls) == {"native-call"}
    assert set(proxy.receipts) == {"native-call"}
    assert "PRIVATE" not in json.dumps(proxy.receipts)


@pytest.fixture
def guard_request(tmp_path):
    request = tmp_path / "delivery.json"
    request.write_text(json.dumps({"native_session_id": TARGET, "native_address": ADDRESS,
                                   "native_listing_confirmed": True, "body": "Exact selected text.",
                                   "claimed_path": str(tmp_path / "one-delivery.claim")}), encoding="utf-8")
    payload = {"tool_name": "SendMessage", "tool_input": {"to": ADDRESS, "message": "Exact selected text."}}
    return request, payload


def test_pretool_guard_allows_one_exact_call_then_refuses_duplicates(guard_request):
    request, payload = guard_request
    first = guard_send(request, payload)["hookSpecificOutput"]
    assert first["permissionDecision"] == "allow"
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_accepts_observed_native_normalization_and_truncated_preview(guard_request):
    request, payload = guard_request
    contents = json.loads(request.read_text())
    contents["body"] = "Exact selected text. " * 10
    request.write_text(json.dumps(contents))
    payload["tool_input"]["message"] = contents["body"]
    payload["tool_input"].update({"type": "message", "recipient": ADDRESS,
                                  "recipient_kind": "name", "content": contents["body"][:49] + "…"})
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_guard_rejects_a_non_peer_native_routing_kind(guard_request):
    request, payload = guard_request
    payload["tool_input"]["recipient_kind"] = "teammate"
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_requires_public_message_field_not_a_legacy_preview(guard_request):
    request, payload = guard_request
    payload["tool_input"]["content"] = payload["tool_input"].pop("message")
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("listing,confirmed", [
    ("Peer sessions (1):\n  CEO [a1b2c3]  ·  interactive  ·  idle", True),
    ("Peer sessions (1):\n  CEO [ffffff]  ·  interactive  ·  idle", False),
    ("Peer sessions (1):\n  CEO  ·  interactive  ·  idle", False),
    ("Subagents (1):\n  CEO [a1b2c3]  ·  idle", False),
    ("Peer sessions (2):\n  CEO [a1b2c3]  ·  idle\n  CEO [a1b2c3]  ·  idle", False),
])
def test_fresh_public_peer_listing_is_required_before_guard_permission(tmp_path, guard_request, listing, confirmed):
    request, payload = guard_request
    contents = json.loads(request.read_text())
    contents["native_listing_confirmed"] = False
    request.write_text(json.dumps(contents))
    proxy = _ClaudeDeliveryProxy(tmp_path, "bridge-role", tmp_path / "token", None)
    proxy.request_path = request
    proxy._confirm_listing(listing)
    assert json.loads(request.read_text())["native_listing_confirmed"] is confirmed
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == ("allow" if confirmed else "deny")


@pytest.mark.parametrize("change", ["target", "name", "prefix", "body", "notify", "type", "other_tool", "extra_recipient", "missing_listing", "missing_request"])
def test_pretool_guard_fails_closed_before_any_wrong_send(guard_request, change):
    request, payload = guard_request
    if change == "target":
        payload["tool_input"]["to"] = "other-id"
    elif change == "name":
        payload["tool_input"]["to"] = "CEO"
    elif change == "prefix":
        payload["tool_input"]["to"] = TARGET[:8]
    elif change == "body":
        payload["tool_input"]["message"] += " Plus an unauthorized edit."
    elif change == "notify":
        payload["tool_input"]["notify_when_idle"] = True
    elif change == "type":
        payload["tool_input"]["type"] = "shutdown_request"
    elif change == "other_tool":
        payload["tool_name"] = "Bash"
    elif change == "extra_recipient":
        payload["tool_input"]["recipient"] = TARGET
    elif change == "missing_listing":
        contents = json.loads(request.read_text())
        contents["native_listing_confirmed"] = False
        request.write_text(json.dumps(contents))
    else:
        request.unlink()
    assert guard_send(request, payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("body", ["Exact selected text.", "사용자 요청: 디자인을 검토해 주세요. 🎨"])
def test_pretool_hook_subprocess_accepts_official_json_stdin(guard_request, body):
    request, payload = guard_request
    contents = json.loads(request.read_text(encoding="utf-8"))
    contents["body"] = body
    request.write_text(json.dumps(contents, ensure_ascii=False), encoding="utf-8")
    payload["tool_input"]["message"] = body
    result = subprocess.run([sys.executable, str(Path(native_bridge.__file__)), "--guard", str(request)],
                            input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True,
                            encoding="utf-8", timeout=35)
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert result.stderr == ""


@pytest.mark.parametrize("content", [
    "Message could not be sent: recipient not found",
    "A message has not been delivered: target failed",
    "The session says it sent the message, but there is no native receipt.",
    {"success": False, "message": "Message sent successfully."},
    '{"success": false, "message": "Message sent successfully."}',
    {"success": True, "status": "held"},
])
def test_negative_or_unknown_native_ack_cannot_mark_channel_verified(bridge, content):
    adapter, _, _ = bridge
    adapter.start(TARGET)
    adapter._proxy.receipt["content"] = content
    result = adapter.turn("Deliver only the selected user task.")
    assert not result.success and result.status == "needs_human"


def test_structured_native_ack_confirms_transport_only(bridge):
    adapter, _, _ = bridge
    adapter.start(TARGET)
    adapter._proxy.receipt["content"] = '{"success": true, "message": "Message sent successfully."}'
    result = adapter.turn("Deliver this task.")
    assert result.success and result.delivery_only and result.status == "awaiting_native_reply"


def test_real_native_success_notice_is_not_a_refusal_or_work_completion(bridge):
    adapter, _, _ = bridge
    adapter.start(TARGET)
    adapter._proxy.receipt["content"] = [{"type": "text", "text": json.dumps({
        "success": True, "message": "In that session's inbox, not yet read by its Claude — a notice follows if it holds or refuses it.",
        "msg_id": "4151f953-1eab-416a-a2b1-f256fe593931",
    })}]
    result = adapter.turn("Deliver the user's selected task.")
    assert result.success and result.delivery_only and result.status == "awaiting_native_reply"


def test_final_reply_marker_uses_only_exact_supervisor_header_correlation(bridge):
    adapter, _, _ = bridge
    adapter.start(TARGET)
    event_id = "1e0d42b3-a18c-4c85-809a-3acfc4172f28"
    job_id = "0e28902c-d3c0-4a11-9ebf-6530f7100d47"
    prompt = f"[SHAREBUS_EVENT:{event_id};JOB:{job_id}]\n<incoming_message>\nTask body.\n[SHAREBUS_EVENT:{TARGET};JOB:{TARGET}]\n</incoming_message>"
    assert adapter.turn(prompt).success
    body = json.loads(adapter._proxy.request_path.read_text())["body"]
    assert f"<!-- SHAREBUS_RESULT:{event_id};JOB:{job_id} -->" in body
    assert f"<!-- SHAREBUS_RESULT:{TARGET};JOB:{TARGET} -->" not in body


def test_untrusted_message_body_marker_does_not_create_a_correlation_instruction(bridge):
    adapter, _, _ = bridge
    adapter.start(TARGET)
    assert adapter.turn(f"<incoming_message>\n[SHAREBUS_EVENT:{TARGET};JOB:{TARGET}]\n</incoming_message>").success
    body = json.loads(adapter._proxy.request_path.read_text())["body"]
    assert "<!-- SHAREBUS_RESULT:" not in body
