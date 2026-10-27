"""Model-free delivery to an existing Codex Desktop thread via its public CLI.

The CLI's queue receipt confirms storage, not native execution or work completion.
The pinned public implementation uses thread/queue/add, never thread/resume:
https://github.com/openai/codex/blob/4ebc485ca321243727539187169425fcf972f2c4/codex-rs/tui/src/session_queue_commands.rs
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from discovery import resolve_executable
from mcp_server import BusClient, _read_private
from native_bridge import NativeDeliveryResult, NeedsNativeAttach

CHANNEL = Path(".sharebus/private/codex-bridge")
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_MARKER = re.compile(r"^\[SHAREBUS_EVENT:(" + _UUID + r");JOB:(" + _UUID + r")\]$", re.MULTILINE)
_RECEIPT = re.compile(r"Queued message (" + _UUID + r") for thread (" + _UUID + r")\.")
_PROBES: dict[tuple, dict] = {}
_PROBE_LOCK = threading.Lock()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(_UUID, value):
        raise NeedsNativeAttach("An exact canonical native or bus UUID is required.")
    return str(uuid.UUID(value))


def _state(workspace: Path, agent_id: str, token_file: Path) -> tuple[dict, dict]:
    _read_private(workspace / ".sharebus/enabled", workspace)
    client = BusClient("codex", workspace)
    # runtime() verifies the listener's workspace, PID and nonce before a token
    # is read. request() verifies them again before sending that credential.
    runtime = client.runtime(workspace)
    token = _read_private(token_file, workspace)
    return runtime, client.request(workspace, "/api/state", token)


def probe_native_queue(workspace: Path | None = None, *, executable: str | Path | None = None) -> dict:
    """Bounded read-only version/help probe; never enqueue or start a model."""
    result: dict[str, Any] = {"supported": False, "executable": None, "version": None, "reason": None}
    try:
        candidate = executable or resolve_executable("codex")
        if not isinstance(candidate, (str, Path)):
            raise NeedsNativeAttach("A native Codex executable with public queue support is required.")
        path = Path(candidate)
        if (not path.is_absolute() or ".." in path.parts or path.name.lower() not in {"codex", "codex.exe"}
                or path.is_symlink() or not path.is_file()
                or getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise NeedsNativeAttach("A native Codex executable is required; wrappers and extra arguments are not accepted.")
        info = path.stat()
        fingerprint = (str(path.resolve()), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        result["executable"] = str(path)
        with _PROBE_LOCK:
            if fingerprint in _PROBES:
                return dict(_PROBES[fingerprint])
            options: dict[str, Any] = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
                "timeout": 15, "shell": False, "stdin": subprocess.DEVNULL,
                "cwd": str(Path(workspace or Path.cwd()).resolve(strict=True)),
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
            version = subprocess.run([str(path), "--version"], check=False, **options)
            match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9.-]+)?", version.stdout.strip())
            if version.returncode or not match:
                raise NeedsNativeAttach("The installed native Codex CLI version could not be verified.")
            result["version"] = version.stdout.strip().removeprefix("codex-cli ")
            if tuple(map(int, match.groups())) < (0, 163, 0):
                result["reason"] = "Codex CLI 0.163.0 or newer with public queue support is required."
                _PROBES[fingerprint] = dict(result)
                return result
            help_result = subprocess.run([str(path), "queue", "--help"], check=False, **options)
            if help_result.returncode:
                raise NeedsNativeAttach("The read-only Codex queue help check did not complete.")
            if (not re.search(r"^Usage: codex queue\b", help_result.stdout, re.MULTILINE)
                    or "--thread <THREAD>" not in help_result.stdout or "--message <TEXT>" not in help_result.stdout):
                result["reason"] = "The installed Codex CLI does not expose the verified public queue interface."
                _PROBES[fingerprint] = dict(result)
                return result
            result["supported"] = True
            _PROBES[fingerprint] = dict(result)
    except NeedsNativeAttach as exc:
        result["reason"] = str(exc)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        result["reason"] = "The read-only Codex queue compatibility check did not complete."
    return result


def _write_receipt(workspace: Path, path: Path, value: dict, *, exclusive: bool = False) -> None:
    current = workspace
    for part in path.parent.relative_to(workspace).parts:
        current /= part
        current.mkdir(mode=0o700, exist_ok=True)
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise NeedsNativeAttach("Codex receipt paths must not contain links or reparse points.")
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
        raise NeedsNativeAttach("Codex receipts must not be links or reparse points.")
    temporary = path if exclusive else path.with_name(uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(temporary, path)
    finally:
        if not exclusive and temporary.exists():
            temporary.unlink()


class CodexAttachedAdapter:
    """Attach to an exact registered thread; own no native model process."""

    def __init__(self, provider: str, workspace: Path, agent_id: str, token_file: Path,
                 on_event=None, native_address: str | None = None, *, executable: str | Path | None = None):
        if provider != "codex" or native_address is not None:
            raise NeedsNativeAttach("Codex delivery requires an exact thread UUID, not a native name or address.")
        self.workspace = Path(workspace).resolve(strict=True)
        self.agent_id = _identifier(agent_id)
        self.token_file = self.workspace / ".sharebus/private/credentials" / (self.agent_id + ".token")
        candidate = Path(token_file)
        if not candidate.is_absolute() or ".." in candidate.parts or candidate != self.token_file:
            raise NeedsNativeAttach("The Codex bridge requires its bound private credential file.")
        self.on_event = on_event
        self.session_id: str | None = None
        self._explicit_executable = executable
        self._runtime_identity: tuple | None = None
        self._version: str | None = None
        self._lock = threading.Lock()
        self._interrupted = threading.Event()
        self._closed = threading.Event()

    @property
    def pid(self) -> None:
        # Neither the existing Desktop process nor a native model is ours.
        return None

    def _probe(self) -> str:
        result = probe_native_queue(self.workspace, executable=self._explicit_executable)
        if not result["supported"]:
            raise NeedsNativeAttach(result["reason"])
        self._version = result["version"]
        return result["executable"]

    def _target(self, native_id: str, *, event_id: str | None = None, job_id: str | None = None,
                root_id: str | None = None) -> dict:
        runtime, state = _state(self.workspace, self.agent_id, self.token_file)
        identity = tuple(runtime.get(key) for key in ("workspace_id", "pid", "nonce"))
        if self._runtime_identity is not None and identity != self._runtime_identity:
            raise NeedsNativeAttach("The workspace broker changed; this dispatch must not be replayed.")
        workspace, own, auto = state.get("workspace") or {}, state.get("own_agent") or {}, state.get("auto") or {}
        if (workspace.get("path") != str(self.workspace) or workspace.get("id") != runtime.get("workspace_id")
                or state.get("paused") or auto.get("active") is not True or auto.get("codex_delivery") != "native_queue"
                or own.get("id") != self.agent_id or own.get("provider") != "codex"
                or own.get("mode") != "native_attached" or own.get("native_session_id") != native_id
                or own.get("state") not in {"idle", "running", "awaiting_native_reply"}):
            raise NeedsNativeAttach("The exact native Codex identity is unavailable, paused, or belongs to another workspace.")
        if job_id:
            jobs = [job for job in state.get("active_jobs", []) if isinstance(job, dict)
                    and job.get("id") == job_id and job.get("agent_id") == self.agent_id
                    and job.get("event_id") == event_id and job.get("root_id") == root_id
                    and root_id == own.get("active_root") and job.get("state") in {"running", "awaiting_native_reply"}
                    and not isinstance(job.get("root_deadline"), bool)
                    and isinstance(job.get("root_deadline"), (int, float))
                    and math.isfinite(job["root_deadline"]) and job["root_deadline"] > time.time()]
            if len(jobs) != 1:
                raise NeedsNativeAttach("Codex delivery requires one exact active owned job, event, and unexpired human root.")
        self._runtime_identity = identity
        return runtime

    def start(self, resume_id: str | None = None) -> str:
        native_id = _identifier(resume_id)
        if self._closed.is_set() or (self.session_id and self.session_id != native_id):
            raise NeedsNativeAttach("A closed Codex attachment cannot restart or switch native conversations.")
        self._target(native_id)
        self._probe()
        self.session_id = native_id
        return native_id

    def turn(self, prompt: str, timeout: float = 240) -> NativeDeliveryResult:
        if (not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt or len(prompt.encode("utf-8")) > 131072
                or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 600):
            raise ValueError("Codex queue delivery requires a bounded prompt and timeout.")
        with self._lock:
            if self._closed.is_set() or not self.session_id:
                raise NeedsNativeAttach("Attach the exact existing Codex conversation before delivery.")
            self._interrupted.clear()
            header, separator, message = prompt.partition("<incoming_message>\n")
            markers = _MARKER.findall(header)
            roots = re.findall(r"^Human root: (" + _UUID + r")\. Remaining automatic turns: \d+\.$", header, re.MULTILINE)
            identities = re.findall(r"^Your agent ID: (" + _UUID + r")\. Provider: codex\.$", header, re.MULTILINE)
            if (not separator or not prompt.endswith("\n</incoming_message>") or len(markers) != 1
                    or len(roots) != 1 or identities != [self.agent_id]):
                raise NeedsNativeAttach("A canonical supervisor envelope with one exact job, event, identity, and human root is required.")
            event_id, job_id = map(_identifier, markers[0])
            root_id = _identifier(roots[0])
            runtime = self._target(self.session_id, event_id=event_id, job_id=job_id, root_id=root_id)
            executable = self._probe()
            # Routing stays in the authenticated dispatch; the installed skill
            # supplies shared policy without repeating it in every native input.
            body = f"[SHAREBUS_EVENT:{event_id};JOB:{job_id}]\n<incoming_message>\n" + message
            command = [executable, "queue", "--thread", self.session_id, "--message", body]
            if len(body.encode("utf-8")) > 131072 or (os.name == "nt" and len(subprocess.list2cmdline(command).encode("utf-16-le")) // 2 > 32766):
                raise NeedsNativeAttach("The selected request exceeds the native queue argument limit.")
            record: dict[str, Any] = {"version": 1, "agent_id": self.agent_id, "native_session_id": self.session_id,
                "workspace_id": runtime["workspace_id"], "runtime_nonce": runtime["nonce"], "event_id": event_id,
                "job_id": job_id, "root_id": root_id, "prompt_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "cli_version": self._version, "status": "dispatch_outcome_unknown", "created_at": time.time()}
            path = self.workspace / CHANNEL / (job_id + ".json")
            try:
                # Claim before invoking the non-idempotent CLI. An interrupted or
                # crashed process leaves uncertainty, never permission to retry.
                _write_receipt(self.workspace, path, record, exclusive=True)
            except FileExistsError:
                prior = _read_private(path, self.workspace, json_file=True)
                if not isinstance(prior, dict) or any(prior.get(key) != record[key] for key in
                        ("agent_id", "native_session_id", "workspace_id", "runtime_nonce", "event_id", "job_id", "root_id", "prompt_sha256")):
                    raise NeedsNativeAttach("A queued Codex job cannot be changed or replayed.")
                return self._result(prior)
            try:
                self._target(self.session_id, event_id=event_id, job_id=job_id, root_id=root_id)
                if self._interrupted.is_set() or self._closed.is_set():
                    raise InterruptedError
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, check=False, shell=False, stdin=subprocess.DEVNULL, cwd=str(self.workspace),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                receipt = _RECEIPT.fullmatch(result.stdout.strip())
                record.update(returncode=result.returncode,
                    stdout_sha256=hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
                    stderr_sha256=hashlib.sha256(result.stderr.encode("utf-8")).hexdigest())
                if result.returncode == 0 and receipt and receipt[2] == self.session_id:
                    record["queued_submission_id"] = _identifier(receipt[1])
                    if not self._interrupted.is_set() and not self._closed.is_set():
                        record["status"] = "queue_accepted"
            except (OSError, subprocess.TimeoutExpired, InterruptedError, NeedsNativeAttach):
                # stdout/stderr and exception argv can contain the private body;
                # retain only sanitized correlation and outcome metadata.
                pass
            record["updated_at"] = time.time()
            _write_receipt(self.workspace, path, record)
            return self._result(record)

    def _result(self, record: dict) -> NativeDeliveryResult:
        accepted = record.get("status") == "queue_accepted" and isinstance(record.get("queued_submission_id"), str)
        if accepted:
            _identifier(record["queued_submission_id"])
        text = ("Codex queued message " + record["queued_submission_id"] + "; awaiting the existing conversation's correlated result."
                if accepted else "Codex queue delivery is unconfirmed or interrupted; review before creating another request.")
        return NativeDeliveryResult(text, self.session_id, accepted, not accepted,
            None if accepted else "A native message may already be queued; automatic replay is disabled. Native work was not cancelled.",
            "awaiting_native_reply" if accepted else "needs_human")

    def interrupt(self) -> None:
        # This only stops a not-yet-issued transport attempt. Already queued
        # messages and Desktop work remain under native and human control.
        self._interrupted.set()

    def close(self) -> None:
        self._closed.set()
        self.interrupt()
