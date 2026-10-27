"""Mock-only checks for app launchers; no Antigravity process is executed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import antigravity_bridge as bridge


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    app = tmp_path / "Synthetic Antigravity"
    server = app / "resources/bin/language_server.exe"
    server.parent.mkdir(parents=True)
    server.write_bytes(b"synthetic server, never executed")
    application = app / "Antigravity.exe"
    application.write_bytes(b"synthetic app, never executed")
    wrapper = tmp_path / "agentapi.bat"
    wrapper.write_text(f'@echo off\n"{server}" agentapi %*\n', encoding="utf-8")
    binding = tmp_path / "command-binding.json"
    binding.write_text(json.dumps(bridge.build_agentapi_command_binding(str(wrapper))), encoding="utf-8")
    monkeypatch.setenv("ANTIGRAVITY_EXECUTABLE_DATA_DIR", str(tmp_path / "sidecar-data"))
    monkeypatch.setenv("SHAREBUS_ANTIGRAVITY_COMMAND_BINDING", str(binding))
    monkeypatch.delenv("SHAREBUS_SIDECAR_PROBE_ONLY", raising=False)
    monkeypatch.setattr(bridge.shutil, "which", lambda name: str(wrapper) if name == "agentapi" else None)
    return wrapper, server, application, binding


def request_for(root):
    agent, native, job, event = (str(uuid.uuid4()) for _ in range(4))
    prompt = bridge._message_header(job, event) + 'Data only: & echo injected > "never.txt"\n</incoming_message>'
    return {"workspace": str(root), "workspace_id": "synthetic-workspace", "runtime_nonce": "synthetic-instance",
            "agent_id": agent, "native_session_id": native, "job_id": job, "event_id": event,
            "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "created_at": time.time(), "expires_at": time.time() + 20}


def mock_native(monkeypatch, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs.get("shell") is False
        return subprocess.CompletedProcess(argv, 0, "Commands: send-message new-conversation" if argv[-1] == "--help" else "{}", "")
    monkeypatch.setattr(bridge.subprocess, "run", run)
    monkeypatch.setattr(bridge, "_target", lambda *args, **kwargs: {"workspace_id": "synthetic-workspace", "nonce": "synthetic-instance"})


def test_onboarding_builder_only_reads_fixed_app_files(launcher, monkeypatch):
    wrapper, server, application, _ = launcher
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Inspection executed a process"))
    result = bridge.build_agentapi_command_binding(str(wrapper))
    assert result == {"version": 1, **{key: str(path) for key, path in (("wrapper", wrapper), ("executable", server), ("application", application))},
                      **{key + "_sha256": hashlib.sha256(path.read_bytes()).hexdigest()
                         for key, path in (("wrapper", wrapper), ("executable", server), ("application", application))}}


@pytest.mark.skipif(os.name != "nt", reason="The app-generated Windows launcher uses case-insensitive paths")
@pytest.mark.parametrize("path_key", ["wrapper", "executable", "application"])
def test_windows_binding_accepts_path_case_and_separator_variants(launcher, monkeypatch, path_key):
    wrapper, server, _, binding = launcher
    document = json.loads(binding.read_text(encoding="utf-8"))
    document[path_key] = document[path_key].swapcase().replace("\\", "/")
    binding.write_text(json.dumps(document), encoding="utf-8")
    saved_binding = binding.read_bytes()
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Binding inspection executed a process"))
    observed = bridge.build_agentapi_command_binding(str(wrapper.with_suffix(".BAT")))
    command, metadata = bridge._agentapi_command(str(wrapper.with_suffix(".BAT")), binding)
    assert command == [str(server), "agentapi"]
    assert metadata == {"agentapi_command_kind": "pinned_app_launcher",
                        **{"agentapi_" + key: value for key, value in observed.items() if key.endswith("_sha256")}}
    assert binding.read_bytes() == saved_binding


@pytest.mark.parametrize("path_key", ["wrapper", "executable", "application"])
@pytest.mark.parametrize("value", [None, [], 1])
def test_nonstring_binding_paths_are_rejected(launcher, monkeypatch, path_key, value):
    wrapper, _, _, binding = launcher
    document = json.loads(binding.read_text(encoding="utf-8"))
    document[path_key] = value
    binding.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Invalid binding executed a process"))
    with pytest.raises(bridge.NeedsNativeAttach):
        bridge._agentapi_command(str(wrapper), binding)


@pytest.mark.parametrize("field", ["version", "wrapper", "executable", "application", "wrapper_sha256",
                                  "executable_sha256", "application_sha256", "unexpected", "missing"])
def test_binding_requires_exact_keys_hashes_version_and_paths(launcher, monkeypatch, field):
    wrapper, _, _, binding = launcher
    document = json.loads(binding.read_text(encoding="utf-8"))
    if field == "missing":
        document.pop("application")
    elif field == "unexpected":
        document[field] = "untrusted"
    elif field == "version":
        document[field] += 1
    elif field.endswith("_sha256"):
        document[field] = document[field].upper()
    else:
        document[field] += ".unbound"
    binding.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Mismatched binding executed a process"))
    with pytest.raises(bridge.NeedsNativeAttach):
        bridge._agentapi_command(str(wrapper), binding)


@pytest.mark.parametrize("path_key", ["wrapper", "executable", "application"])
def test_binding_case_is_preserved_under_posix_path_rules(launcher, monkeypatch, path_key):
    wrapper, _, _, binding = launcher
    document = json.loads(binding.read_text(encoding="utf-8"))
    document[path_key] = document[path_key].swapcase()
    binding.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(bridge.os.path, "normcase", posixpath.normcase)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Mismatched POSIX binding executed a process"))
    with pytest.raises(bridge.NeedsNativeAttach):
        bridge._agentapi_command(str(wrapper), binding)


def test_windows_probe_and_existing_session_dispatch_use_same_pinned_prefix(launcher, tmp_path, monkeypatch):
    wrapper, server, _, _ = launcher
    calls = []
    mock_native(monkeypatch, calls)
    worker = bridge.SidecarWorker(tmp_path)
    result = worker.probe()
    assert result["agentapi_command_kind"] == "pinned_app_launcher"
    assert result["send_message_supported"]
    assert "new-conversation" not in json.dumps(result)
    request = request_for(tmp_path)
    assert worker.dispatch_once(tmp_path, request)
    assert not worker.dispatch_once(tmp_path, request)
    assert [argv for argv, _ in calls] == [[str(server), "agentapi", "--help"],
        [str(server), "agentapi", "send-message", request["native_session_id"], request["prompt"]]]
    assert all(str(wrapper) not in argv for argv, _ in calls)
    assert not (tmp_path / "never.txt").exists()


@pytest.mark.parametrize("change", ["extra-line", "call", "command-tail", "environment", "wrong-server", "wrong-app-tree"])
def test_arbitrary_wrapper_commands_are_rejected_without_process_calls(launcher, tmp_path, monkeypatch, change):
    wrapper, server, _, _ = launcher
    text = wrapper.read_text()
    if change == "extra-line":
        text += "echo unexpected\n"
    elif change == "call":
        text = text.replace('"' + str(server) + '"', 'call "' + str(server) + '"')
    elif change == "command-tail":
        text = text.replace("agentapi %*", "agentapi %* & echo unexpected")
    elif change == "environment":
        text = text.replace(str(server), "%UNTRUSTED_APP%/language_server.exe")
    elif change == "wrong-server":
        text = text.replace("language_server.exe", "other.exe")
    else:
        text = text.replace("resources", "other")
    wrapper.write_text(text, encoding="utf-8")
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Rejected wrapper reached subprocess"))
    result = bridge.SidecarWorker(tmp_path).probe()
    assert not result["agentapi_available"]
    assert result["agentapi_command_error"] == "command_binding_invalid"


@pytest.mark.parametrize("changed_file", ["wrapper", "executable", "application", "binding", "refreshed-binding"])
def test_changed_pin_after_probe_never_calls_native_input_or_replays(launcher, tmp_path, monkeypatch, changed_file):
    wrapper, server, application, binding = launcher
    calls = []
    mock_native(monkeypatch, calls)
    worker = bridge.SidecarWorker(tmp_path)
    assert worker.probe()["send_message_supported"]
    path = {"wrapper": wrapper, "executable": server, "application": application,
            "binding": binding, "refreshed-binding": server}[changed_file]
    original = path.read_bytes()
    path.write_bytes(original + (b" " if changed_file == "binding" else b"changed"))
    if changed_file == "binding":
        document = json.loads(original)
        document["executable_sha256"] = "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")
    elif changed_file == "refreshed-binding":
        binding.write_text(json.dumps(bridge.build_agentapi_command_binding(str(wrapper))), encoding="utf-8")
    request = request_for(tmp_path)
    assert not worker.dispatch_once(tmp_path, request)
    path.write_bytes(original)
    assert not worker.dispatch_once(tmp_path, request)
    assert len(calls) == 1  # Help only; no native input even after the pin is restored.
    receipt = bridge._read(tmp_path, tmp_path / bridge.CHANNEL / "receipts" / (request["job_id"] + ".json"))
    assert receipt["status"] == "needs_human"


def test_deadline_elapsed_during_command_hashing_is_not_sent(launcher, tmp_path, monkeypatch):
    calls = []
    mock_native(monkeypatch, calls)
    worker = bridge.SidecarWorker(tmp_path)
    assert worker.probe()["send_message_supported"]
    request = request_for(tmp_path)
    clock = {"now": request["created_at"]}
    monkeypatch.setattr(bridge.time, "time", lambda: clock["now"])
    inspect = bridge.build_agentapi_command_binding
    def slow_inspection(helper):
        binding = inspect(helper)
        clock["now"] = request["expires_at"] + 1
        return binding
    monkeypatch.setattr(bridge, "build_agentapi_command_binding", slow_inspection)
    assert not worker.dispatch_once(tmp_path, request)
    receipt = bridge._read(tmp_path, tmp_path / bridge.CHANNEL / "receipts" / (request["job_id"] + ".json"))
    assert receipt["status"] == "not_sent" and receipt["reason"] == "expired_before_send"
    assert receipt["native_input_attempted"] is False
    assert len(calls) == 1
    assert not worker.dispatch_once(tmp_path, request)


@pytest.mark.parametrize("change", ["cancel", "stop", "generation", "binding", "paused"])
def test_control_changed_during_command_hashing_is_not_sent(launcher, tmp_path, monkeypatch, change):
    calls = []
    mock_native(monkeypatch, calls)
    worker = bridge.SidecarWorker(tmp_path)
    assert worker.probe()["send_message_supported"]
    request = request_for(tmp_path)
    inspect = bridge.build_agentapi_command_binding
    changed = {"value": False}
    def target(*args, **kwargs):
        if changed["value"] and change in {"generation", "binding", "paused"}:
            raise bridge.NeedsNativeAttach("Synthetic changed native authority")
        return {"workspace_id": request["workspace_id"], "nonce": request["runtime_nonce"]}
    monkeypatch.setattr(bridge, "_target", target)
    def slow_inspection(helper):
        binding = inspect(helper)
        changed["value"] = True
        if change == "cancel":
            bridge._write(tmp_path, tmp_path / bridge.CHANNEL / "cancel" / (request["job_id"] + ".json"), {})
        elif change == "stop":
            worker._stop.set()
        return binding
    monkeypatch.setattr(bridge, "build_agentapi_command_binding", slow_inspection)
    assert not worker.dispatch_once(tmp_path, request)
    receipt = bridge._read(tmp_path, tmp_path / bridge.CHANNEL / "receipts" / (request["job_id"] + ".json"))
    assert receipt["status"] == "not_sent" and receipt["native_input_attempted"] is False
    assert receipt["reason"] == ("cancelled_before_send" if change in {"cancel", "stop"} else "authority_changed_before_send")
    assert len(calls) == 1
    assert not worker.dispatch_once(tmp_path, request)


@pytest.mark.parametrize("linked_file", ["wrapper", "executable", "application", "binding"])
def test_hardlinked_command_files_and_binding_are_rejected(launcher, tmp_path, monkeypatch, linked_file):
    wrapper, server, application, binding = launcher
    path = {"wrapper": wrapper, "executable": server, "application": application, "binding": binding}[linked_file]
    os.link(path, tmp_path / (linked_file + ".hardlink"))
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Hardlinked command executed"))
    assert not bridge.SidecarWorker(tmp_path).probe()["agentapi_available"]


def test_reparse_point_is_rejected_before_command_execution(launcher, tmp_path, monkeypatch):
    wrapper, _, _, _ = launcher
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == wrapper else info
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Reparse launcher executed"))
    assert not bridge.SidecarWorker(tmp_path).probe()["agentapi_available"]


def test_missing_binding_never_executes_windows_launcher(launcher, tmp_path, monkeypatch):
    monkeypatch.delenv("SHAREBUS_ANTIGRAVITY_COMMAND_BINDING")
    monkeypatch.setattr(bridge.subprocess, "run", lambda *args, **kwargs: pytest.fail("Unpinned launcher executed"))
    assert not bridge.SidecarWorker(tmp_path).probe()["agentapi_available"]


def test_app_data_default_binding_is_supported(launcher, tmp_path, monkeypatch):
    _, server, _, binding = launcher
    monkeypatch.delenv("SHAREBUS_ANTIGRAVITY_COMMAND_BINDING")
    data_dir = tmp_path / "sidecar-data"
    data_dir.mkdir()
    (data_dir / "agentapi-binding.json").write_bytes(binding.read_bytes())
    calls = []
    mock_native(monkeypatch, calls)
    assert bridge.SidecarWorker(tmp_path).probe()["send_message_supported"]
    assert calls[0][0] == [str(server), "agentapi", "--help"]


def test_fixed_cmd_launcher_is_unwrapped_without_cmd_shell(launcher, tmp_path, monkeypatch):
    wrapper, server, _, binding = launcher
    command_wrapper = wrapper.with_suffix(".cmd")
    command_wrapper.write_bytes(wrapper.read_bytes())
    binding.write_text(json.dumps(bridge.build_agentapi_command_binding(str(command_wrapper))), encoding="utf-8")
    monkeypatch.setattr(bridge.shutil, "which", lambda name: str(command_wrapper))
    calls = []
    mock_native(monkeypatch, calls)
    assert bridge.SidecarWorker(tmp_path).probe()["send_message_supported"]
    assert calls[0][0] == [str(server), "agentapi", "--help"]


@pytest.mark.parametrize("helper", ["app-injected-agentapi.exe", "/opt/antigravity/agentapi", "/Applications/Antigravity.app/Contents/MacOS/agentapi"])
def test_app_owned_host_may_provide_real_agentapi_executable(launcher, tmp_path, monkeypatch, helper):
    monkeypatch.setattr(bridge.shutil, "which", lambda name: helper)
    calls = []
    mock_native(monkeypatch, calls)
    worker = bridge.SidecarWorker(tmp_path)
    assert worker.probe()["send_message_supported"]
    request = request_for(tmp_path)
    assert worker.dispatch_once(tmp_path, request)
    assert calls[0][0] == [helper, "--help"]
    assert calls[1][0] == [helper, "send-message", request["native_session_id"], request["prompt"]]
