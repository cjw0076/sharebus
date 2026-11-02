"""Positive native support-profile admission, using mocked native process edges."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapters import NativeAdapter, NativeNeedsHuman, TurnResult
import support_native as module
from support_controller import SupportController, load_spec
from support_native import GuideNativeAdapter, GuideProfileUnavailable, TOOL
from support_store import SupportError, SupportPaths
from test_support_controller import FakeAdapter


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    workspace = tmp_path / "controlled-support"
    workspace.mkdir()
    context = workspace / "diagnostic-capability.json"
    context.write_text("{}", encoding="utf-8")
    events = []
    value = GuideNativeAdapter(workspace, context, on_event=events.append,
        on_reserved=lambda native: events.append(("reserved", native)),
        on_identity=lambda native: events.append(("identity", native)),
        on_profile=lambda: events.append(("profile", True)))
    monkeypatch.setattr(module, "resolve_executable", lambda _: str(tmp_path / "claude.exe"))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="2.1.303 (Claude Code)\n"))
    return value, events


def test_native_flags_are_instance_local_and_mcp_has_only_current_job(adapter, monkeypatch):
    value, events = adapter
    monkeypatch.setenv("SHAREBUS_NATIVE_TOOLS", "workspace")
    command = value._command(None)
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--allowedTools") + 1] == TOOL
    assert command[command.index("--permission-prompts") + 1] == "host"
    assert "--strict-mcp-config" in command and "--disable-slash-commands" in command
    assert "--bare" not in command and "--add-dir" not in command
    config = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert set(config["mcpServers"]) == {"sharebus_guide"}
    server = config["mcpServers"]["sharebus_guide"]
    assert server["args"][-2:] == ["--context-file", str(value.context_file)]
    assert server["args"][:2] == ["-I", "-B"]
    assert "env" not in server
    assert events == []  # Preparing argv is not native identity adoption.


@pytest.mark.parametrize("version", ["2.1.304 (Claude Code)", "2.0.0", "unknown", "2.1.303malformed"])
def test_unverified_version_fails_before_model_input(adapter, monkeypatch, version):
    value, events = adapter
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=version))
    with pytest.raises(GuideProfileUnavailable) as failure:
        value.turn("a human help request")
    assert failure.value.reason_code == "unsupported_version"
    assert not isinstance(failure.value, (NativeNeedsHuman, RuntimeError))
    assert events == []
    assert value._process is None


def test_missing_native_is_unavailable_without_approval_or_identity(adapter, monkeypatch):
    value, events = adapter
    monkeypatch.setattr(module, "resolve_executable", lambda _: None)
    with pytest.raises(GuideProfileUnavailable) as failure:
        value.turn("help")
    assert failure.value.reason_code == "native_missing"
    assert events == [] and value._process is None and value._expected_session is None


@pytest.mark.parametrize("error", [OSError("PRIVATE_OS_DETAIL"), module.subprocess.TimeoutExpired("private", 10)])
def test_failed_version_probe_is_not_a_human_approval(adapter, monkeypatch, error):
    value, events = adapter
    def unavailable(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(module.subprocess, "run", unavailable)
    with pytest.raises(GuideProfileUnavailable) as failure:
        value.turn("help")
    assert failure.value.reason_code == "profile_unverified"
    assert "PRIVATE_OS_DETAIL" not in str(failure.value)
    assert events == [] and value._process is None


def test_actual_subscription_guard_still_reports_native_human_requirement(adapter, monkeypatch):
    value, events = adapter
    def requires_native_sign_in(*_args, **_kwargs):
        raise NativeNeedsHuman("Native authentication requires the real human.")
    monkeypatch.setattr(NativeAdapter, "_check_subscription", requires_native_sign_in)
    result = value.turn("help")
    assert result.needs_human and not result.success and not result.input_attempted
    assert result.status == "needs_human"
    assert not any(isinstance(event, tuple) and event[0] == "reserved" for event in events)
    assert value._process is None


def test_exact_resume_keeps_native_id_no_fallback(adapter, monkeypatch):
    value, events = adapter
    command = value._command("existing-owned-id")
    assert command[-2:] == ["--resume", "existing-owned-id"]
    assert "--session-id" not in command
    monkeypatch.setattr(NativeAdapter, "_check_subscription", lambda *_a, **_k: events.append("subscription-check"))
    value._check_subscription("claude")
    assert events == ["subscription-check", ("reserved", "existing-owned-id")]
    with pytest.raises(RuntimeError):
        value._remember_session("other-peer-id")
    assert value.session_id is None
    value._remember_session("existing-owned-id")
    assert value.session_id == "existing-owned-id"


def test_mandatory_identity_failure_is_not_swallowed_by_public_observer(adapter):
    value, _ = adapter
    value._expected_session = "exact-owned-id"
    def fail(_native):
        raise OSError("private persistence failure")
    value._on_identity = fail
    with pytest.raises(OSError):
        value._remember_session("exact-owned-id")
    assert value.session_id is None


@pytest.mark.parametrize("tools,session", [([TOOL, "Read"], "exact-owned-id"),
    ([TOOL, "Bash"], "exact-owned-id"), ([TOOL], "wrong-native-id"), ([], "exact-owned-id"),
    (None, "exact-owned-id")])
def test_bad_inventory_or_wrong_native_identity_refuses_diagnostic_admission(adapter, tools, session):
    value, events = adapter
    value._expected_session = "exact-owned-id"
    assert value._wire_event({"type": "system", "subtype": "init", "session_id": session, "tools": tools}) is None
    assert not value._profile_ok
    assert value._reader_state["error"]
    assert not events


def test_verified_init_commits_identity_before_diagnostic_profile(adapter):
    value, events = adapter
    value._expected_session = "exact-owned-id"
    frame = {"type": "system", "subtype": "init", "session_id": "exact-owned-id", "tools": [TOOL]}
    assert value._wire_event(frame) == frame
    assert events == [("identity", "exact-owned-id"), ("profile", True)]
    assert value._profile_ok


def test_unobserved_inventory_cannot_publish_success(adapter, monkeypatch):
    value, _ = adapter
    monkeypatch.setattr(NativeAdapter, "turn", lambda *_a, **_k: TurnResult(
        "not verified", "owned-id", True, input_attempted=True, accepted=True, status="completed"))
    result = value.turn("request")
    assert not result.success and result.delivery_unknown and result.text == ""


def test_avalokita_spec_keeps_current_capability_and_human_boundaries():
    spec, spec_hash = load_spec()
    raw = Path(module.__file__).with_name("support_spec.md").read_bytes()
    assert spec == raw.decode("utf-8") and spec_hash == hashlib.sha256(raw).hexdigest()
    assert spec.startswith("# Avalokita 도움 — support-v1\n")
    for rule in (
        "사용자에게 보이는 앱은 Avalokita 하나",
        "ShareBus는 에이전트 사이의 메시지 통신을 맡는 내부 구성요소",
        "현재 도움 기능은 설명과 진단만 제공합니다",
        "현재 도움 입력창",
        "파일 위치·토큰·세션 ID·MCP 연결·슬래시 명령·수동 등록을 사용자의 해결 절차로 요구하지 않습니다",
        "전체 중지 여부가 `null`이면 모르는 상태",
        "실제 네이티브 앱이 인증이나 사용자 승인을 요구했다는 관측",
    ):
        assert rule in spec


def test_exact_spec_reaches_fake_native_preserving_identity_and_private_state(tmp_path, monkeypatch):
    class RecordingAdapter(FakeAdapter):
        instances = []
        behavior = "reply"

    def forbidden(*_args, **_kwargs):
        pytest.fail("Spec propagation must not call a real process, profile, account or model")
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    monkeypatch.setattr(NativeAdapter, "turn", forbidden)
    workspace = tmp_path / "selected-room"
    workspace.mkdir()
    paths = SupportPaths(workspace, root=tmp_path / "help")
    business = {"paused": True, "auto": {}, "agents": [
        {"provider": "claude", "state": "idle", "native_session_id": "PRIVATE_NATIVE_CANARY",
         "token": "PRIVATE_TOKEN_CANARY"}], "jobs": [{"body": "PRIVATE_TRANSCRIPT_CANARY"}]}
    before_business = json.dumps(business, sort_keys=True)
    controller = SupportController(paths, adapter_factory=RecordingAdapter,
                                   diagnostic_reader=lambda: business)
    controller.service_runtime = {"service": "fixture-help", "key": paths.key,
                                  "nonce": "fixture-runtime-only", "pid": 424242,
                                  "url": "http://127.0.0.1:29999"}
    try:
        spec, spec_hash = load_spec()
        for index in range(2):
            if index:
                native_id = controller.store.binding()["native_id"]
                history = controller.store.public("paused")["messages"]
                controller.store.open("claude", "historical-spec-fixture")
                controller.open()
                assert controller.store.binding()["native_id"] == native_id
                assert controller.store.public("paused")["messages"] == history
                assert len(RecordingAdapter.instances) == 1  # Opening changed spec sends no input.
            text = "연결 상태를 설명해 주세요." if index == 0 else "같은 도움 대화에서 이어서 설명해 주세요."
            job, capability = controller.store.create_job(text, "spec-fixture-request-" + str(index))
            controller._work(job, capability)
            adapter = RecordingAdapter.instances[index]
            expected = (spec + "\n\n현재 작업실의 제한된 진단:\n"
                        + json.dumps(controller.diagnostic(), ensure_ascii=False)
                        + "\n\n사용자의 도움 요청:\n" + text)
            assert adapter.prompts == [expected]
            assert controller.store.binding()["spec_hash"] == spec_hash
            assert controller.store.public("paused")["guide"]["state"] == "ready"
            for private in (str(workspace), str(paths.root), controller.operator,
                            adapter.session_id, "PRIVATE_NATIVE_CANARY", "PRIVATE_TOKEN_CANARY",
                            "PRIVATE_TRANSCRIPT_CANARY"):
                assert private not in expected
        native_id = controller.store.binding()["native_id"]
        assert native_id == RecordingAdapter.instances[0].session_id == RecordingAdapter.instances[1].session_id
        job, _ = controller.store.create_job("uncertain fixture input", "spec-fixture-unknown")
        controller.store.reserve_native(job["id"], native_id)
        controller.store.receipt(job["id"], {"type": "input_attempt"})
        controller.store.open("claude", "historical-unknown-spec-fixture")
        controller.open()
        assert controller.store.binding()["native_id"] == native_id
        assert controller.store.public("paused")["guide"]["state"] == "unknown"
        assert len(RecordingAdapter.instances) == 2
        with pytest.raises(SupportError) as caught:
            controller.store.create_job("do not replay", "spec-fixture-no-replay")
        assert caught.value.code == "unknown"
        assert json.dumps(business, sort_keys=True) == before_business
    finally:
        controller.close()
