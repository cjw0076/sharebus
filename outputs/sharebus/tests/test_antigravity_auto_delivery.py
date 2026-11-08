"""Automatic delivery selection using generated homes, without native input."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto_mode
import install


@pytest.fixture
def generated(tmp_path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("AVALOKITA_PACKAGED_RUNTIME", raising=False)
    monkeypatch.delenv("AVALOKITA_RESOURCES_ROOT", raising=False)
    monkeypatch.setattr(install, "installed_resources_root", lambda: None)
    monkeypatch.setattr(install.shutil, "which", lambda command: None)
    for owner, name in ((subprocess, "Popen"), (subprocess, "run"), (socket, "create_connection")):
        monkeypatch.setattr(owner, name, lambda *args, **kwargs: pytest.fail("Native/service I/O is forbidden"))
    captured = []
    prepare = install.prepare

    def observe(*args, **kwargs):
        changes = prepare(*args, **kwargs)
        captured.append(changes)
        return changes

    monkeypatch.setattr(install, "prepare", observe)
    return home, workspace, captured


def sidecar_path(home):
    return home / ".gemini/config/sidecars/sharebus/sidecar.json"


def seeded_sidecar(home, *, probe_only):
    path = sidecar_path(home)
    value = {}
    install.sidecar_update(value, Path(sys.executable).resolve(), path.parent / "workspaces", probe_only, False)
    value["env"]["KEEP_USER_ENV"] = "generated-test-value"
    value["user_note"] = "preserve this setting"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    return path.read_bytes()


def prepared_probe_mode(home, captured):
    return json.loads(captured[-1][sidecar_path(home)])["env"]["SHAREBUS_SIDECAR_PROBE_ONLY"]


def test_fresh_automatic_room_prepares_delivery_without_host_flags(generated):
    home, workspace, captured = generated
    receipt = install.install_auto(workspace, home=home, providers=["antigravity"])
    assert prepared_probe_mode(home, captured) == "0"
    assert receipt["applied"] is False
    assert receipt["native_messages_sent"] == receipt["managed_workers_started"] == 0
    assert list(home.iterdir()) == [] and list(workspace.iterdir()) == []


@pytest.mark.parametrize("old_mode", [True, False])
def test_automatic_room_upgrades_probe_mode_and_preserves_unrelated_settings(generated, old_mode):
    home, workspace, captured = generated
    original = seeded_sidecar(home, probe_only=old_mode)
    config = home / ".gemini/config/config.json"
    config.write_text('{"unrelated":{"theme":"light"},"sidecars":{"other":{"enabled":false}}}\n', encoding="utf-8")
    config_before = config.read_bytes()
    install.install_auto(workspace, home=home, providers=["antigravity"])
    result = json.loads(captured[-1][sidecar_path(home)])
    assert result["env"] == {"SHAREBUS_SIDECAR_PROBE_ONLY": "0", "KEEP_USER_ENV": "generated-test-value"}
    assert result["user_note"] == "preserve this setting"
    settings = json.loads(captured[-1][config])
    assert settings["unrelated"] == {"theme": "light"}
    assert settings["sidecars"] == {"other": {"enabled": False}, "sharebus": {"enabled": True}}
    assert sidecar_path(home).read_bytes() == original and config.read_bytes() == config_before
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("existing", [False, True])
def test_explicit_automatic_diagnostic_mode_remains_probe_only(generated, existing):
    home, workspace, captured = generated
    before = seeded_sidecar(home, probe_only=False) if existing else None
    install.install_auto(workspace, home=home, providers=["antigravity"], sidecar_probe_only=True)
    assert prepared_probe_mode(home, captured) == "1"
    assert sidecar_path(home).read_bytes() == before if existing else not sidecar_path(home).exists()


@pytest.mark.parametrize("old_mode", [None, True, False])
def test_manual_prepare_keeps_its_existing_probe_or_delivery_selection(generated, old_mode):
    home, _, captured = generated
    before = seeded_sidecar(home, probe_only=old_mode) if old_mode is not None else None
    install.prepare(home, Path(sys.executable).resolve(), providers=["antigravity"])
    assert prepared_probe_mode(home, captured) == ("0" if old_mode is False else "1")
    assert sidecar_path(home).read_bytes() == before if before is not None else not sidecar_path(home).exists()


@pytest.mark.parametrize("invalid", [None, 0, 1, "0", [], {}])
def test_invalid_automatic_selection_is_rejected_before_filesystem_inspection(generated, monkeypatch, invalid):
    home, workspace, captured = generated
    monkeypatch.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("Validate before resolving paths"))
    with pytest.raises(ValueError, match="boolean"):
        install.install_auto(workspace, home=home, providers=["antigravity"], sidecar_probe_only=invalid)
    assert captured == []


def test_existing_automatic_entry_uses_delivery_default_without_app_or_apply(generated, monkeypatch):
    home, workspace, captured = generated
    original = install.install_auto

    def preview(*args, **kwargs):
        assert kwargs.pop("apply_changes") is True
        return original(*args, **kwargs, apply_changes=False)

    monkeypatch.setattr(install, "install_auto", preview)
    receipt = auto_mode._install(workspace, home, Path(sys.executable))
    assert prepared_probe_mode(home, captured) == "0"
    assert receipt["applied"] is False and receipt["native_messages_sent"] == 0
    assert list(home.iterdir()) == [] and list(workspace.iterdir()) == []


def test_delivery_selection_preserves_guard_refusal_before_configuration_io(generated, monkeypatch):
    home, _, captured = generated
    refusal = RuntimeError("generated workspace guard refusal")

    def refuse():
        raise refusal

    monkeypatch.setattr(install, "installed_resources_root", lambda: pytest.fail("Guard must run first"))
    with pytest.raises(RuntimeError) as caught:
        install.prepare(home, Path(sys.executable), providers=["antigravity"],
                        sidecar_probe_only=False, workspace_guard=refuse)
    assert caught.value is refusal and captured == []
    assert list(home.iterdir()) == []


def test_automatic_delivery_selection_does_not_replace_custom_sidecar_command(generated):
    home, workspace, captured = generated
    seeded_sidecar(home, probe_only=True)
    path = sidecar_path(home)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["args"] = ["generated-custom-worker.py"]
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Existing sharebus Sidecar differs"):
        install.install_auto(workspace, home=home, providers=["antigravity"])
    assert path.read_bytes() == before and list(workspace.iterdir()) == []
    assert captured == []


def test_prepared_delivery_upgrade_keeps_exact_baseline_for_conflict_detection(generated):
    home, workspace, captured = generated
    original = seeded_sidecar(home, probe_only=True)
    install.install_auto(workspace, home=home, providers=["antigravity"])
    changes, path = captured[-1], sidecar_path(home)
    assert changes.baseline[path] == install.hashlib.sha256(original).hexdigest()
    concurrent = original + b" "
    path.write_bytes(concurrent)
    backup = home.parent / "must-not-be-created"
    with pytest.raises(install.ConfigurationConflict):
        install.apply(changes, backup)
    assert path.read_bytes() == concurrent and not backup.exists()
    assert all(not target.exists() for target in changes if target != path)
