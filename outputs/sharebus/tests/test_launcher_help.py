"""Help entry uses a separate service and preserves selected-room controls."""
from __future__ import annotations

from types import ModuleType, SimpleNamespace
import sys

import pytest

from test_launcher import gui, sandbox, launcher  # Reuse the same headless Tk/OS fixtures.


def support_stub(monkeypatch, calls, *, failure=None):
    module = ModuleType("support_controller")

    class SupportClient:
        def __init__(self, workspace, *, home=None):
            calls.append(("construct", workspace, home))

        def start(self):
            calls.append(("start",))
            if failure:
                raise failure
            return {"guide": {"state": "ready", "can_send": True}}

    module.SupportClient = SupportClient
    monkeypatch.setitem(sys.modules, "support_controller", module)


def queue_thread(monkeypatch):
    pending = []
    monkeypatch.setattr(launcher.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: pending.append(kwargs["target"])))
    return pending


def test_help_entry_is_available_without_any_discovered_provider(gui, sandbox):
    assert any(widget.options.get("text") == "도움과 이야기하기" for widget in gui.controls)
    assert not gui.providers["claude"].get()
    assert sandbox.calls == []


def test_help_requires_a_selected_folder_and_never_prompts_for_identity(gui, sandbox, monkeypatch):
    calls = []
    support_stub(monkeypatch, calls)
    gui.act("help")
    assert not gui.busy and calls == []
    assert "폴더" in gui.status.get()
    assert list(sandbox.home.iterdir()) == [] and list(sandbox.workspace.iterdir()) == []


def test_cancelled_folder_picker_never_opens_help(gui, sandbox, monkeypatch):
    calls = []
    support_stub(monkeypatch, calls)
    gui.choose_workspace()
    assert gui.selection is None and calls == []
    assert list(sandbox.home.iterdir()) == [] and list(sandbox.workspace.iterdir()) == []


def test_help_captures_folder_and_uses_only_support_without_confirmation(gui, sandbox, monkeypatch):
    calls = []
    support_stub(monkeypatch, calls)
    gui.dialogs.askokcancel = lambda *args, **kwargs: pytest.fail("Opening help needs no extra approval click")
    pending = queue_thread(monkeypatch)
    gui.selection = sandbox.selection
    for value in gui.providers.values():
        value.set(False)
    gui.act("help")
    assert gui.busy and len(pending) == 1
    assert all(widget.states[-1] == "disabled" for widget in gui.controls)
    gui.selection = None
    pending[0]()
    gui.poll()
    assert calls == [("construct", sandbox.workspace, None), ("start",)]
    assert not gui.busy and all(widget.states[-1] == "!disabled" for widget in gui.controls)
    assert "도움 대화" in gui.status.get() and "승인" not in gui.status.get()
    assert sandbox.calls == [] and sandbox.starts == [] and sandbox.browser == []
    assert list(sandbox.home.iterdir()) == [] and list(sandbox.workspace.iterdir()) == []


def test_replaced_folder_is_refused_before_support_import_or_start(gui, sandbox, monkeypatch):
    calls = []
    support_stub(monkeypatch, calls)
    pending = queue_thread(monkeypatch)
    gui.selection = sandbox.selection
    gui.act("help")
    sandbox.workspace.rename(sandbox.workspace.with_name("old-room"))
    sandbox.workspace.mkdir()
    pending[0]()
    gui.poll()
    assert not gui.busy and calls == []
    assert "폴더" in gui.status.get() and sandbox.calls == []


def test_help_keeps_explicit_home_and_starts_no_business_service(sandbox, monkeypatch):
    calls = []
    support_stub(monkeypatch, calls)
    result = sandbox.actions.open_help()
    assert calls == [("construct", sandbox.workspace, sandbox.home), ("start",)]
    assert result.startswith("도움 대화")
    assert sandbox.calls == [] and sandbox.starts == [] and sandbox.browser == []


def test_missing_support_module_has_help_error_without_falling_back(sandbox, monkeypatch):
    monkeypatch.setitem(sys.modules, "support_controller", None)
    with pytest.raises(launcher.LauncherError) as caught:
        sandbox.actions.open_help()
    assert caught.value.code == "help"
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)
    assert "support_controller" not in str(caught.value)
    assert sandbox.calls == [] and sandbox.starts == [] and sandbox.browser == []


def test_support_error_restores_controls_and_never_shows_private_values(gui, sandbox, monkeypatch):
    calls = []
    private = "fixture-token native-session-identity C:\\private\\operator.token"
    support_stub(monkeypatch, calls, failure=RuntimeError(private))
    pending = queue_thread(monkeypatch)
    gui.selection = sandbox.selection
    gui.act("help")
    pending[0]()
    gui.poll()
    assert not gui.busy
    assert all(widget.states[-1] == "!disabled" for widget in gui.controls)
    assert "도움" in gui.status.get()
    assert private not in gui.status.get() and private not in gui.details
    assert "fixture-token" not in gui.details and "native-session-identity" not in gui.details
    assert sandbox.calls == [] and sandbox.starts == []


def test_thread_failure_never_starts_support_and_keeps_launcher_usable(gui, sandbox, monkeypatch):
    calls = []
    support_stub(monkeypatch, calls)
    gui.selection = sandbox.selection

    def fail_start():
        raise RuntimeError("private-start-failure")

    monkeypatch.setattr(launcher.threading, "Thread", lambda **kwargs: SimpleNamespace(start=fail_start))
    gui.act("help")
    assert not gui.busy and calls == []
    assert all(widget.states[-1] == "!disabled" for widget in gui.controls)
    assert "private-start-failure" not in gui.status.get()
    gui.close()
    assert gui.root.destroyed


def test_cli_help_does_not_import_support_service(sandbox, monkeypatch, capsys):
    # --help must retain its existing exit-before-GUI behavior.
    monkeypatch.delitem(sys.modules, "support_controller", raising=False)
    monkeypatch.setattr(launcher, "run_gui", lambda: pytest.fail("No GUI during --help"))
    with pytest.raises(SystemExit) as caught:
        launcher.main(["--help"])
    assert caught.value.code == 0 and "--help" in capsys.readouterr().out
    assert "support_controller" not in sys.modules
    assert sandbox.calls == [] and list(sandbox.home.iterdir()) == []
