"""Pure hook-output formatting; no hook lifecycle or native process is invoked."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hooks_bridge


def command_from(text):
    return json.loads(next(line for line in text.splitlines() if line.startswith('["')))


@pytest.mark.parametrize("event", ["SessionStart", "PreInvocation"])
def test_antigravity_context_uses_one_ephemeral_step_without_human_authority(tmp_path, event):
    workspace = tmp_path / "한글 회의 공간"
    context = workspace / ".sharebus/private/fixture-context.json"
    output = hooks_bridge.context_output("antigravity", event, context, workspace)
    assert set(output) == {"injectSteps"}
    assert isinstance(output["injectSteps"], list) and len(output["injectSteps"]) == 1
    step = output["injectSteps"][0]
    assert set(step) == {"ephemeralMessage"}
    text = step["ephemeralMessage"]
    assert isinstance(text, str) and text.startswith("[SHAREBUS_NATIVE_CONTEXT]")
    assert json.dumps(str(workspace), ensure_ascii=True) in text
    assert json.dumps(str(context), ensure_ascii=True) in text
    assert "@WORKSPACE@" not in text and "@CONTEXT_FILE@" not in text and "@CONNECT_COMMAND@" not in text
    assert "never creates a new human goal" in text
    assert "Never route this lifecycle context itself as typed human input." in text
    command = command_from(text)
    assert command[command.index("--provider") + 1] == "antigravity"
    assert command[command.index("--context-file") + 1] == str(context)
    assert command[command.index("--workspace") + 1] == str(workspace)
    assert not context.exists()


@pytest.mark.parametrize("event", [
    "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd", "StopFailure",
    "PreToolUse", "PostToolUse", "PostInvocation", "unknown",
])
def test_other_antigravity_events_return_empty_before_template_io(tmp_path, monkeypatch, event):
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("Unexpected template read"))
    assert hooks_bridge.context_output("antigravity", event, tmp_path / "context.json", tmp_path) == {}


def test_missing_antigravity_context_or_workspace_returns_empty_before_template_io(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("Unexpected template read"))
    for event in ("SessionStart", "PreInvocation"):
        assert hooks_bridge.context_output("antigravity", event, None, tmp_path) == {}
        assert hooks_bridge.context_output("antigravity", event, tmp_path / "context.json", None) == {}


@pytest.mark.parametrize("provider,event", [
    ("claude", "SessionStart"), ("claude", "UserPromptSubmit"), ("codex", "SessionStart"),
])
def test_claude_and_codex_keep_their_existing_output_shape(tmp_path, provider, event):
    output = hooks_bridge.context_output(provider, event, tmp_path / "context.json", tmp_path)
    assert set(output) == {"hookSpecificOutput"}
    hook = output["hookSpecificOutput"]
    assert set(hook) == {"hookEventName", "additionalContext"}
    assert hook["hookEventName"] == event
    assert isinstance(hook["additionalContext"], str)
    assert command_from(hook["additionalContext"])[-3:] == [provider, "--workspace", str(tmp_path)]


@pytest.mark.parametrize("inherited_B,packaged", [(False, False), (True, False), (False, True)])
def test_antigravity_keeps_unicode_arguments_and_existing_bytecode_flag(tmp_path, monkeypatch, inherited_B, packaged):
    monkeypatch.setattr(sys, "dont_write_bytecode", inherited_B)
    if packaged:
        monkeypatch.setenv("AVALOKITA_PACKAGED_RUNTIME", "1")
    else:
        monkeypatch.delenv("AVALOKITA_PACKAGED_RUNTIME", raising=False)
    workspace = tmp_path / "한글 공간 🙂"
    context = workspace / "문맥.json"
    output = hooks_bridge.context_output("antigravity", "PreInvocation", context, workspace)
    command = command_from(output["injectSteps"][0]["ephemeralMessage"])
    assert command[0] == sys.executable
    assert ("-B" in command) == (inherited_B or packaged)
    assert command[command.index("--context-file") + 1] == str(context)
    assert command[command.index("--workspace") + 1] == str(workspace)
    assert json.loads(json.dumps(output, ensure_ascii=False).encode("utf-8")) == output
