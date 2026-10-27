"""Packaged launch contracts in isolated homes; no native clients or model input."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import adapters
import hooks_bridge
import hosted_runtime
import install
import native_bridge
import sharebus
from store import BusError


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(home / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    for key in ("AVALOKITA_PACKAGED_RUNTIME", "AVALOKITA_RESOURCES_ROOT", "SHAREBUS_INSTALL_BACKUP_ROOT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    return home


@pytest.mark.parametrize("result", ["ready", "wrong-version", "failed", "timeout"])
def test_packaged_mcp_probes_only_its_bundled_interpreter(tmp_path, monkeypatch, result):
    resources = tmp_path / "resources"
    python = resources / "python/python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("fixture executable, never launched")
    monkeypatch.setenv("AVALOKITA_PACKAGED_RUNTIME", "1")
    monkeypatch.setenv("AVALOKITA_RESOURCES_ROOT", str(resources))
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pytest.fail("Packaged resolver must not read user configuration")))
    calls = []

    def probe(command, **kwargs):
        calls.append((command, kwargs))
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, 10)
        return subprocess.CompletedProcess(command, int(result == "failed"), "1.30.0" if result == "ready" else "1.29.0", "")

    monkeypatch.setattr(hosted_runtime.subprocess, "run", probe)
    if result == "ready":
        assert hosted_runtime.resolve_mcp_python() == str(python.resolve())
    else:
        with pytest.raises(BusError, match="Bundled MCP"):
            hosted_runtime.resolve_mcp_python()
    assert len(calls) == 1
    assert calls[0][0][:4] == [str(python), "-B", "-I", "-c"]
    assert not any(key.upper().startswith("PYTHON") for key in calls[0][1]["env"])
    assert list(resources.rglob("__pycache__")) == []


def test_packaged_mcp_rejects_external_interpreter_before_any_probe(tmp_path, monkeypatch):
    monkeypatch.setenv("AVALOKITA_PACKAGED_RUNTIME", "1")
    monkeypatch.setenv("AVALOKITA_RESOURCES_ROOT", str(tmp_path / "resources"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pytest.fail("No user configuration reads")))
    monkeypatch.setattr(hosted_runtime.subprocess, "run", lambda *args, **kwargs: pytest.fail("No external interpreter probe"))
    with pytest.raises(BusError, match="app-owned"):
        hosted_runtime.resolve_mcp_python()


def test_development_mcp_keeps_its_configured_runtime_fallback(tmp_path, isolated_home, monkeypatch):
    current = tmp_path / "current/python.exe"
    configured = tmp_path / "configured/python.exe"
    for python in (current, configured):
        python.parent.mkdir()
        python.write_text("fixture, never executed")
    monkeypatch.setattr(sys, "executable", str(current))
    config = isolated_home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text("[mcp_servers.sharebus]\ncommand = " + json.dumps(str(configured)) + "\nargs = [" +
                      json.dumps(str(Path(hosted_runtime.__file__).with_name("mcp_server.py").resolve())) + "]\n")
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, int(command[0] == str(current)), "1.30.0", "")

    monkeypatch.setattr(hosted_runtime.subprocess, "run", probe)
    assert hosted_runtime.resolve_mcp_python() == str(configured.resolve())
    assert [command[0] for command in calls] == [str(current), str(configured)]
    assert all(command[1] == "-I" for command in calls)


@pytest.mark.parametrize("platform,suffix", [
    ("win32", "roaming/Avalokita/private/sharebus-install"),
    ("darwin", "Library/Application Support/Avalokita/private/sharebus-install"),
    ("linux", "config/Avalokita/private/sharebus-install"),
])
def test_private_backup_defaults_follow_platform_user_data(isolated_home, monkeypatch, platform, suffix):
    monkeypatch.setattr(sys, "platform", platform)
    assert install.private_backup_root() == (isolated_home / suffix).resolve()
    assert not (isolated_home / suffix).exists()


def test_backup_override_validation_and_fixture_only_apply(tmp_path, monkeypatch):
    resources = tmp_path / "resources"
    resources.mkdir()
    resource_file = resources / "signed-resource.txt"
    resource_file.write_text("preserve signed bytes")
    resource_hash = hashlib.sha256(resource_file.read_bytes()).hexdigest()
    monkeypatch.setenv("AVALOKITA_RESOURCES_ROOT", str(resources))
    for invalid in (Path("relative"), Path(""), resources / "private", resource_file):
        with pytest.raises(ValueError):
            install.private_backup_root(invalid)
    with pytest.raises(ValueError, match="absolute"):
        install.private_backup_root(Path("bad\0path"))
    target = tmp_path / "fixture-config.json"
    target.write_text('{"fixture":"old"}')
    original = target.read_bytes()
    changes = install.PreparedChanges([target])
    changes[target] = '{"fixture":"new"}'
    backup_root = tmp_path / "user-data/private/sharebus-install/fixture"
    monkeypatch.setenv("SHAREBUS_INSTALL_BACKUP_ROOT", str(backup_root.parent))
    assert install.private_backup_root() == backup_root.parent.resolve()
    receipt = install.apply(changes, backup_root)
    assert receipt == [{"path": str(target), "changed": True}]
    assert [backup.read_bytes() for backup in backup_root.glob("*.bak")] == [original]
    assert hashlib.sha256(resource_file.read_bytes()).hexdigest() == resource_hash
    assert list(resources.iterdir()) == [resource_file]


@pytest.fixture
def installed_layout(tmp_path, monkeypatch):
    resources = tmp_path / "installed/Resources"
    source = resources / "sharebus/install.py"
    source.parent.mkdir(parents=True)
    source.write_text("# Installed source fixture, never executed\n")
    executable_name = "python.exe" if sys.platform == "win32" else "bin/python3.13"
    python = resources / "python" / executable_name
    python.parent.mkdir(parents=True)
    python.write_text("fixture executable, never launched")
    metadata = resources / "PYTHON-RUNTIME.json"
    metadata.write_text(json.dumps({"schema": "avalokita-python-runtime/1", "executable": executable_name}))
    monkeypatch.setattr(install, "__file__", str(source))
    monkeypatch.setattr(install, "HERE", source.parent)
    monkeypatch.setattr(sys, "executable", str(python))
    return resources, python, metadata


@pytest.mark.parametrize("windowless", [False, True])
def test_envless_installed_helper_rejects_resources_backup_override(installed_layout, tmp_path, monkeypatch, windowless):
    resources, python, _ = installed_layout
    if windowless:
        if sys.platform != "win32":
            return
        python = python.with_name("pythonw.exe")
        python.write_text("fixture windowless executable, never launched")
        monkeypatch.setattr(sys, "executable", str(python))
    assert install.installed_resources_root() == resources.resolve()
    for override in (resources, resources / "private/backup"):
        with pytest.raises(ValueError, match="outside packaged resources") as failure:
            install.private_backup_root(override)
        assert str(resources) not in str(failure.value)
    monkeypatch.setenv("AVALOKITA_RESOURCES_ROOT", str(tmp_path / "unrelated-env-resources"))
    with pytest.raises(ValueError, match="outside packaged resources"):
        install.private_backup_root(resources / "backup")
    assert install.private_backup_root(tmp_path / "user-data/private") == (tmp_path / "user-data/private").resolve()
    assert not (resources / "private").exists()


@pytest.mark.parametrize("corruption", ["schema", "traversal", "missing"])
def test_envless_installed_helper_fails_closed_on_invalid_metadata(installed_layout, tmp_path, corruption):
    resources, _, metadata = installed_layout
    if corruption == "missing":
        metadata.unlink()
    elif corruption == "schema":
        metadata.write_text('{"schema":"unrelated","executable":"python.exe"}')
    else:
        metadata.write_text('{"schema":"avalokita-python-runtime/1","executable":"../outside.exe"}')
    with pytest.raises(ValueError, match="metadata could not be validated") as failure:
        install.private_backup_root(tmp_path / "user-data/private")
    assert str(resources) not in str(failure.value)
    assert not (tmp_path / "user-data").exists()


def test_unrelated_metadata_does_not_reclassify_development_source(installed_layout, tmp_path, monkeypatch):
    resources, _, metadata = installed_layout
    external = tmp_path / "development/python.exe"
    external.parent.mkdir()
    external.write_text("fixture executable, never launched")
    monkeypatch.setattr(sys, "executable", str(external))
    metadata.write_text('{"schema":"unrelated"}')
    assert install.installed_resources_root() is None
    assert install.private_backup_root(resources / "dev-backup") == (resources / "dev-backup").resolve()


def test_envless_installer_generates_B_for_its_verified_bundle(installed_layout, isolated_home):
    resources, python, _ = installed_layout
    skill = resources / "sharebus/commands/sharebus/SKILL.md"
    skill.parent.mkdir(parents=True)
    original_template = Path(__file__).resolve().parents[1] / "commands/sharebus/SKILL.md"
    skill.write_text(original_template.read_text(encoding="utf-8"), encoding="utf-8")
    changes = install.prepare(isolated_home, python)
    entry = json.loads(changes[isolated_home / ".claude.json"])["mcpServers"]["sharebus"]
    assert entry["args"][0] == "-B"
    sidecar = json.loads(changes[isolated_home / ".gemini/config/sidecars/sharebus/sidecar.json"])
    assert sidecar["args"][0] == "-B"
    hooks = json.loads(changes[isolated_home / ".gemini/config/hooks.json"])["sharebus-native-metadata"]
    assert all(" -B " in group["command"] for groups in hooks.values() for group in groups)
    assert f"& '{python}' -B '" in changes[isolated_home / ".agents/skills/sharebus/SKILL.md"]
    assert list(isolated_home.iterdir()) == []


@pytest.mark.parametrize("ready", [False, True])
def test_envless_standalone_mcp_verifies_bundle_before_probe_or_config_read(installed_layout, monkeypatch, ready):
    resources, python, _ = installed_layout
    source = resources / "sharebus/hosted_runtime.py"
    source.write_text("# Installed runtime fixture, never executed\n")
    monkeypatch.setattr(hosted_runtime, "__file__", str(source))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pytest.fail("No standalone bundled config fallback")))
    calls = []

    def probe(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "1.30.0" if ready else "1.29.0", "")

    monkeypatch.setattr(hosted_runtime.subprocess, "run", probe)
    if ready:
        assert hosted_runtime.resolve_mcp_python() == str(python.resolve())
    else:
        with pytest.raises(BusError, match="Bundled MCP"):
            hosted_runtime.resolve_mcp_python()
    assert len(calls) == 1
    assert calls[0][:4] == [str(python), "-B", "-I", "-c"]


@pytest.mark.parametrize("queue", [False, True])
def test_packaged_installer_emits_bytecode_free_mcp_hooks_watchers_and_skills(tmp_path, isolated_home, monkeypatch, queue):
    monkeypatch.setenv("AVALOKITA_PACKAGED_RUNTIME", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    python = Path(sys.executable).resolve()
    changes = install.prepare(isolated_home, python, hooks_workspace=workspace, auto_hooks=True, codex_native_queue=queue)
    for relative in (".claude.json", ".gemini/config/mcp_config.json"):
        entry = json.loads(changes[isolated_home / relative])["mcpServers"]["sharebus"]
        assert entry["command"] == str(python) and entry["args"][0] == "-B"
    entry = tomllib.loads(changes[isolated_home / ".codex/config.toml"])["mcp_servers"]["sharebus"]
    assert entry["command"] == str(python) and entry["args"][0] == "-B"
    sidecar = json.loads(changes[isolated_home / ".gemini/config/sidecars/sharebus/sidecar.json"])
    assert sidecar["command"] == str(python) and sidecar["args"][0] == "-B"
    assert sidecar["env"]["SHAREBUS_SIDECAR_PROBE_ONLY"] == "1"
    for path, content in changes.items():
        if path.suffix == ".json":
            value = json.loads(content)
            hooks = value.get("hooks", value.get("sharebus-native-metadata", {}))
            for groups in hooks.values():
                for group in groups:
                    for hook in group.get("hooks", [group]):
                        if "args" in hook:
                            argv = [hook["command"], *hook["args"]]
                            assert argv[argv.index(str(python)) + 1] == "-B"
                        elif ".py" in hook.get("command", ""):
                            assert " -B " in hook["command"]
    skill = changes[isolated_home / ".agents/skills/sharebus/SKILL.md"]
    assert skill.count(f"& '{python}' -B '") == 2
    assert skill.count(f"{python} -B {install.HERE / 'auto_enroll.py'}") == 2
    assert not (workspace / ".sharebus").exists()
    assert list(isolated_home.iterdir()) == []


def test_packaged_installer_and_adapter_reject_python_overrides(tmp_path, isolated_home, monkeypatch):
    monkeypatch.setenv("AVALOKITA_PACKAGED_RUNTIME", "1")
    outside = tmp_path / "outside-python.exe"
    outside.write_text("fixture")
    with pytest.raises(ValueError, match="bundled"):
        install.prepare(isolated_home, outside)
    with pytest.raises(ValueError, match="bundled"):
        adapters.NativeAdapter("codex", tmp_path, "fixture", tmp_path / "token", mcp_python=outside)
    assert list(isolated_home.iterdir()) == []
    assert not (tmp_path / ".sharebus").exists()


@pytest.mark.parametrize("provider", ["codex", "claude", "antigravity"])
@pytest.mark.parametrize("from_env", [False, True])
def test_packaged_native_mcp_arguments_have_B_without_altering_native_cli(tmp_path, monkeypatch, provider, from_env):
    if from_env:
        monkeypatch.setenv("AVALOKITA_PACKAGED_RUNTIME", "1")
    else:
        monkeypatch.setattr(sys, "dont_write_bytecode", True)
    executable = str(tmp_path / "native-client.exe")
    monkeypatch.setattr(adapters, "resolve_executable", lambda _: executable)
    adapter = adapters.NativeAdapter(provider, tmp_path, "fixture", tmp_path / "token")
    command = adapter._command(None)
    assert command[0] == executable and "-B" not in command
    if provider == "codex":
        config = dict(argument.split("=", 1) for argument in command if argument.startswith("mcp_servers."))
        args = json.loads(config["mcp_servers.sharebus.args"])
        assert config["mcp_servers.sharebus.tools.sharebus_send.approval_mode"] == '"approve"'
    elif provider == "claude":
        args = json.loads(Path(command[command.index("--mcp-config") + 1]).read_text())["mcpServers"]["sharebus"]["args"]
        assert command[command.index("--permission-prompts") + 1] == "host"
    else:
        args = json.loads((tmp_path / ".agents/mcp_config.json").read_text())["mcpServers"]["sharebus"]["args"]
    assert args[0] == "-B"
    assert Path(args[1]).name == "mcp_server.py"


@pytest.mark.parametrize("from_env", [False, True])
def test_packaged_native_guard_is_bytecode_free_and_keeps_native_approval(tmp_path, monkeypatch, from_env):
    if from_env:
        monkeypatch.setenv("AVALOKITA_PACKAGED_RUNTIME", "1")
    else:
        monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.setattr(native_bridge, "resolve_executable", lambda _: str(tmp_path / "fixture-claude.exe"))
    monkeypatch.setattr(native_bridge.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, "2.1.299 (Claude Code)", ""))
    proxy = native_bridge._ClaudeDeliveryProxy(tmp_path, "fixture", tmp_path / "token", None)
    command = proxy._command(None)
    settings = json.loads(command[command.index("--settings") + 1])
    hook = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["command"] == sys.executable and hook["args"][0] == "-B"
    assert hook["onFailure"] == "block"
    assert command[command.index("--permission-mode") + 1] == "manual"
    assert "-B" not in command


@pytest.mark.parametrize("inherited_B", [False, True])
def test_native_hook_and_daemon_child_preserve_explicit_B_after_host_env_filtering(tmp_path, monkeypatch, inherited_B):
    monkeypatch.setattr(sys, "dont_write_bytecode", inherited_B)
    output = hooks_bridge.context_output("codex", "SessionStart", tmp_path / "fixture-context.json", tmp_path)
    text = output["hookSpecificOutput"]["additionalContext"]
    assert ('"-B"' in text) == inherited_B
    client = sharebus.Client(tmp_path)
    monkeypatch.setattr(client, "runtime", lambda: (_ for _ in ()).throw(RuntimeError("fixture stopped")))
    calls = []
    monkeypatch.setattr(sharebus.subprocess, "run", lambda command, **kwargs: calls.append(command))
    assert client.start(background=False) == {"stopped": True}
    assert (calls[0][1] == "-B") == inherited_B
    assert calls[0][0] == sys.executable


def test_B_prevents_actual_source_import_bytecode_with_filtered_environment(tmp_path):
    resources = tmp_path / "resources"
    resources.mkdir()
    module = resources / "fixture_module.py"
    module.write_text("VALUE = 42\n")
    script = resources / "probe.py"
    script.write_text("import fixture_module, sys\nprint(fixture_module.VALUE, sys.dont_write_bytecode)\n")
    before = {path.name: path.read_bytes() for path in resources.iterdir()}
    environment = {name: value for name, value in os.environ.items() if not name.upper().startswith("PYTHON")}
    result = subprocess.run([sys.executable, "-B", str(script)], cwd=resources, env=environment,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, check=True)
    assert result.stdout.strip() == "42 True"
    assert {path.name: path.read_bytes() for path in resources.iterdir()} == before
