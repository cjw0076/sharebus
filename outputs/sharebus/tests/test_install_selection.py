"""Provider-specific installation in temporary homes; no native or service I/O."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


HOST_FILES = {
    "codex": (".codex/config.toml", ".codex/hooks.json", ".agents/skills/sharebus/SKILL.md"),
    "claude": (".claude.json", ".claude/settings.json", ".claude/skills/sharebus/SKILL.md",
               ".claude/commands/sharebus.md"),
    "antigravity": (".gemini/config/mcp_config.json", ".gemini/config/config.json", ".gemini/config/hooks.json",
                    ".gemini/config/sidecars/sharebus/sidecar.json", ".gemini/config/skills/sharebus/SKILL.md"),
}


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(home / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("SHAREBUS_INSTALL_BACKUP_ROOT", str(tmp_path / "private-backups"))
    monkeypatch.delenv("AVALOKITA_PACKAGED_RUNTIME", raising=False)
    monkeypatch.delenv("AVALOKITA_RESOURCES_ROOT", raising=False)
    monkeypatch.setattr(install.shutil, "which", lambda command: None)
    return home, workspace


@pytest.mark.parametrize("provider", install.SUPPORTED_PROVIDERS)
def test_only_selected_host_is_read_or_written_and_other_hosts_remain_exact(isolated, tmp_path, provider):
    home, workspace = isolated
    untouched = [home / suffix for other, suffixes in HOST_FILES.items() if other != provider for suffix in suffixes]
    untouched += [home / ".cursor/commands/sharebus.md", home / "AppData/Roaming/Claude/claude_desktop_config.json"]
    untouched += [workspace / suffix for other, suffix in (
        ("claude", ".claude/settings.local.json"), ("codex", ".codex/hooks.json")) if other != provider]
    for path in untouched:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xef\xbb\xbf private host content; deliberately not valid JSON or TOML\r\n")
    before = {path: path.read_bytes() for path in untouched}
    selected_config = home / HOST_FILES[provider][0]
    selected_config.parent.mkdir(parents=True, exist_ok=True)
    selected_config.write_text('model = "preserve-model"\n' if provider == "codex" else '{"preserve": true}\n', encoding="utf-8")

    changes = install.prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, providers=[provider])
    assert all(path not in changes and path not in changes.baseline for path in untouched)
    assert all(path.read_bytes() == content for path, content in before.items())
    assert not (workspace / ".sharebus").exists()
    receipt = install.apply(changes, tmp_path / "backups")
    assert all(path.read_bytes() == content for path, content in before.items())
    assert any(item["changed"] for item in receipt)
    if provider == "codex":
        assert 'model = "preserve-model"' in selected_config.read_text(encoding="utf-8")
    else:
        assert json.loads(selected_config.read_text(encoding="utf-8"))["preserve"] is True
    assert (workspace / "SHAREBUS.md").is_file()


def test_omitted_selection_retains_legacy_cursor_command_and_all_providers(isolated):
    home, workspace = isolated
    legacy = install.prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True)
    explicit = install.prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True,
                               providers=install.SUPPORTED_PROVIDERS)
    cursor = home / ".cursor/commands/sharebus.md"
    assert cursor in legacy and cursor not in explicit
    assert {path: content for path, content in legacy.items() if path != cursor} == explicit


@pytest.mark.parametrize("providers", [[], (), "codex", ["cursor"], ["claude", "invalid"], [None], [["codex"]], 3])
def test_invalid_selection_fails_before_configuration_inspection_or_writes(isolated, monkeypatch, providers):
    home, workspace = isolated
    monkeypatch.setattr(install, "installed_resources_root", lambda: pytest.fail("Selection must be validated first"))
    with pytest.raises(ValueError, match="supported providers"):
        install.prepare(home, Path(sys.executable), providers=providers)
    with pytest.raises(ValueError, match="supported providers"):
        install.install_auto(workspace, home=home, providers=providers, apply_changes=True)
    assert list(home.iterdir()) == [] and list(workspace.iterdir()) == []


def test_config_conflict_preserves_all_prepared_targets_before_any_write(isolated, tmp_path):
    home, workspace = isolated
    changes = install.prepare(home, Path(sys.executable), hooks_workspace=workspace, auto_hooks=True, providers=["codex"])
    target = home / ".codex/config.toml"
    target.parent.mkdir()
    target.write_bytes(b'model = "concurrent-user-change"\r\n')
    with pytest.raises(install.ConfigurationConflict):
        install.apply(changes, tmp_path / "backups")
    assert target.read_bytes() == b'model = "concurrent-user-change"\r\n'
    assert all(not path.exists() for path in changes if path != target)
    assert not (tmp_path / "backups").exists()


def test_selected_install_auto_is_idempotent_and_reports_only_relevant_trust(isolated):
    home, workspace = isolated
    first = install.install_auto(workspace, home=home, providers=["claude"], apply_changes=True)
    again = install.install_auto(workspace, home=home, providers=["claude"], apply_changes=True)
    assert any(item["changed"] for item in first["files"])
    assert all(not item["changed"] for item in again["files"])
    assert first["host_reload_required"] is True
    assert first["codex_hook_trust_review_required"] is False
    assert not (home / ".codex").exists() and not (home / ".gemini").exists()


def test_claude_desktop_requires_claude_selection(isolated):
    home, _ = isolated
    with pytest.raises(ValueError, match="selecting Claude"):
        install.prepare(home, Path(sys.executable), providers=["codex"], desktop=True)
    assert list(home.iterdir()) == []
