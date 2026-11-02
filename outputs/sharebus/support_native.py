"""The verified, instance-local Claude support profile; no business tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from adapters import NativeAdapter, TurnResult
from discovery import resolve_executable
from support_store import PROFILE_REASONS, SupportError, write_private


TOOL = "mcp__sharebus_guide__guide_diagnose"
VERIFIED_CLAUDE_VERSIONS = {"2.1.303"}


class GuideProfileUnavailable(Exception):
    """Pre-input profile failure, distinct from a real native approval request."""
    def __init__(self, reason_code: str):
        if reason_code not in PROFILE_REASONS:
            raise ValueError("Invalid guide profile failure")
        self.reason_code = reason_code
        super().__init__("The native support profile is unavailable.")


def verified_executable() -> str:
    """Probe only the supported profile; no model input or native identity."""
    executable = resolve_executable("claude")
    if not executable or (os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}):
        raise GuideProfileUnavailable("native_missing")
    try:
        version = subprocess.run([executable, "--version"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10, check=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise GuideProfileUnavailable("profile_unverified") from None
    match = re.match(r"^([0-9]+\.[0-9]+\.[0-9]+)(?:\s|$)", version.stdout.strip())
    if version.returncode or len(version.stdout) > 4096 or not match or match[1] not in VERIFIED_CLAUDE_VERSIONS:
        raise GuideProfileUnavailable("unsupported_version")
    return executable


class GuideNativeAdapter(NativeAdapter):
    """Reuse native delivery/subscription/approval logic with a positive tool profile.

    Native processes close after each turn; the exact persisted conversation ID,
    not a process or a new prompt transcript, provides conversational continuity.
    Each MCP process therefore receives only its own human job's capability.
    """
    def __init__(self, workspace: Path, context_file: Path, *, on_event,
                 on_reserved, on_identity, on_profile, on_approval=None):
        super().__init__("claude", workspace, "sharebus-guide", context_file,
                         on_event=on_event, on_approval=on_approval,
                         subscription_only=True, mcp_python=sys.executable)
        self.context_file = context_file
        self._on_reserved = on_reserved
        self._on_identity = on_identity
        self._on_profile = on_profile
        self._profile_ok = False
        self._planned_id = None

    def _command(self, resume_id: str | None) -> list[str]:
        # Version/flags and the finite MCP configuration are enforced before a
        # prompt can reach stdin. An observed init list is only extra defense.
        executable = verified_executable()
        config_path = self._logs / "guide-mcp.json"
        write_private(config_path, json.dumps({"mcpServers": {"sharebus_guide": {
            "command": self.mcp_python,
            "args": ["-I", "-B", str(Path(__file__).with_name("support_mcp.py")),
                     "--context-file", str(self.context_file)],
        }}}, ensure_ascii=False))
        command = [executable, "--print", "--input-format", "stream-json",
                   "--output-format", "stream-json", "--verbose",
                   "--mcp-config", str(config_path), "--allowedTools", TOOL,
                   "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                   "--disable-slash-commands", "--permission-prompts", "host",
                   "--prompt-suggestions", "false", "--replay-user-messages"]
        self._planned_id = resume_id or str(uuid.uuid4())
        self._expected_session = self._planned_id
        command.extend(["--resume" if resume_id else "--session-id", self._planned_id])
        return command

    def _check_subscription(self, *args, **kwargs) -> None:
        super()._check_subscription(*args, **kwargs)
        # Before Popen, but after a known subscription auth failure can stop
        # without reserving an unobserved conversation.
        if not self._planned_id:
            raise SupportError("stale", 409)
        self._on_reserved(self._planned_id)

    def _remember_session(self, value: object) -> None:
        if (not isinstance(value, str) or not value or len(value) > 256
                or value != self._expected_session):
            raise RuntimeError("The support native identity did not match.")
        # The base public session observer is intentionally best effort. This
        # private binding callback must commit or fail before accepting a result.
        self._on_identity(value)
        super()._remember_session(value)

    def _wire_event(self, value: dict) -> dict | None:
        if value.get("type") == "system" and value.get("subtype") == "init":
            tools = value.get("tools")
            valid = (isinstance(tools, list) and all(isinstance(tool, str) for tool in tools)
                     and TOOL in tools and set(tools) <= {TOOL, "EndConversation"}
                     and value.get("session_id") == self._expected_session)
            if valid:
                try:
                    self._on_identity(value["session_id"])
                    self._on_profile()
                    self._profile_ok = True
                except Exception:
                    valid = False
            if not valid:
                self._reader_state["error"] = "The support native tool profile did not match."
                process = self._process
                if process is not None and process.poll() is None:
                    try:
                        process.terminate()
                    except OSError:
                        pass
                return None
        return super()._wire_event(value)

    def turn(self, prompt: str, timeout: float = 180) -> TurnResult:
        result = super().turn(prompt, timeout)
        if result.success and not self._profile_ok:
            return TurnResult("", result.session_id, False, True,
                              "The support native profile was not observed.", result.native_turn_id,
                              result.input_attempted, result.accepted, True, "delivery_unknown")
        return result
