"""Click actions with real Client validation and mocked OS/network/native edges."""
from __future__ import annotations

from contextlib import nullcontext
import copy
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install
import launcher
import sharebus

REAL_CLIENT_START = sharebus.Client.start


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("APPDATA", str(home / "roaming"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("SHAREBUS_INSTALL_BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.delenv("AVALOKITA_PACKAGED_RUNTIME", raising=False)
    monkeypatch.delenv("AVALOKITA_RESOURCES_ROOT", raising=False)
    monkeypatch.setattr(install.shutil, "which", lambda command: None)

    def forbidden(*args, **kwargs):
        pytest.fail("No OS process, service, native input, or external browser may run")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    calls, starts = [], []
    state = {"paused": False, "auto": {"enabled": False, "suspended": False, "paused": False},
             "agents": [], "jobs": [{"state": "needs_human", "body": "preserve unknown outcome"}]}
    health = {}

    def write_runtime():
        private = workspace / ".sharebus/private"
        private.mkdir(parents=True, exist_ok=True)
        value = {"workspace": str(workspace), "workspace_id": "fixture-workspace", "pid": 123,
                 "nonce": "fixture-private-process-nonce", "url": "http://127.0.0.1:12345"}
        (private / "runtime.json").write_text(json.dumps(value), encoding="utf-8")
        (private / "operator.token").write_text("fixture-operator-credential-do-not-display", encoding="utf-8")
        return value

    def opened(request, **kwargs):
        calls.append(("health", request.full_url))
        assert request.full_url == "http://127.0.0.1:12345/health"
        if "error" in health:
            raise health["error"]
        value = json.loads((workspace / ".sharebus/private/runtime.json").read_text(encoding="utf-8"))
        raw = json.dumps({"ok": True, **value, **health}).encode()
        return nullcontext(SimpleNamespace(read=lambda maximum: raw))

    monkeypatch.setattr(sharebus, "build_opener", lambda *args: SimpleNamespace(open=opened))

    def start(client, agents="none", **kwargs):
        calls.append(("start", agents, client.workspace))
        starts.append(kwargs)
        assert agents == "none"
        health.pop("error", None)
        return write_runtime()

    def request(client, path, body=None):
        calls.append((path, copy.deepcopy(body), client.workspace))
        if path == "/api/auto/config":
            assert set(body) == {"enabled"}
            state["auto"]["enabled"] = body["enabled"]
        elif path == "/api/control":
            assert body == {"action": "resume"}
            state["paused"] = False
            state["auto"]["paused"] = False
            return {"ok": True, "state": copy.deepcopy(state)}
        else:
            assert path == "/api/state" and body is None
        return copy.deepcopy(state)

    monkeypatch.setattr(sharebus.Client, "start", start)
    monkeypatch.setattr(sharebus.Client, "request", request)
    browser = []
    monkeypatch.setattr(sharebus.webbrowser, "open", lambda url: browser.append(url))
    selection = launcher.WorkspaceSelection.capture(str(workspace))
    actions = launcher.WorkspaceActions(selection, home=home)
    return SimpleNamespace(home=home, workspace=workspace, actions=actions, selection=selection, calls=calls,
                           state=state, health=health, browser=browser, write_runtime=write_runtime, starts=starts)


class Variable:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self, *args, **kwargs):
        self.options = kwargs
        self.states = []
        self.scheduled = []

    def pack(self, **kwargs):
        pass

    def state(self, states):
        self.states.extend(states)

    def title(self, value):
        pass

    def minsize(self, *args):
        pass

    def protocol(self, *args):
        pass

    def after(self, *args):
        self.scheduled.append(args)

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def gui(sandbox, monkeypatch):
    fake_tk = ModuleType("tkinter")
    fake_tk.StringVar = Variable
    fake_tk.BooleanVar = Variable
    fake_tk.ttk = SimpleNamespace(Frame=Widget, Label=Widget, Button=Widget, Checkbutton=Widget)
    dialogs = SimpleNamespace(askokcancel=lambda *args, **kwargs: False,
                              showerror=lambda *args, **kwargs: None)
    fake_tk.messagebox = dialogs
    fake_tk.filedialog = SimpleNamespace(askdirectory=lambda **kwargs: "")
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setattr(launcher, "available_apps", lambda: {"codex": True, "claude": False, "antigravity": False})
    return launcher.LauncherApp(Widget())


def test_initial_render_and_cancelled_folder_picker_do_nothing(gui, sandbox):
    assert gui.selection is None and gui.auto_enroll.get() is True
    gui.choose_workspace()
    assert gui.selection is None
    assert sandbox.calls == [] and sandbox.browser == []
    assert list(sandbox.home.iterdir()) == [] and list(sandbox.workspace.iterdir()) == []


def test_cancelled_confirmation_does_not_prepare_or_start(gui, sandbox, monkeypatch):
    gui.selection = sandbox.selection
    monkeypatch.setattr(launcher, "prepare", lambda *args, **kwargs: pytest.fail("Cancelled action must not prepare"))
    gui.act("connect")
    assert not gui.busy and sandbox.calls == []
    assert list(sandbox.home.iterdir()) == [] and list(sandbox.workspace.iterdir()) == []


def test_readonly_discovery_looks_up_only_supported_executables(sandbox, monkeypatch):
    providers = []

    def lookup(provider):
        providers.append(provider)
        return "fixture-executable" if provider == "claude" else None

    monkeypatch.setattr(launcher, "resolve_executable", lookup)
    assert launcher.available_apps() == {"codex": False, "claude": True, "antigravity": False}
    assert tuple(providers) == install.SUPPORTED_PROVIDERS
    assert sandbox.calls == [] and list(sandbox.workspace.iterdir()) == []


def test_explicit_connect_uses_selected_install_real_client_and_private_browser_handoff(sandbox, capsys):
    result = sandbox.actions.connect(("codex",))
    assert result == "앱 연결 대기"
    assert (sandbox.home / ".codex/config.toml").is_file()
    assert not (sandbox.home / ".claude.json").exists() and not (sandbox.home / ".gemini").exists()
    assert [call[:2] for call in sandbox.calls if call[0] == "start"] == [("start", "none")]
    assert [call[:2] for call in sandbox.calls if call[0] == "/api/auto/config"] == [("/api/auto/config", {"enabled": True})]
    assert all(call[0] != "/api/control" for call in sandbox.calls)
    assert len(sandbox.browser) == 1 and "#token=" in sandbox.browser[0]
    assert sandbox.state["jobs"] == [{"state": "needs_human", "body": "preserve unknown outcome"}]
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_connect_can_disable_auto_without_clearing_pause_suspension_or_individual_holds(sandbox):
    sandbox.state.update(paused=True, agents=[{"state": "needs_human"}, {"state": "paused"}])
    sandbox.state["auto"].update(enabled=True, suspended=True, paused=True)
    assert sandbox.actions.connect(("claude",), auto_enroll=False) == "잠시 멈춤"
    assert sandbox.state["auto"] == {"enabled": False, "suspended": True, "paused": True}
    assert sandbox.state["paused"] and sandbox.state["agents"] == [{"state": "needs_human"}, {"state": "paused"}]
    assert all(call[0] != "/api/control" for call in sandbox.calls)


def test_matching_running_broker_is_reused_and_open_does_not_install_or_enable_auto(sandbox, monkeypatch):
    sandbox.write_runtime()
    monkeypatch.setattr(launcher, "prepare", lambda *args, **kwargs: pytest.fail("Reopening must not install"))
    sandbox.actions.open_console()
    assert all(call[0] not in {"start", "/api/auto/config", "/api/control"} for call in sandbox.calls)
    assert len(sandbox.browser) == 1 and list(sandbox.home.iterdir()) == []


def test_valid_offline_metadata_delegates_recovery_to_supported_start_once(sandbox):
    sandbox.write_runtime()
    sandbox.health["error"] = ConnectionRefusedError("fixture private detail")
    sandbox.actions.open_console()
    assert len([call for call in sandbox.calls if call[0] == "start"]) == 1
    assert all(call[0] != "/api/control" for call in sandbox.calls)
    assert len(sandbox.browser) == 1


@pytest.mark.parametrize("invalid", ["workspace", "url", "pid", "nonce", "json", "generation"])
def test_invalid_or_mismatching_runtime_never_installs_starts_or_controls(sandbox, monkeypatch, invalid):
    runtime = sandbox.write_runtime()
    path = sandbox.workspace / ".sharebus/private/runtime.json"
    if invalid == "generation":
        sandbox.health["nonce"] = "fixture-different-generation"
    elif invalid == "json":
        path.write_text("broken", encoding="utf-8")
    else:
        runtime[invalid] = {"workspace": str(sandbox.home), "url": "https://external.invalid",
                            "pid": -1, "nonce": "short"}[invalid]
        path.write_text(json.dumps(runtime), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(launcher, "prepare", lambda *args, **kwargs: pytest.fail("Identity failure must precede installation"))
    with pytest.raises(launcher.LauncherError) as caught:
        sandbox.actions.connect(("codex",))
    assert caught.value.code == "identity"
    assert not sandbox.browser and all(call[0] == "health" for call in sandbox.calls)
    assert path.read_bytes() == before and list(sandbox.home.iterdir()) == []


def test_separate_resume_click_sends_only_global_existing_control_once(sandbox):
    sandbox.state.update(paused=True, agents=[{"state": "needs_human"}, {"state": "paused"}])
    sandbox.state["auto"].update(paused=True, suspended=True)
    assert sandbox.actions.resume() == "잠시 멈춤"
    controls = [call[:2] for call in sandbox.calls if call[0] == "/api/control"]
    assert controls == [("/api/control", {"action": "resume"})]
    assert sandbox.state["agents"] == [{"state": "needs_human"}, {"state": "paused"}]
    assert sandbox.state["auto"]["suspended"] and not sandbox.browser
    assert all(call[0] != "/api/auto/config" for call in sandbox.calls)


def test_existing_sharebus_conflict_is_preserved_and_has_sanitized_concrete_scope(sandbox):
    target = sandbox.home / ".codex/config.toml"
    target.parent.mkdir()
    original = b'[mcp_servers.sharebus]\ncommand = "private-old-command"\nargs = []\n'
    target.write_bytes(original)
    with pytest.raises(launcher.LauncherError) as caught:
        sandbox.actions.connect(("codex",))
    assert caught.value.code == "configuration"
    details = launcher.diagnostic_details(caught.value, sandbox.actions.scope)
    assert ".codex/config.toml" in details and "codex_update" in details
    assert "private-old-command" not in details and str(sandbox.home) not in details
    assert target.read_bytes() == original and sandbox.calls == []
    assert list(sandbox.workspace.iterdir()) == []


def test_replaced_workspace_blocks_every_action_before_io(sandbox):
    original = sandbox.workspace.with_name("original-workspace")
    sandbox.workspace.rename(original)
    sandbox.workspace.mkdir()
    for action in (lambda: sandbox.actions.connect(("codex",)), sandbox.actions.open_console, sandbox.actions.resume):
        with pytest.raises(launcher.LauncherError) as caught:
            action()
        assert caught.value.code == "workspace"
    assert sandbox.calls == [] and list(sandbox.home.iterdir()) == []


def test_workspace_change_during_prepare_blocks_apply_and_service(sandbox, monkeypatch):
    original_prepare = launcher.prepare

    def changed(*args, **kwargs):
        changes = original_prepare(*args, **kwargs)
        sandbox.workspace.rename(sandbox.workspace.with_name("original-workspace"))
        sandbox.workspace.mkdir()
        return changes

    monkeypatch.setattr(launcher, "prepare", changed)
    with pytest.raises(launcher.LauncherError):
        sandbox.actions.connect(("claude",))
    assert sandbox.calls == [] and list(sandbox.home.iterdir()) == []


def test_action_captures_workspace_apps_and_auto_choice_before_background_work(gui, sandbox, monkeypatch):
    gui.selection = sandbox.selection
    gui.dialogs.askokcancel = lambda *args, **kwargs: True
    pending, observed = [], []
    monkeypatch.setattr(launcher.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: pending.append(kwargs["target"])))

    class Actions:
        scope = ()

        def __init__(self, selection):
            self.selection = selection

        def connect(self, providers, *, auto_enroll):
            observed.append((self.selection, providers, auto_enroll))
            return "앱 연결 대기"

    monkeypatch.setattr(launcher, "WorkspaceActions", Actions)
    gui.act("connect")
    gui.selection = None
    gui.providers["codex"].set(False)
    gui.auto_enroll.set(False)
    pending[0]()
    gui.poll()
    assert observed == [(sandbox.selection, ("codex",), True)]
    assert not gui.busy and sandbox.calls == []


def test_errors_never_display_raw_secret_path_url_or_exception_text(gui, sandbox):
    raw = "fixture-token native-session-identity C:\\private\\operator.token http://127.0.0.1/#token=fixture-token"
    gui.show_error(RuntimeError(raw))
    assert raw not in gui.details and raw not in gui.status.get()
    assert "fixture-token" not in gui.details and "native-session-identity" not in gui.details
    assert str(sandbox.workspace) not in gui.details


def test_headless_help_never_imports_tk_or_starts_services(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(launcher, "run_gui", lambda: pytest.fail("Help must not initialize a GUI"))
    with pytest.raises(SystemExit) as caught:
        launcher.main(["--help"])
    assert caught.value.code == 0 and "--help" in capsys.readouterr().out
    assert sandbox.calls == [] and list(sandbox.home.iterdir()) == []


def test_no_tk_gui_start_failure_has_safe_korean_message(sandbox, monkeypatch, capsys):
    def missing():
        raise ImportError("fixture-private-module-path")

    monkeypatch.setattr(launcher, "run_gui", missing)
    assert launcher.main([]) == 1
    message = capsys.readouterr().err
    assert "Tk" in message and "fixture-private-module-path" not in message
    assert sandbox.calls == []


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_real_client_start_and_enable_do_not_register_unselected_sidecar(sandbox, monkeypatch, provider):
    import sidecar_registry
    registered = []
    monkeypatch.setattr(sidecar_registry, "register_workspace", lambda *args, **kwargs: registered.append(args))
    runtime = sandbox.write_runtime()
    (sandbox.workspace / ".sharebus/private/runtime.json").unlink()
    monkeypatch.setattr(sharebus.Client, "start", REAL_CLIENT_START)
    monkeypatch.setattr(sharebus.Client, "runtime", lambda client, **kwargs: runtime)
    sandbox.actions.connect((provider,))
    assert registered == [] and not (sandbox.home / ".gemini").exists()


def test_real_client_start_keeps_legacy_sidecar_registration_default(sandbox, monkeypatch):
    import sidecar_registry
    registered = []
    runtime = sandbox.write_runtime()
    monkeypatch.setattr(sidecar_registry, "register_workspace", lambda *args, **kwargs: registered.append(args))
    monkeypatch.setattr(sharebus.Client, "runtime", lambda client, **kwargs: runtime)
    assert REAL_CLIENT_START(sharebus.Client(sandbox.workspace)) == runtime
    assert registered == [(sandbox.workspace, runtime)]


@pytest.mark.parametrize("running", [False, True])
def test_selected_antigravity_uses_real_registration_once_in_selected_home(sandbox, monkeypatch, tmp_path, running):
    import sidecar_registry
    wrong_registry = tmp_path / "different-home-registry"
    monkeypatch.setattr(sidecar_registry, "REGISTRY", wrong_registry)
    monkeypatch.setattr(sidecar_registry, "build_opener", sharebus.build_opener)
    if running:
        sandbox.write_runtime()
    sandbox.actions.connect(("antigravity",))
    registry = sandbox.home / ".gemini/config/sidecars/sharebus/workspaces"
    assert len(list(registry.glob("*.json"))) == 1
    assert not wrong_registry.exists()
    assert not (sandbox.home / ".codex").exists() and not (sandbox.home / ".claude.json").exists()
    assert sandbox.starts == ([] if running else [{"register_sidecar": False}])


def make_directory_link(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_selected_home_config_junction_is_rejected_before_outside_read_or_write(sandbox, tmp_path, monkeypatch):
    outside = tmp_path / "outside-selected-home"
    outside.mkdir()
    original = outside / "config.toml"
    original.write_bytes(b'model = "outside-preserved"\n')
    link = sandbox.home / ".codex"
    make_directory_link(outside, link)
    read = Path.read_bytes

    def protected(path):
        assert path != link / "config.toml" and not path.is_relative_to(outside), "Outside configuration must not be read"
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", protected)
    try:
        with pytest.raises(launcher.LauncherError) as caught:
            sandbox.actions.connect(("codex",))
        assert caught.value.code == "configuration"
        assert read(original) == b'model = "outside-preserved"\n'
        assert list(outside.iterdir()) == [original] and sandbox.calls == []
        assert list(sandbox.workspace.iterdir()) == []
    finally:
        link.rmdir() if sys.platform == "win32" else link.unlink()


def test_unselected_home_junction_is_neither_inspected_nor_modified(sandbox, tmp_path, monkeypatch):
    outside = tmp_path / "unselected-host"
    outside.mkdir()
    marker = outside / "preserved.txt"
    marker.write_bytes(b"untouched")
    link = sandbox.home / ".gemini"
    make_directory_link(outside, link)
    original = Path.lstat

    def protected(path, *args, **kwargs):
        assert path != link and not path.is_relative_to(link), "Unselected host must not be inspected"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", protected)
    try:
        sandbox.actions.connect(("codex",))
        assert marker.read_bytes() == b"untouched" and list(outside.iterdir()) == [marker]
        assert sandbox.starts == [{"register_sidecar": False}]
    finally:
        link.rmdir() if sys.platform == "win32" else link.unlink()


def test_existing_selected_directory_replacement_during_apply_stops_remaining_writes(sandbox, monkeypatch):
    config = sandbox.home / ".codex"
    config.mkdir()
    original = config / "config.toml"
    original.write_bytes(b'model = "keep"\n')
    preserved = sandbox.home / "original-codex"
    real_replace = install.os.replace
    replacements = []

    def replaced(source, target):
        real_replace(source, target)
        replacements.append(Path(target))
        config.rename(preserved)
        config.mkdir()

    monkeypatch.setattr(install.os, "replace", replaced)
    with pytest.raises(launcher.LauncherError) as caught:
        sandbox.actions.connect(("codex",))
    assert caught.value.code == "configuration"
    assert len(replacements) == 1 and replacements[0].is_relative_to(sandbox.workspace)
    assert (preserved / "config.toml").read_bytes() == b'model = "keep"\n'
    assert list(config.iterdir()) == [] and sandbox.calls == []


@pytest.mark.parametrize("state,label", [("offline", "연결 끊김"), ("unavailable", "사용 불가"),
                                         ("starting", "연결 중"), ("waiting_for_user", "사용자 답변 대기")])
def test_actual_nonready_agent_states_are_not_reported_connected(state, label):
    assert launcher.connection_summary({"agents": [{"state": state}]}) == label


def test_thread_start_failure_restores_controls_and_allows_close(gui, sandbox, monkeypatch):
    gui.selection = sandbox.selection
    gui.dialogs.askokcancel = lambda *args, **kwargs: True

    def failed():
        raise RuntimeError("fixture-private-thread-start-error")

    monkeypatch.setattr(launcher.threading, "Thread", lambda **kwargs: SimpleNamespace(start=failed))
    gui.act("connect")
    assert gui.busy is False and all(widget.states[-1] == "!disabled" for widget in gui.controls)
    assert "fixture-private-thread-start-error" not in gui.details
    gui.close()
    assert gui.root.destroyed is True and sandbox.calls == []
