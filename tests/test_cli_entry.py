"""Installed CLI entry contracts; distinct from the runtime GUI launcher suite."""

from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "src/sharebus_cli/__init__.py"
SPEC = spec_from_file_location("release_launcher_under_test", SOURCE)
assert SPEC is not None and SPEC.loader is not None
launcher = module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


@pytest.mark.parametrize("arguments", [[], ["gui"], ["--help"], ["--workspace", "a folder/작업실", "auto", "status"]])
def test_launcher_passes_literal_arguments_and_exit_status(tmp_path, monkeypatch, arguments):
    runtime = tmp_path / "installed runtime"
    runtime.mkdir()
    initializer = runtime / "__init__.py"
    initializer.write_text('"""fixture"""\n', encoding="utf-8")
    script = runtime / "sharebus.py"
    script.write_text("# Fixture, never executed.\n", encoding="utf-8")
    gui_script = runtime / "launcher.py"
    gui_script.write_text("# GUI fixture, never executed.\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "find_spec", lambda name: SimpleNamespace(origin=str(initializer)))
    calls = []

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.main(arguments) == 7
    expected = gui_script if not arguments or arguments[0] == "gui" else script
    passed = arguments[1:] if arguments[:1] == ["gui"] else arguments
    assert calls == [([launcher.sys.executable, "-B", str(expected), *passed], {"check": False})]


@pytest.mark.parametrize("spec", [None, SimpleNamespace(origin=None)])
def test_missing_runtime_fails_before_process_launch(monkeypatch, spec):
    monkeypatch.setattr(launcher, "find_spec", lambda name: spec)
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: pytest.fail("No process expected"))
    with pytest.raises(SystemExit, match="runtime is missing"):
        launcher.main(["--help"])


def test_incomplete_runtime_fails_before_process_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "find_spec", lambda name: SimpleNamespace(origin=str(tmp_path / "__init__.py")))
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: pytest.fail("No process expected"))
    with pytest.raises(SystemExit, match="runtime is incomplete"):
        launcher.main(["--help"])
