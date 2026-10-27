"""Actual collected adapter paths; all auth responses and process creation are fake."""
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest


@pytest.fixture
def modules():
    root = Path(os.environ.get("SHAREBUS_TEST_SOURCE", Path(__file__).resolve().parents[1]))
    runtime = root / "outputs/sharebus"
    if not (runtime / "supervisor.py").is_file():
        pytest.skip("Approved standalone source has not been collected yet")
    sys.path.insert(0, str(runtime))
    found = tuple(importlib.import_module(name) for name in ("supervisor", "adapters", "native_bridge"))
    assert all(Path(module.__file__).resolve().parent == runtime.resolve() for module in found)
    return found


@pytest.mark.parametrize("mode", ["managed_new", "managed_resume", "native_attached"])
def test_actual_supervisor_factory_options_are_scoped(modules, tmp_path, mode):
    supervisor, _, _ = modules
    seen = []
    agent = {"mode": mode, "provider": "claude", "native_address": "fixture", "native_session_id": None}
    store = SimpleNamespace(workspace=tmp_path, credentials=tmp_path, agent=lambda _: agent,
                            finish=lambda *args, **kwargs: None, workspace_retired=False)
    instance = object.__new__(supervisor.Supervisor)
    instance.store = store
    instance.hosted = SimpleNamespace(guard=lambda: None, retired=False)
    instance._lock = threading.RLock()
    instance._adapters, instance._workers = {}, {}
    instance._busy, instance._interrupted = set(), set()
    instance._stop = threading.Event()
    instance.wake = lambda: None

    def factory(*args, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("Stop at the actual factory boundary; no adapter or model starts")

    instance.adapter_factory = factory
    instance._run_claimed("fixture-agent", {"id": "fixture-job"})
    assert len(seen) == 1
    assert seen[0].get("subscription_only") is (None if mode == "native_attached" else True)


@pytest.mark.parametrize("method", ["api_key", "claude.ai"])
def test_actual_claude_proxy_start_checks_auth_and_filters_api_environment(modules, tmp_path, monkeypatch, method):
    _, adapters, bridge = modules
    captured = []
    spawned = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-value")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")

    def auth(command, **kwargs):
        captured.append((command, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"loggedIn": True, "authMethod": method}), stderr="")

    class AtSpawn(Exception):
        pass

    def fake_process(*args, **kwargs):
        spawned.append(kwargs["env"])
        raise AtSpawn()

    monkeypatch.setattr(adapters.subprocess, "run", auth)
    monkeypatch.setattr(adapters.subprocess, "Popen", fake_process)
    proxy = bridge._ClaudeDeliveryProxy(tmp_path, "fixture-agent", tmp_path / "token", None)
    monkeypatch.setattr(proxy, "_command", lambda _: ["fixture-claude.exe"])
    with pytest.raises(adapters.NativeNeedsHuman if method == "api_key" else AtSpawn):
        proxy.start()
    assert captured[0][0] == ["fixture-claude.exe", "auth", "status"]
    assert "ANTHROPIC_API_KEY" not in captured[0][1] and "OPENAI_API_KEY" not in captured[0][1]
    assert bool(spawned) is (method == "claude.ai")
    assert all("ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env for env in spawned)
