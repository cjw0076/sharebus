"""Resident native sessions for Sharebus. No model request is made while idle."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import copy
import json
import math
import os
from pathlib import Path
import queue
import re
import stat
import subprocess
import sys
import threading
import time
from typing import Callable
import uuid

from discovery import MANAGED_PROVIDERS, discover_native_sessions, resolve_executable


BUS_TOOLS = (
    "sharebus_join", "sharebus_status", "sharebus_send", "sharebus_read",
    "sharebus_publish", "sharebus_copy", "sharebus_ask_user", "sharebus_peer",
)

MAX_WIRE_BYTES = 1048576
MAX_MESSAGES = 64
MAX_PENDING = 64
MAX_OUTPUT_CHARS = 262144
MAX_PROMPT_CHARS = 131072
MAX_APPROVAL_BYTES = 65536
MAX_TURN_SECONDS = 3600
WRITE_TIMEOUT = 10.0
QUEUE_TIMEOUT = 0.5


class NativeNeedsHuman(RuntimeError):
    """The native request has not been answered by a human."""


@dataclass(frozen=True)
class TurnResult:
    text: str
    session_id: str | None
    success: bool
    needs_human: bool = False
    error: str | None = None
    native_turn_id: str | None = None
    input_attempted: bool = False
    accepted: bool = False
    delivery_unknown: bool = False
    status: str = "not_sent"


class NativeAdapter:
    """Own one child process and its exact resumable native conversation ID."""

    def __init__(
        self,
        provider: str,
        workspace: Path,
        agent_id: str,
        token_file: Path,
        on_event: Callable[[dict], None] | None = None,
        *,
        on_approval: Callable[[dict], None] | None = None,
        subscription_only: bool = False,
        mcp_python: str | Path | None = None,
    ):
        self.provider = "antigravity" if provider == "agy" else provider
        if self.provider not in MANAGED_PROVIDERS:
            raise ValueError("This provider has no verified native session adapter.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", agent_id):
            raise ValueError("Invalid agent ID.")
        self.workspace = Path(workspace).resolve()
        root_stat = self.workspace.stat()
        self._workspace_identity = (root_stat.st_dev, root_stat.st_ino)
        self.agent_id = agent_id
        self.token_file = Path(token_file).resolve()
        self.on_event = on_event
        self.on_approval = on_approval
        if not isinstance(subscription_only, bool):
            raise ValueError("subscription_only must be a boolean.")
        self.subscription_only = subscription_only
        if (os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" and mcp_python is not None
                and Path(mcp_python).resolve() != Path(sys.executable).resolve()):
            raise ValueError("Packaged MCP must use the bundled Python interpreter.")
        self.mcp_python = str(Path(mcp_python or sys.executable).resolve())
        if not Path(self.mcp_python).is_file() or (os.name == "nt" and Path(self.mcp_python).suffix.lower() in {".cmd", ".bat"}):
            raise ValueError("An existing native MCP Python executable is required.")
        self.startup_timeout = float(os.environ.get("SHAREBUS_STARTUP_TIMEOUT", "120"))
        if not math.isfinite(self.startup_timeout) or not 0 < self.startup_timeout <= 600:
            raise ValueError("SHAREBUS_STARTUP_TIMEOUT must be between 0 and 600 seconds.")
        self.session_id: str | None = None
        self._expected_session: str | None = None
        self._process: subprocess.Popen | None = None
        self._messages: queue.Queue = queue.Queue(maxsize=MAX_MESSAGES)
        self._pending: deque[dict] = deque()
        self._reader_state: dict = {}
        self._turn_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._request_id = 0
        self._turn_id: str | None = None
        self._interrupted = threading.Event()
        self._needs_human = False
        self._input_attempted = False
        self._accepted = False
        self._input_rejected = False
        self._delivery_unknown = False
        self._deadline: float | None = None
        self._approval_lock = threading.Lock()
        self._approval: dict | None = None
        self._logs = self._checked_directory(".sharebus", "private", "logs")

    def _check_workspace(self) -> None:
        for path in (self.workspace, *self.workspace.parents):
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or path.is_symlink():
                raise RuntimeError("Native workspace paths must be ordinary directories.")
        info = self.workspace.stat()
        if (info.st_dev, info.st_ino) != self._workspace_identity:
            raise RuntimeError("The native workspace directory has changed.")

    def _checked_directory(self, *parts: str) -> Path:
        self._check_workspace()
        directory = self.workspace
        for part in parts:
            directory = directory / part
            directory.mkdir(exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or directory.is_symlink():
                raise RuntimeError("Native configuration directories cannot be links.")
        self._check_workspace()
        return directory

    def _antigravity_mcp(self, server_args: list[str]) -> None:
        """Use documented workspace MCP config without a per-agent credential."""
        directory = self._checked_directory(".agents")
        config_path = directory / "mcp_config.json"
        expected = {"command": self.mcp_python, "args": server_args}
        if config_path.exists() or config_path.is_symlink():
            info = config_path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink != 1:
                raise RuntimeError("Native MCP configuration must be an ordinary file.")
            try:
                with config_path.open(encoding="utf-8") as source:
                    content = source.read(MAX_WIRE_BYTES + 1)
                if len(content.encode("utf-8")) > MAX_WIRE_BYTES:
                    raise ValueError("oversized")
                config = json.loads(content)
            except (OSError, ValueError):
                raise RuntimeError("Native workspace MCP configuration could not be validated.") from None
            if not isinstance(config, dict) or not isinstance(config.get("mcpServers"), dict) or config["mcpServers"].get("sharebus") != expected:
                raise RuntimeError("An existing workspace MCP configuration requires an explicit compatible Sharebus entry.")
            self._check_workspace()
            return
        self._check_workspace()
        # Exclusive creation never overwrites another app's configuration. The
        # child inherits its private managed identity; this shared file does not.
        with config_path.open("x", encoding="utf-8") as target:
            target.write(json.dumps({"mcpServers": {"sharebus": expected}}, ensure_ascii=False))
        self._check_workspace()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process and self._process.poll() is None else None

    def _emit(self, event: dict, *, required: bool = False) -> None:
        # This observer is public. Wire payloads, tool inputs, usage, and private
        # reasoning never cross it. Approval details use a separate private API.
        fields = {
            "session": {"session_id"},
            "turn": {"session_id", "turn_id", "status"},
            "input_attempt": {"session_id", "turn_id"},
            "status": {"session_id", "turn_id", "status"},
            "assistant_text": {"session_id", "turn_id", "text"},
            "needs_human": {"session_id", "turn_id", "reason"},
        }.get(event.get("type"))
        if fields is None:
            return
        public = {"type": event["type"], "provider": self.provider}
        for name in fields:
            value = event.get(name)
            limit = MAX_OUTPUT_CHARS if name == "text" else 256
            if isinstance(value, str) and value and len(value) <= limit:
                public[name] = value
        if self.on_event:
            try:
                self.on_event(public)
            except Exception:
                if required:
                    raise RuntimeError("Native lifecycle receipt could not be recorded.") from None

    def _accepted_turn(self, turn_id: object = None) -> None:
        previous_turn = self._turn_id
        if turn_id is not None:
            if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 256:
                raise RuntimeError("Native client did not return a valid turn ID.")
            if self._turn_id and self._turn_id != turn_id:
                raise RuntimeError("Native client returned a different turn ID.")
            self._turn_id = turn_id
        first = not self._accepted
        self._accepted = True
        if first or self._turn_id != previous_turn:
            self._emit({"type": "turn" if self._turn_id else "status", "status": "accepted",
                        "session_id": self.session_id, "turn_id": self._turn_id}, required=True)

    def _result(self, text: str, success: bool, error: str | None = None,
                *, status: str | None = None, unknown: bool = False) -> TurnResult:
        if len(text) > MAX_OUTPUT_CHARS:
            raise RuntimeError("Native public output exceeded its size limit.")
        self._delivery_unknown = self._delivery_unknown or unknown
        state = status or ("completed" if success else "needs_human" if self._needs_human else "failed")
        if text:
            self._emit({"type": "assistant_text", "session_id": self.session_id,
                        "turn_id": self._turn_id, "text": text})
        self._emit({"type": "status", "session_id": self.session_id,
                    "turn_id": self._turn_id, "status": state})
        return TurnResult(text, self.session_id, success, self._needs_human, error,
                          self._turn_id, self._input_attempted, self._accepted, unknown, state)

    def _remember_session(self, value: object) -> None:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise RuntimeError("Native client did not return a valid session ID.")
        if self._expected_session and value != self._expected_session:
            raise RuntimeError("Native resume returned a different session ID; session adoption refused.")
        if self.session_id != value:
            self.session_id = value
            self._emit({"type": "session", "provider": self.provider, "session_id": value})

    def _command(self, resume_id: str | None) -> list[str]:
        executable = resolve_executable(self.provider)
        if not executable:
            raise FileNotFoundError("The native client executable was not found.")
        if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
            raise RuntimeError("Native Windows executable required; shell wrappers are not launched.")
        server_args = [
            *(["-B"] if os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or sys.dont_write_bytecode else []),
            str(Path(__file__).resolve().with_name("mcp_server.py")),
            "--workspace", str(self.workspace), "--provider", self.provider,
        ]
        native_tools = os.environ.get("SHAREBUS_NATIVE_TOOLS", "workspace") != "relay"
        if self.provider == "codex":
            command = [executable, "app-server", "--listen", "stdio://"]
            config = {
                "mcp_servers.sharebus.command": self.mcp_python,
                "mcp_servers.sharebus.args": server_args,
                # Codex filters the environment inherited by stdio MCP children.
                "mcp_servers.sharebus.env_vars": [
                    "SHAREBUS_MANAGED", "SHAREBUS_WORKSPACE",
                    "SHAREBUS_AGENT_ID", "SHAREBUS_AGENT_TOKEN_FILE",
                ],
                "mcp_servers.sharebus.required": True,
            }
            for name in BUS_TOOLS:
                config[f"mcp_servers.sharebus.tools.{name}.approval_mode"] = "approve"
            for key, value in config.items():
                command.extend(["-c", key + "=" + json.dumps(value)])
            return command
        if self.provider == "claude":
            self._logs = self._checked_directory(".sharebus", "private", "logs")
            config_path = self._logs / (self.agent_id + "-mcp.json")
            if config_path.exists() or config_path.is_symlink():
                info = config_path.lstat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink != 1:
                    raise RuntimeError("Native MCP configuration must be an ordinary file.")
            config_path.write_text(json.dumps({"mcpServers": {"sharebus": {
                "command": self.mcp_python, "args": server_args,
            }}}), encoding="utf-8")
            self._check_workspace()
            command = [
                executable, "--print", "--input-format", "stream-json",
                "--output-format", "stream-json", "--verbose",
                "--mcp-config", str(config_path),
                "--allowedTools", ",".join("mcp__sharebus__" + name for name in BUS_TOOLS),
                "--permission-prompts", "host", "--prompt-suggestions", "false",
                "--replay-user-messages",
            ]
            if not native_tools:
                command.extend(["--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands"])
            if resume_id:
                command.extend(["--resume", resume_id])
            else:
                self._expected_session = str(uuid.uuid4())
                command.extend(["--session-id", self._expected_session])
            return command
        self._antigravity_mcp(server_args)
        command = [
            executable, "--input-format", "stream-json", "--output-format", "stream-json",
        ]
        if not native_tools:
            command.append("--disable-slash-commands")
        if resume_id:
            command.extend(["--conversation", resume_id])
        return command

    def _wire_event(self, value: dict) -> dict | None:
        """Keep only protocol frames needed in bounded, transient transport RAM."""
        if self.provider == "codex":
            if "id" in value:
                return value
            if value.get("method") in {"item/agentMessage/delta", "item/completed", "turn/completed"}:
                params = value.get("params") or {}
                if value.get("method") == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") != "agentMessage":
                        return None
                return value
            return None
        kind = value.get("type", value.get("event"))
        if self.provider == "claude":
            if kind in {"result", "control_request", "control_cancel_request", "error", "system"}:
                return value
            if kind in {"assistant", "user"}:
                selected = dict(value)
                message = value.get("message") or {}
                selected["message"] = {"content": [block for block in message.get("content", [])
                    if isinstance(block, dict) and block.get("type") in {"text", "tool_use", "tool_result"}]}
                # User echo content is not a public output. Tool receipts remain
                # transient for the existing exact-delivery proxy only.
                if kind == "user":
                    selected["message"]["content"] = [block for block in selected["message"]["content"]
                        if block.get("type") == "tool_result"]
                return selected
            return None
        if kind == "step_update":
            data = value.get("step_update") or {}
            return {"event": kind, "step_update": {key: data[key] for key in
                    ("conversation_id", "turn_id") if key in data}}
        return value if kind in {"result", "error"} else None

    def _reader(self, process: subprocess.Popen, messages: queue.Queue, stream,
                parse: bool, state: dict) -> None:
        try:
            while True:
                # Stderr is drained, never replicated into Avalokita logs.
                line = stream.readline(MAX_WIRE_BYTES + 1) if parse else stream.read(4096)
                if not line:
                    break
                if not parse:
                    continue
                if len(line.encode("utf-8")) > MAX_WIRE_BYTES:
                    state["error"] = "Native wire frame exceeded its size limit."
                    break
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(value, dict):
                    cancelled = None
                    if (self.provider == "codex" and value.get("method") == "serverRequest/resolved"
                            and (value.get("params") or {}).get("threadId") == self.session_id):
                        cancelled = (value.get("params") or {}).get("requestId")
                    elif self.provider == "claude" and value.get("type") == "control_cancel_request":
                        cancelled = value.get("request_id")
                    if isinstance(cancelled, (str, int)) and not isinstance(cancelled, bool):
                        state.setdefault("resolved", deque(maxlen=MAX_MESSAGES)).append(cancelled)
                        with self._approval_lock:
                            if self._process is process and self._approval and self._approval["view"]["request_id"] == cancelled:
                                self._approval["ready"].set()
                value = self._wire_event(value) if isinstance(value, dict) else None
                if value is not None:
                    try:
                        messages.put(value, timeout=QUEUE_TIMEOUT)
                    except queue.Full:
                        state["error"] = "Native response queue exceeded its size limit."
                        break
        except (OSError, ValueError, TypeError, AttributeError):
            state["error"] = "Native output stream could not be read."
        finally:
            if parse:
                state["eof"] = True

    def _check_subscription(self, executable: str | None = None, environment: dict | None = None) -> None:
        if not self.subscription_only:
            return
        if self.provider == "codex":
            account = self._request("account/read", {"refreshToken": False}, self.startup_timeout).get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                raise NativeNeedsHuman("This hosted session requires native ChatGPT subscription authentication.")
        elif self.provider == "claude":
            executable = executable or resolve_executable("claude")
            if not executable:
                raise NativeNeedsHuman("The native subscription authentication check is unavailable.")
            timeout = min(self.startup_timeout, self._deadline - time.monotonic() if self._deadline else self.startup_timeout)
            if timeout <= 0:
                raise TimeoutError("Native startup timed out.")
            try:
                result = subprocess.run([executable, "auth", "status"], env=environment,
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                    check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                status = json.loads(result.stdout) if len(result.stdout.encode("utf-8")) <= MAX_APPROVAL_BYTES else {}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                raise NativeNeedsHuman("The native subscription authentication check could not be completed.") from None
            if (result.returncode or not isinstance(status, dict) or status.get("loggedIn") is not True
                    or status.get("authMethod") not in {"claude.ai", "oauth_token"}):
                raise NativeNeedsHuman("This hosted session requires native Claude subscription authentication.")

    def start(self, resume_id: str | None = None) -> str | None:
        if resume_id is not None and (not isinstance(resume_id, str) or not resume_id or len(resume_id) > 256):
            raise ValueError("An exact nonempty native resume ID is required.")
        previous = self._deadline
        self._deadline = min(previous or float("inf"), time.monotonic() + self.startup_timeout)
        try:
            return self._start(resume_id)
        finally:
            self._deadline = previous

    def _start(self, resume_id: str | None = None) -> str | None:
        """Create or resume without a user turn. Resume never falls back to new."""
        self._check_workspace()
        if self.pid is not None:
            if resume_id and self.session_id and resume_id != self.session_id:
                raise RuntimeError("Adapter already owns another native session.")
            return self.session_id
        if resume_id and any(
            item.get("native_session_id") == resume_id
            and item.get("status") not in {"stopped", "exited", "completed", "dead"}
            for item in discover_native_sessions(self.provider)
        ):
            raise RuntimeError("This native session already has an active owner. Attach through its supported channel; a clone will not be created.")
        self._expected_session = resume_id
        self._interrupted.clear()
        self._messages = queue.Queue(maxsize=MAX_MESSAGES)
        self._reader_state = {}
        self._pending.clear()
        command = self._command(resume_id)
        # Native clients keep their own subscription authentication. Never
        # select an API-key/cloud-billing route inherited from another harness.
        api_variables = {
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "AGY_ADC_AUTH",
            "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID",
        }
        environment = {name: value for name, value in os.environ.items()
                       if not self.subscription_only or name.upper() not in api_variables}
        environment.update({
            "SHAREBUS_MANAGED": "1", "SHAREBUS_WORKSPACE": str(self.workspace),
            "SHAREBUS_AGENT_ID": self.agent_id, "SHAREBUS_AGENT_TOKEN_FILE": str(self.token_file),
        })
        if self.provider == "claude":
            self._check_subscription(command[0], environment)
        self._process = subprocess.Popen(
            command, cwd=self.workspace, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for stream, parse in [(self._process.stdout, True), (self._process.stderr, False)]:
            threading.Thread(
                target=self._reader,
                args=(self._process, self._messages, stream, parse, self._reader_state),
                daemon=True,
            ).start()
        if self.provider != "codex":
            return self.session_id
        try:
            self._request("initialize", {"clientInfo": {"name": "sharebus", "title": "Sharebus", "version": "0.1.0"}}, self.startup_timeout)
            self._write({"method": "initialized"})
            self._check_subscription()
            params: dict = {
                "cwd": str(self.workspace), "approvalPolicy": "on-request",
                "sandbox": "read-only" if os.environ.get("SHAREBUS_NATIVE_TOOLS") == "relay" else "workspace-write",
            }
            if resume_id:
                params.update({"threadId": resume_id, "excludeTurns": True})
            result = self._request("thread/resume" if resume_id else "thread/start", params, self.startup_timeout)
            self._remember_session(result.get("thread", {}).get("id"))
            return self.session_id
        except Exception:
            self.close()
            raise

    def _write(self, message: dict, *, input_attempt: bool = False) -> None:
        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise TimeoutError("Native input deadline expired.")
        encoded = json.dumps(message, ensure_ascii=False) + "\n"
        if len(encoded.encode("utf-8")) > MAX_WIRE_BYTES:
            raise RuntimeError("Native input frame exceeded its size limit.")
        process = self._process
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeError("Native process is not running.")
        if input_attempt:
            self._check_workspace()
            # A durable receipt must succeed before stdin can be touched.
            self._emit({"type": "input_attempt", "session_id": self.session_id,
                        "turn_id": self._turn_id}, required=True)
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise TimeoutError("Native input deadline expired.")
            self._input_attempted = True
        finished = threading.Event()
        outcome: list[bool] = []

        def write() -> None:
            try:
                with self._write_lock:
                    if (self._interrupted.is_set() or self._reader_state.get("error") or process.poll() is not None
                            or (self._deadline is not None and time.monotonic() >= self._deadline)):
                        raise OSError("Native transport is closed")
                    process.stdin.write(encoded)
                    process.stdin.flush()
                outcome.append(True)
            except (OSError, ValueError):
                outcome.append(False)
            finally:
                finished.set()

        threading.Thread(target=write, daemon=True).start()
        remaining = WRITE_TIMEOUT if self._deadline is None else min(WRITE_TIMEOUT, self._deadline - time.monotonic())
        if remaining <= 0 or not finished.wait(remaining):
            self._reader_state["error"] = "Native input write timed out."
            # An owned pipe writer must not remain blocked when a child stops
            # reading. No foreign PID or process tree is terminated here.
            try:
                process.terminate()
            except OSError:
                pass
            raise TimeoutError("Native input write timed out.")
        if not outcome or not outcome[0]:
            raise RuntimeError("Native input could not be written.")

    def _send_request(self, method: str, params: dict) -> int:
        self._request_id += 1
        identifier = self._request_id
        self._write({"id": identifier, "method": method, "params": params}, input_attempt=method == "turn/start")
        return identifier

    def _next(self, deadline: float, pending: bool = True) -> dict:
        while True:
            if self._interrupted.is_set():
                raise InterruptedError("Native turn was interrupted.")
            if self._reader_state.get("error"):
                raise RuntimeError(self._reader_state["error"])
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native client response timed out.")
            try:
                message = self._pending.popleft() if pending and self._pending else self._messages.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if self._reader_state.get("eof"):
                    raise RuntimeError("Native process exited before completing the response.")
                continue
            if message.get("_sharebus_eof"):
                raise RuntimeError("Native process exited before completing the response.")
            return message

    def _request(self, method: str, params: dict, timeout: float) -> dict:
        identifier = self._send_request(method, params)
        deadline = min(time.monotonic() + timeout, self._deadline or float("inf"))
        while True:
            message = self._next(deadline, pending=False)
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    if method == "turn/start":
                        self._input_rejected = True
                    raise RuntimeError(f"Native client rejected {method}.")
                result = message.get("result", {})
                if not isinstance(result, dict):
                    raise RuntimeError("Native client returned an invalid protocol result.")
                return result
            if "method" in message and "id" in message and method != "turn/start":
                self._approval_request(message, deadline)
            else:
                if len(self._pending) >= MAX_PENDING:
                    raise RuntimeError("Native pending response queue exceeded its size limit.")
                self._pending.append(message)

    def pending_approvals(self) -> list[dict]:
        """Private, selected human prompts; never the public event stream."""
        with self._approval_lock:
            return [copy.deepcopy(self._approval["view"])] if self._approval and not self._approval["ready"].is_set() else []

    def respond_approval(self, approval_id: str, result: dict) -> None:
        """Queue a real human's exact response, once, for the waiting request."""
        if not isinstance(result, dict) or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_APPROVAL_BYTES:
            raise ValueError("Invalid native approval response.")
        with self._approval_lock:
            approval = self._approval
            if not approval or approval["view"]["approval_id"] != approval_id or approval["ready"].is_set():
                raise ValueError("Native approval request is stale or already answered.")
            if approval["view"]["request_id"] in self._reader_state.get("resolved", ()):
                raise ValueError("The native host has already cleared this approval.")
            view, message = approval["view"], approval["message"]
            method = view["method"]
            if self.provider == "claude":
                allowed = {"behavior", "message", "answers"}
                if set(result) - allowed or result.get("behavior") not in {"allow", "deny"}:
                    raise ValueError("An explicit allow or deny response is required.")
                if result["behavior"] == "allow":
                    if view["details"].get("requires_native_review"):
                        raise ValueError("This tool input requires review in its native host.")
                    original = copy.deepcopy(message["request"]["input"])
                    if message["request"].get("tool_name") == "AskUserQuestion":
                        answers = result.get("answers")
                        questions = view["details"].get("questions", [])
                        if (not isinstance(answers, dict) or set(answers) != {q["question"] for q in questions}
                                or any(not isinstance(value, str) or not value.strip() for value in answers.values())):
                            raise ValueError("Every native question requires the human's answer.")
                        original["answers"] = answers
                    elif "answers" in result:
                        raise ValueError("This approval is not a user question.")
                    response = {"behavior": "allow", "updatedInput": original}
                else:
                    reason = result.get("message", "Declined by the human.")
                    if not isinstance(reason, str) or len(reason) > 8192:
                        raise ValueError("Invalid human denial message.")
                    response = {"behavior": "deny", "message": reason}
                packet = {"type": "control_response", "response": {"subtype": "success",
                          "request_id": message["request_id"], "response": response}}
                declined = result["behavior"] == "deny"
            else:
                if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                    if set(result) != {"decision"} or result.get("decision") not in {"accept", "decline", "cancel"}:
                        raise ValueError("A single-request native approval decision is required.")
                    offered = message["params"].get("availableDecisions")
                    if offered and result["decision"] not in offered:
                        raise ValueError("This native approval decision was not offered.")
                    declined = result["decision"] != "accept"
                elif method == "item/tool/requestUserInput":
                    answers = result.get("answers")
                    questions = view["details"].get("questions", [])
                    if (set(result) != {"answers"} or not isinstance(answers, dict)
                            or set(answers) != {q["id"] for q in questions}):
                        raise ValueError("Every exact native question requires an answer.")
                    for answer in answers.values():
                        if (not isinstance(answer, dict) or set(answer) != {"answers"}
                                or not isinstance(answer["answers"], list) or not 0 < len(answer["answers"]) <= 8
                                or any(not isinstance(text, str) or not text.strip() or len(text) > 8192 for text in answer["answers"])):
                            raise ValueError("Empty or invalid native question answers are not permitted.")
                    declined = False
                elif method == "mcpServer/elicitation/request":
                    if set(result) != {"action", "content"} or result.get("action") not in {"accept", "decline", "cancel"}:
                        raise ValueError("An explicit native elicitation response is required.")
                    if result["action"] != "accept" and result["content"] is not None:
                        raise ValueError("A declined elicitation cannot contain answers.")
                    if result["action"] == "accept" and message["params"].get("mode") in {"form", "openai/form"} and not isinstance(result["content"], dict):
                        raise ValueError("Native form elicitation requires explicit human answers.")
                    declined = result["action"] != "accept"
                else:
                    raise ValueError("This native approval requires its supported native host.")
                packet = {"id": message["id"], "result": copy.deepcopy(result)}
            approval["response"] = packet
            approval["declined"] = declined
            approval["ready"].set()

    def _approval_request(self, message: dict, deadline: float) -> None:
        """Wait for an exact human reply; absence is never a fabricated denial."""
        self._needs_human = True
        self._emit({"type": "needs_human", "session_id": self.session_id, "turn_id": self._turn_id,
                    "reason": "Native tool approval or user input is required."})
        if not self._input_attempted or not self.session_id:
            raise NativeNeedsHuman("Native approval has no current input binding.")
        if self.provider == "codex":
            params = message.get("params") or {}
            if (params.get("threadId") != self.session_id or not self._turn_id
                    or params.get("turnId") != self._turn_id):
                raise NativeNeedsHuman("Native approval does not match the current session and turn.")
            method, request_id = message.get("method"), message.get("id")
            details = {key: copy.deepcopy(params[key]) for key in (
                "command", "cwd", "reason", "grantRoot", "availableDecisions", "questions",
                "message", "mode", "requestedSchema", "url", "serverName",
            ) if key in params}
        else:
            request = message.get("request") or {}
            if request.get("subtype") != "can_use_tool" or not isinstance(request.get("input"), dict):
                raise NativeNeedsHuman("This native control request requires its supported native host.")
            method, request_id = "can_use_tool", message.get("request_id")
            tool_input = request["input"]
            selected = {"command", "description", "timeout", "file_path", "path", "old_string", "new_string",
                        "replace_all", "content", "questions", "answers", "offset", "limit"}
            details = {"tool_name": request.get("tool_name"), "tool_input": {
                key: copy.deepcopy(value) for key, value in tool_input.items() if key in selected},
                "requires_native_review": bool(set(tool_input) - selected)}
            if request.get("tool_name") == "AskUserQuestion":
                details["questions"] = copy.deepcopy(tool_input.get("questions", []))
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)) or len(str(request_id)) > 256:
            raise NativeNeedsHuman("Native approval request ID is invalid.")
        if method not in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                          "item/tool/requestUserInput", "mcpServer/elicitation/request", "can_use_tool"}:
            raise NativeNeedsHuman("This native approval requires its supported native host.")
        details["title"] = "Native user question" if details.get("questions") else "Native tool approval"
        details["summary"] = "This exact native request requires the human's response."
        if "questions" in details:
            if not isinstance(details["questions"], list) or not 0 < len(details["questions"]) <= 8:
                raise NativeNeedsHuman("Native user questions are invalid or exceed their limit.")
            fields = {"id", "header", "question", "options", "isOther", "isSecret", "multiSelect"}
            details["questions"] = [{key: copy.deepcopy(value) for key, value in question.items() if key in fields}
                for question in details["questions"] if isinstance(question, dict)]
            if not details["questions"] or any(not isinstance(q.get("question"), str) or not q["question"] for q in details["questions"]):
                raise NativeNeedsHuman("Native user question text is missing.")
            if self.provider == "codex" and (any(not isinstance(q.get("id"), str) or not q["id"] for q in details["questions"])
                    or len({q["id"] for q in details["questions"]}) != len(details["questions"])):
                raise NativeNeedsHuman("Native user question identities are invalid.")
            for question in details["questions"]:
                if "options" in question:
                    if not isinstance(question["options"], list) or len(question["options"]) > 32:
                        raise NativeNeedsHuman("Native question options are invalid or exceed their limit.")
                    question["options"] = [{key: option[key] for key in ("label", "description")
                        if isinstance(option.get(key), str)} for option in question["options"] if isinstance(option, dict)]
            if self.provider == "claude":
                details["tool_input"].pop("questions", None)
        view = {"approval_id": uuid.uuid4().hex, "provider": self.provider,
                "request_id": request_id, "method": method, "session_id": self.session_id,
                "details": details}
        if self._turn_id:
            view["turn_id"] = self._turn_id
        if len(json.dumps(view, ensure_ascii=False).encode("utf-8")) > MAX_APPROVAL_BYTES:
            raise NativeNeedsHuman("Native approval exceeded its size limit; use its native host.")
        approval = {"view": view, "message": message, "ready": threading.Event()}
        with self._approval_lock:
            if self._approval:
                raise NativeNeedsHuman("Another native approval is already pending.")
            self._approval = approval
        try:
            if request_id in self._reader_state.get("resolved", ()):
                raise NativeNeedsHuman("The native approval is no longer active.")
            if not self.on_approval:
                raise NativeNeedsHuman("Human approval is required; no approval handler is connected.")
            try:
                self.on_approval(copy.deepcopy(view))
            except Exception:
                raise NativeNeedsHuman("The native human approval could not be presented.") from None
            while not approval["ready"].wait(0.05):
                if self._interrupted.is_set():
                    raise InterruptedError("Native turn was interrupted.")
                if request_id in self._reader_state.get("resolved", ()):
                    raise NativeNeedsHuman("The native approval is no longer active.")
                if time.monotonic() >= deadline:
                    raise NativeNeedsHuman("The native approval expired without a human response.")
                if self._reader_state.get("eof") or self._reader_state.get("error"):
                    raise NativeNeedsHuman("The native approval transport is no longer available.")
            if (time.monotonic() >= deadline or self._interrupted.is_set()
                    or request_id in self._reader_state.get("resolved", ())):
                raise NativeNeedsHuman("The native approval is no longer active.")
            self._write(approval["response"])
            self._needs_human = bool(approval["declined"])
        finally:
            with self._approval_lock:
                if self._approval is approval:
                    self._approval = None

    def turn(self, prompt: str, timeout: float = 240) -> TurnResult:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("A nonempty prompt within the native input limit is required.")
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_TURN_SECONDS:
            raise ValueError("Timeout must be finite and within the native turn limit.")
        if not self._turn_lock.acquire(blocking=False):
            return TurnResult("", self.session_id, False, True,
                              "The native session already has an active input.", status="not_sent")
        try:
            if self._delivery_unknown:
                return TurnResult("", self.session_id, False, True,
                    "An earlier native input has an unknown outcome; reconcile it before sending again.",
                    delivery_unknown=True, status="delivery_unknown")
            self._interrupted.clear()
            self._needs_human = False
            self._input_attempted = False
            self._accepted = False
            self._input_rejected = False
            self._turn_id = None
            self._deadline = time.monotonic() + timeout
            try:
                if self.pid is None:
                    self.start(self.session_id)
                elif self.provider == "codex":
                    self._check_subscription()
                if self.provider == "codex":
                    return self._codex_turn(prompt, timeout)
                return self._stream_turn(prompt, timeout)
            except (RuntimeError, OSError, TimeoutError, InterruptedError) as error:
                interrupted = isinstance(error, InterruptedError)
                if not interrupted:
                    self.close()
                unknown = self._input_attempted and not self._input_rejected
                self._needs_human = self._needs_human or unknown or isinstance(error, NativeNeedsHuman)
                # Exception text from callbacks/OS may contain private paths or
                # arguments. Transport errors exposed here use fixed messages.
                reason = str(error) if isinstance(error, RuntimeError) and not isinstance(error, OSError) else (
                    "Native turn was interrupted." if interrupted else "Native client response timed out."
                    if isinstance(error, TimeoutError) else "Native transport could not complete the request.")
                state = "needs_human" if isinstance(error, NativeNeedsHuman) else "delivery_unknown" if unknown else "not_sent"
                return self._result("", False, reason, status=state, unknown=unknown)
            finally:
                self._turn_id = None
                self._deadline = None
        finally:
            self._turn_lock.release()

    def _codex_turn(self, prompt: str, timeout: float) -> TurnResult:
        deadline = self._deadline or time.monotonic() + timeout
        result = self._request("turn/start", {
            "threadId": self.session_id, "input": [{"type": "text", "text": prompt}],
        }, timeout)
        turn_id = result.get("turn", {}).get("id")
        if not isinstance(turn_id, str):
            raise RuntimeError("Native client did not return a turn ID.")
        self._accepted_turn(turn_id)
        messages: dict[str, str] = {}
        deltas: dict[str, str] = {}
        while True:
            event = self._next(deadline)
            if "id" in event and "method" in event:
                self._approval_request(event, deadline)
                continue
            params = event.get("params") or {}
            if params.get("threadId") not in (None, self.session_id):
                continue
            event_turn = params.get("turnId") or (params.get("turn") or {}).get("id")
            if event_turn and event_turn != self._turn_id:
                continue
            method = event.get("method")
            if method == "item/agentMessage/delta":
                item_id = str(params.get("itemId", "final"))
                deltas[item_id] = deltas.get(item_id, "") + str(params.get("delta", ""))
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    messages[str(item.get("id", "final"))] = item["text"]
            elif method == "turn/completed":
                status = str((params.get("turn") or {}).get("status", "")).lower()
                text = "\n".join((messages or deltas).values())
                success = status == "completed" and not self._needs_human
                return self._result(text, success,
                    None if success else ("Native tool permission requires human review." if self._needs_human else "Codex did not complete the turn successfully."))
            if (len(messages) > MAX_MESSAGES or len(deltas) > MAX_MESSAGES
                    or sum(map(len, messages.values())) > MAX_OUTPUT_CHARS
                    or sum(map(len, deltas.values())) > MAX_OUTPUT_CHARS):
                raise RuntimeError("Native public output exceeded its size limit.")

    def _stream_turn(self, prompt: str, timeout: float) -> TurnResult:
        if self.provider == "claude":
            self._write({"type": "user", "message": {"role": "user", "content": prompt},
                         "parent_tool_use_id": None}, input_attempt=True)
        else:
            self._write({"event": "user", "message": {"content": prompt}}, input_attempt=True)
        deadline = self._deadline or time.monotonic() + timeout
        text_parts: list[str] = []
        while True:
            event = self._next(deadline)
            kind = event.get("type", event.get("event"))
            if self.provider == "claude":
                if event.get("session_id"):
                    self._remember_session(event["session_id"])
                if kind in {"user", "assistant", "control_request", "result"} and self.session_id:
                    self._accepted_turn(event.get("turn_id"))
                if kind == "assistant" and event.get("parent_tool_use_id") is None:
                    for block in (event.get("message") or {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                            text_parts.append(block["text"])
                if kind == "control_request":
                    self._approval_request(event, deadline)
                if kind == "result":
                    denied = bool(event.get("permission_denials")) or self._needs_human
                    success = bool(self.session_id) and not event.get("is_error") and event.get("subtype", "success") == "success" and not denied
                    final_text = event.get("result")
                    text = final_text if isinstance(final_text, str) else "\n".join(text_parts)
                    self._needs_human = denied
                    return self._result(text, success,
                        None if success else ("Native tool permission requires human review." if denied else "Claude did not complete the turn successfully."))
            else:
                data = event.get("step_update") or {}
                if data.get("conversation_id"):
                    self._remember_session(data["conversation_id"])
                    self._accepted_turn(data.get("turn_id"))
                if kind == "result":
                    result = event.get("result") or {}
                    if result.get("conversation_id"):
                        self._remember_session(result["conversation_id"])
                        self._accepted_turn(result.get("turn_id"))
                    denied = bool(result.get("denied_actions"))
                    success = bool(self.session_id) and str(result.get("status", "")).upper() == "SUCCESS" and not denied
                    response = result.get("response")
                    text = response if isinstance(response, str) else ""
                    self._needs_human = denied
                    return self._result(text, success,
                        None if success else ("Native tool permission requires human review." if denied else "Antigravity did not complete the turn successfully."))
            if len(text_parts) > MAX_MESSAGES or sum(map(len, text_parts)) > MAX_OUTPUT_CHARS:
                raise RuntimeError("Native public output exceeded its size limit.")
            if kind == "error":
                raise RuntimeError("Native client reported an error; its input outcome is unconfirmed.")

    def interrupt(self) -> None:
        """Interrupt only this adapter's owned child; preserve the native ID."""
        self._interrupted.set()
        if self.provider == "codex" and self.session_id and self._turn_id and self.pid:
            try:
                self._send_request("turn/interrupt", {"threadId": self.session_id, "turnId": self._turn_id})
            except (OSError, RuntimeError):
                pass
        # Terminating the owned transport also fences late output and tools.
        # The next turn resumes this exact persisted ID in a new process.
        self.close()

    def close(self) -> None:
        """Close stdin first so native tools can clean up their own MCP children."""
        process = self._process
        if not process:
            return
        try:
            if self._reader_state.get("error") and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            if process.stdin:
                process.stdin.close()
            if process.poll() is None:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
        except (OSError, ValueError):
            pass
