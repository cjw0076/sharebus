"""One-use delivery to existing Antigravity app conversations through a sidecar.

The app injects agentapi into its sidecar's PATH. A successful command is only a
transport receipt; selected native output must complete the correlated bus job.
Official interface: https://www.antigravity.google/docs/sidecars/#agentapi
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time
from typing import Any
from urllib.request import Request
import uuid

from mcp_server import BusClient, _read_private
from native_bridge import NativeDeliveryResult, NeedsNativeAttach
from sidecar_registry import RuntimeVerificationError, _register_workspace


CHANNEL = Path(".sharebus/private/antigravity-bridge")
REGISTRY = Path.home() / ".gemini/config/sidecars/sharebus/workspaces"
_MARKER = re.compile(r"\[SHAREBUS_EVENT:([0-9a-f-]{36});JOB:([0-9a-f-]{36})\]")


def _identifier(value: object) -> str:
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("An exact canonical native or bus UUID is required.")
    return value


def _message_header(job_id: str, event_id: str) -> str:
    return f"[SHAREBUS_EVENT:{_identifier(event_id)};JOB:{_identifier(job_id)}]\n<incoming_message>\n"


def _safe(root: Path, target: Path, *, directory: bool = False) -> Path:
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise NeedsNativeAttach("Native bridge paths must not contain links or reparse points.")
    if directory:
        target.mkdir(parents=True, exist_ok=True)
    return target


def _read(root: Path, path: Path) -> dict[str, Any]:
    path = _safe(root, path)
    if path.stat().st_size > 262144:
        raise ValueError("Native bridge record exceeds its limit.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Native bridge record must be an object.")
    return value


def _write(root: Path, path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    _safe(root, path.parent, directory=True)
    _safe(root, path)
    temporary = path if exclusive else path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
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


def _runtime(workspace: Path) -> dict[str, Any]:
    """Verify loopback process identity before reading an agent credential."""
    client = BusClient("antigravity", workspace)
    runtime = client.runtime(workspace)
    with client.opener.open(Request(runtime["url"].rstrip("/") + "/health"), timeout=5) as response:
        health = json.load(response)
    if not isinstance(health, dict) or any(health.get(key) != runtime.get(key) for key in ("workspace_id", "nonce", "pid")):
        raise NeedsNativeAttach("The workspace broker process could not be verified.")
    return runtime


def _state(workspace: Path, agent_id: str) -> dict[str, Any]:
    _runtime(workspace)
    token = _read_private(workspace / ".sharebus/private/credentials" / (_identifier(agent_id) + ".token"), workspace)
    return BusClient("antigravity", workspace).request(workspace, "/api/state", token)


def register_workspace(workspace: Path, runtime: dict[str, Any] | None = None,
                       registry_path: Path | None = None) -> Path:
    """Register only this explicit workspace; never search for other projects."""
    try:
        return _register_workspace(workspace, runtime, registry_path or REGISTRY, _verify_runtime=_runtime)
    except RuntimeVerificationError as exc:
        raise NeedsNativeAttach(str(exc)) from None


def register_native_session(workspace: Path, payload: dict[str, Any], event: str = "SessionStart") -> bool:
    """Record official app hook metadata without opening transcriptPath."""
    try:
        root = Path(workspace).resolve(strict=True)
        if not _safe(root, root / ".sharebus/enabled").is_file() or event not in {"SessionStart", "PreInvocation", "Stop"}:
            return False
        native_id = _identifier(payload.get("conversationId"))
        paths = payload.get("workspacePaths")
        if not isinstance(paths, list) or not any(
            isinstance(path, str) and Path(path).is_absolute() and Path(path).resolve() == root for path in paths
        ):
            return False
        # CLI and IDE histories belong to different applications. Merely having
        # a UUID does not make their conversations addressable by the app API.
        transcript = payload.get("transcriptPath")
        if not isinstance(transcript, str):
            return False
        expected = (Path.home() / ".gemini/antigravity/brain" / native_id).resolve()
        if expected not in Path(transcript).expanduser().resolve().parents:
            return False
        record = {"version": 1, "application": "antigravity_app", "workspace": str(root),
                  "native_session_id": native_id, "hook_event": event, "updated_at": time.time()}
        for source, target in (("modelName", "model_name"), ("invocationNum", "invocation_num"),
                               ("executionNum", "execution_num"), ("fullyIdle", "fully_idle")):
            value = payload.get(source)
            if isinstance(value, (str, int, bool)):
                record[target] = value[:160] if isinstance(value, str) else value
        _write(root, root / CHANNEL / "sessions" / (native_id + ".json"), record)
        return True
    except (OSError, ValueError, TypeError, NeedsNativeAttach):
        return False


def _target(workspace: Path, agent_id: str, native_id: str, *, job_id: str | None = None,
            event_id: str | None = None, nonce: str | None = None) -> dict[str, Any]:
    if not _safe(workspace, workspace / ".sharebus/enabled").is_file():
        raise NeedsNativeAttach("Native collaboration is not enabled in this workspace.")
    runtime = _runtime(workspace)
    if nonce is not None and runtime["nonce"] != nonce:
        raise NeedsNativeAttach("The broker restarted; this dispatch must not be replayed.")
    metadata = _read(workspace, workspace / CHANNEL / "sessions" / (_identifier(native_id) + ".json"))
    if (metadata.get("application") != "antigravity_app" or metadata.get("workspace") != str(workspace)
            or metadata.get("native_session_id") != native_id):
        raise NeedsNativeAttach("The exact native conversation is not attached to this workspace.")
    state = _state(workspace, agent_id)
    own = state.get("own_agent") or {}
    if (state.get("paused") or own.get("state") in {"paused", "stopped", "needs_human"}
            or own.get("id") != agent_id or own.get("provider") != "antigravity"
            or own.get("mode") != "native_attached" or own.get("native_session_id") != native_id):
        raise NeedsNativeAttach("The native attachment is paused or no longer matches its bound identity.")
    if job_id:
        matches = [job for job in state.get("active_jobs", []) if job.get("id") == job_id
                   and job.get("agent_id") == agent_id and job.get("event_id") == event_id
                   and job.get("root_id") == own.get("active_root")
                   and job.get("state") in {"running", "awaiting_native_reply"}]
        if len(matches) != 1:
            raise NeedsNativeAttach("Native dispatch has no exact active owned job and event.")
    return runtime


def build_agentapi_command_binding(helper: str) -> dict[str, Any]:
    """Inspect an app launcher without executing it; the onboarding owner saves it."""
    wrapper = Path(helper).absolute()
    if wrapper.suffix.lower() not in {".bat", ".cmd"}:
        raise NeedsNativeAttach("Only the app-generated Windows launcher needs this binding.")
    _safe(Path(wrapper.anchor), wrapper)
    if wrapper.stat().st_size > 8192:
        raise NeedsNativeAttach("The agentapi launcher exceeds its fixed schema.")
    source = wrapper.read_bytes()
    lines = source.decode("utf-8-sig").splitlines()
    match = re.fullmatch(r'"([^"\r\n]+)" agentapi %\*', lines[1].strip()) if len(lines) == 2 else None
    if not match or lines[0].strip().lower() != "@echo off":
        raise NeedsNativeAttach("The agentapi launcher does not match the app-generated schema.")
    executable = Path(match[1])
    if (not executable.is_absolute() or executable.name.lower() != "language_server.exe"
            or executable.parent.name.lower() != "bin" or executable.parent.parent.name.lower() != "resources"):
        raise NeedsNativeAttach("The agentapi launcher must target its app's language server.")
    application = executable.parent.parent.parent / "Antigravity.exe"
    binding: dict[str, Any] = {"version": 1}
    for key, path in (("wrapper", wrapper), ("executable", executable), ("application", application)):
        _safe(Path(path.anchor), path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NeedsNativeAttach("Agentapi command files must be regular files without hard links.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1048576), b""):
                digest.update(chunk)
        observed_hash = digest.hexdigest()
        _safe(Path(path.anchor), path)
        after = path.stat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink):
            raise NeedsNativeAttach("An agentapi command file changed during inspection.")
        if key == "wrapper" and observed_hash != hashlib.sha256(source).hexdigest():
            raise NeedsNativeAttach("The agentapi launcher changed during inspection.")
        binding[key], binding[key + "_sha256"] = str(path), observed_hash
    return binding


def _agentapi_command(helper: str, binding_path: Path | None) -> tuple[list[str], dict[str, Any]]:
    """Unwrap only the pinned app-generated Windows launcher, never a shell."""
    if Path(helper).suffix.lower() not in {".bat", ".cmd"}:
        # App-owned hosts can also provide a real agentapi executable on any OS.
        return [helper], {"agentapi_command_kind": "inherited_executable"}
    if binding_path is None or not binding_path.is_absolute():
        raise NeedsNativeAttach("The Windows agentapi launcher requires a local command binding.")
    binding = _read(Path(binding_path.anchor), binding_path)
    if binding_path.stat().st_nlink != 1:
        raise NeedsNativeAttach("The agentapi command binding must not be hard linked.")
    observed = build_agentapi_command_binding(helper)
    path_keys = ("wrapper", "executable", "application")
    if (binding.keys() != observed.keys()
            or any(not isinstance(binding[key], str) for key in path_keys)
            or any(os.path.normcase(binding[key]) != os.path.normcase(observed[key]) for key in path_keys)
            or any(binding[key] != value for key, value in observed.items() if key not in path_keys)):
        raise NeedsNativeAttach("The agentapi command binding does not match its app files.")
    metadata = {"agentapi_command_kind": "pinned_app_launcher"}
    metadata.update({"agentapi_" + key: value for key, value in observed.items() if key.endswith("_sha256")})
    return [observed["executable"], "agentapi"], metadata


class AntigravityAttachedAdapter:
    """Queue delivery into the app-owned sidecar; never start or resume a clone."""

    def __init__(self, provider: str, workspace: Path, agent_id: str, token_file: Path,
                 on_event=None, native_address: str | None = None):
        if provider not in {"antigravity", "agy"}:
            raise NeedsNativeAttach("This attachment requires the Antigravity app.")
        self.workspace = Path(workspace).resolve(strict=True)
        self.agent_id = _identifier(agent_id)
        expected = self.workspace / ".sharebus/private/credentials" / (self.agent_id + ".token")
        if Path(token_file).absolute() != expected:
            raise NeedsNativeAttach("The native bridge requires its bound private credential file.")
        self.on_event = on_event
        self.session_id: str | None = None
        self._lock = threading.Lock()
        self._interrupted = threading.Event()
        self._current_job: str | None = None

    @property
    def pid(self) -> None:
        # The app and its sidecar are externally owned; bus stop never kills them.
        return None

    def _channel(self) -> dict[str, Any]:
        value = _read(self.workspace, self.workspace / CHANNEL / "manifest.json")
        if (value.get("workspace") != str(self.workspace) or not value.get("agentapi_available")
                or not value.get("send_message_supported") or value.get("probe_only")
                or time.time() - float(value.get("updated_at", 0)) > 30):
            raise NeedsNativeAttach("The app-managed Antigravity sidecar is not available.")
        return value

    def start(self, resume_id: str | None = None) -> str:
        native_id = _identifier(resume_id)
        if self.session_id and self.session_id != native_id:
            raise NeedsNativeAttach("An attached adapter cannot switch native conversations.")
        channel = self._channel()
        runtime = _target(self.workspace, self.agent_id, native_id)
        if channel.get("runtime_nonce") != runtime["nonce"]:
            raise NeedsNativeAttach("The app sidecar has not connected to this broker generation.")
        self.session_id = native_id
        return native_id

    def turn(self, prompt: str, timeout: float = 240) -> NativeDeliveryResult:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 131072 or not 0 < timeout <= 600:
            raise ValueError("Native delivery requires a bounded prompt and timeout.")
        with self._lock:
            self._interrupted.clear()
            if not self.session_id:
                raise NeedsNativeAttach("Attach the exact native conversation first.")
            trusted_header, separator, message = prompt.partition("<incoming_message>\n")
            markers = _MARKER.findall(trusted_header)
            if not separator or not prompt.endswith("\n</incoming_message>") or len(markers) != 1:
                raise NeedsNativeAttach("One exact bus job/event marker and bounded message envelope are required.")
            event_id, job_id = map(_identifier, markers[0])
            self._current_job = job_id
            channel = self._channel()
            runtime = _target(self.workspace, self.agent_id, self.session_id, job_id=job_id, event_id=event_id)
            if channel.get("runtime_nonce") != runtime["nonce"]:
                raise NeedsNativeAttach("The app sidecar has not connected to this broker generation.")
            delivery_prompt = _message_header(job_id, event_id) + message
            request = {"version": 1, "workspace": str(self.workspace), "workspace_id": runtime["workspace_id"],
                       "runtime_nonce": runtime["nonce"], "agent_id": self.agent_id,
                       "native_session_id": self.session_id, "job_id": job_id, "event_id": event_id,
                       "prompt": delivery_prompt, "prompt_sha256": hashlib.sha256(delivery_prompt.encode("utf-8")).hexdigest(),
                       "created_at": time.time(), "expires_at": time.time() + timeout}
            base = self.workspace / CHANNEL
            request_path = base / "requests" / (job_id + ".json")
            try:
                _write(self.workspace, request_path, request, exclusive=True)
            except FileExistsError:
                prior = _read(self.workspace, request_path)
                if any(prior.get(key) != request[key] for key in ("agent_id", "native_session_id", "event_id", "runtime_nonce", "prompt_sha256")):
                    raise NeedsNativeAttach("A native dispatch cannot be changed or replayed under the same job ID.")
            deadline = time.monotonic() + timeout
            try:
                while time.monotonic() < deadline and not self._interrupted.is_set():
                    path = base / "receipts" / (job_id + ".json")
                    if path.exists():
                        receipt = _read(self.workspace, path)
                        if any(receipt.get(key) != request[key] for key in ("agent_id", "native_session_id", "event_id", "job_id", "runtime_nonce", "prompt_sha256")):
                            raise NeedsNativeAttach("Native receipt correlation does not match this dispatch.")
                        accepted = receipt.get("status") == "command_accepted"
                        text = "agentapi accepted the command; awaiting the existing app session's correlated result." if accepted else "Native delivery is unconfirmed; review before sending a new request."
                        return NativeDeliveryResult(text, self.session_id, accepted, not accepted,
                            None if accepted else "Native delivery failed or its outcome is unknown; automatic replay is disabled.",
                            "awaiting_native_reply" if accepted else "needs_human", True,
                            str(receipt.get("sidecar_instance_id") or channel.get("sidecar_instance_id")))
                    self._interrupted.wait(0.10)
                self.interrupt()
                return NativeDeliveryResult("Native delivery was interrupted or timed out.", self.session_id, False, True,
                    "Delivery outcome may be unknown; automatic replay is disabled.", "needs_human")
            finally:
                self._current_job = None

    def interrupt(self) -> None:
        self._interrupted.set()
        if self._current_job:
            _write(self.workspace, self.workspace / CHANNEL / "cancel" / (self._current_job + ".json"),
                   {"job_id": self._current_job, "cancelled_at": time.time()})

    def close(self) -> None:
        self.interrupt()


class SidecarWorker:
    """App-owned, filesystem-idle worker; no model calls occur without a job."""

    def __init__(self, workspace: Path | None = None, registry_path: Path | None = None):
        self.workspace = Path(workspace).resolve(strict=True) if workspace else None
        self.registry = Path(registry_path or REGISTRY).absolute()
        self.instance_id = str(uuid.uuid4())
        self.helper = shutil.which("agentapi.exe") or shutil.which("agentapi")
        binding = os.environ.get("SHAREBUS_ANTIGRAVITY_COMMAND_BINDING")
        data_dir = os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR")
        self.command_binding = Path(binding) if binding else Path(data_dir) / "agentapi-binding.json" if data_dir else None
        self._command_prefix: list[str] | None = None
        self._command_receipt: dict[str, Any] = {}
        self.supported = False
        self.probe_only = os.environ.get("SHAREBUS_SIDECAR_PROBE_ONLY") == "1"
        self._stop = threading.Event()
        self._help_receipt: dict[str, Any] = {}

    def probe(self) -> dict[str, Any]:
        if not os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR") or not self.helper:
            self.supported = False
            if not os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR"):
                self.helper = None
            return {"agentapi_available": False, "send_message_supported": False}
        try:
            self._command_prefix, self._command_receipt = _agentapi_command(self.helper, self.command_binding)
        except (OSError, ValueError, NeedsNativeAttach):
            self.helper, self.supported = None, False
            self._command_prefix, self._command_receipt = None, {}
            self._help_receipt = {"agentapi_command_error": "command_binding_invalid"}
            return {"agentapi_available": False, "send_message_supported": False, **self._help_receipt}
        try:
            result = subprocess.run([*self._command_prefix, "--help"], capture_output=True, text=True, shell=False,
                encoding="utf-8", errors="replace", timeout=15, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.supported = result.returncode == 0 and bool(re.search(r"\bsend-message\b", result.stdout + result.stderr))
            self._help_receipt = {"help_returncode": result.returncode,
                "help_stdout_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()}
        except (OSError, subprocess.TimeoutExpired):
            self.supported = False
        return {"agentapi_available": bool(self.helper), "send_message_supported": self.supported,
                **self._command_receipt, **self._help_receipt}

    def _workspaces(self) -> list[Path]:
        if self.workspace:
            return [self.workspace]
        if not self.registry.is_dir():
            return []
        roots = []
        anchor = Path(self.registry.anchor)
        _safe(anchor, self.registry)
        for path in self.registry.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                continue
            try:
                entry = _read(anchor, path)
                root = Path(entry["workspace"]).resolve(strict=True)
                runtime = _runtime(root)
                if runtime["workspace_id"] == entry.get("workspace_id") and runtime["nonce"] == entry.get("runtime_nonce"):
                    roots.append(root)
            except Exception:
                # Unavailable explicit roots remain registered for later reconnect.
                continue
        return roots

    def heartbeat(self) -> None:
        for root in self._workspaces():
            try:
                runtime = _runtime(root)
                _write(root, root / CHANNEL / "manifest.json", {"version": 1, "workspace": str(root),
                    "workspace_id": runtime["workspace_id"], "runtime_nonce": runtime["nonce"],
                    "sidecar_instance_id": self.instance_id, "pid": os.getpid(), "updated_at": time.time(),
                    "agentapi_available": bool(self.helper), "send_message_supported": self.supported,
                    "probe_only": self.probe_only, **self._command_receipt, **self._help_receipt})
            except Exception:
                continue

    def _receipt(self, root: Path, request: dict[str, Any], status: str, **extra: Any) -> None:
        result = {key: request[key] for key in ("agent_id", "native_session_id", "event_id", "job_id", "runtime_nonce", "prompt_sha256")}
        result.update({"status": status, "sidecar_instance_id": self.instance_id, "recorded_at": time.time(), "delivery_only": True, **extra})
        _write(root, root / CHANNEL / "receipts" / (_identifier(request["job_id"]) + ".json"), result)

    def dispatch_once(self, root: Path, request: dict[str, Any]) -> bool:
        """Persist a one-use claim before invoking agentapi, never replay a claim."""
        job_id = _identifier(request.get("job_id"))
        base = root / CHANNEL
        receipt_path = base / "receipts" / (job_id + ".json")
        if receipt_path.exists():
            return False
        if not self.supported or self.probe_only or not self.helper:
            return False
        if (request.get("workspace") != str(root) or request.get("expires_at", 0) <= time.time()
                or (base / "cancel" / (job_id + ".json")).exists()):
            self._receipt(root, request, "needs_human")
            return False
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != request.get("prompt_sha256"):
            self._receipt(root, request, "needs_human")
            return False
        try:
            runtime = _target(root, _identifier(request["agent_id"]), _identifier(request["native_session_id"]),
                    job_id=job_id, event_id=_identifier(request["event_id"]), nonce=request["runtime_nonce"])
            if request.get("workspace_id") != runtime["workspace_id"]:
                raise NeedsNativeAttach("Dispatch workspace identity does not match its broker.")
            if not prompt.startswith(_message_header(job_id, request["event_id"])) or not prompt.endswith("\n</incoming_message>"):
                raise NeedsNativeAttach("Native message correlation does not match the verified dispatch binding.")
            _write(root, base / "claims" / (job_id + ".json"),
                {"job_id": job_id, "sidecar_instance_id": self.instance_id, "claimed_at": time.time()}, exclusive=True)
        except FileExistsError:
            self._receipt(root, request, "needs_human", reason="previous_dispatch_outcome_unknown")
            return False
        except Exception:
            self._receipt(root, request, "needs_human")
            return False
        try:
            if (base / "cancel" / (job_id + ".json")).exists():
                self._receipt(root, request, "needs_human", reason="cancelled_before_send")
                return False
            # Human control may have changed while the claim was flushed.
            _target(root, request["agent_id"], request["native_session_id"],
                    job_id=job_id, event_id=request["event_id"], nonce=request["runtime_nonce"])
            command, command_receipt = _agentapi_command(self.helper, self.command_binding)
            if self._command_prefix is not None and (command != self._command_prefix or command_receipt != self._command_receipt):
                raise NeedsNativeAttach("The probed agentapi command changed; automatic dispatch is disabled.")
            # Hashing app binaries can outlast a deadline or a human control
            # change. Recheck authority after hashing, immediately before spawn.
            try:
                runtime = _target(root, request["agent_id"], request["native_session_id"],
                        job_id=job_id, event_id=request["event_id"], nonce=request["runtime_nonce"])
                if runtime["workspace_id"] != request["workspace_id"]:
                    raise NeedsNativeAttach("The dispatch workspace changed before native input.")
            except Exception:
                self._receipt(root, request, "not_sent", reason="authority_changed_before_send", native_input_attempted=False)
                return False
            if self._stop.is_set() or (base / "cancel" / (job_id + ".json")).exists():
                self._receipt(root, request, "not_sent", reason="cancelled_before_send", native_input_attempted=False)
                return False
            remaining = request["expires_at"] - time.time()
            if remaining <= 0:
                self._receipt(root, request, "not_sent", reason="expired_before_send", native_input_attempted=False)
                return False
            # The original prompt is one argv item, never a shell command.
            result = subprocess.run([*command, "send-message", request["native_session_id"], prompt],
                capture_output=True, text=True, shell=False, encoding="utf-8", errors="replace",
                timeout=min(60, remaining), check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            status = "command_accepted" if result.returncode == 0 else "needs_human"
            try:
                reported = json.loads(result.stdout)
                if isinstance(reported, dict) and (reported.get("success") is False or reported.get("error") or reported.get("denied_actions")
                        or str(reported.get("status", "")).lower() in {"error", "failed", "denied"}):
                    status = "needs_human"
                if isinstance(reported, dict):
                    reported_id = reported.get("conversation_id", reported.get("conversationId"))
                    if reported_id is not None and reported_id != request["native_session_id"]:
                        status = "needs_human"
            except ValueError:
                pass
            _write(root, base / "private-output" / (job_id + ".json"),
                   {"stdout": result.stdout[:131072], "stderr": result.stderr[:131072]})
            self._receipt(root, request, status, returncode=result.returncode,
                          stdout_sha256=hashlib.sha256(result.stdout.encode("utf-8")).hexdigest())
            return status == "command_accepted"
        except Exception:
            self._receipt(root, request, "needs_human", reason="dispatch_outcome_unknown")
            return False

    def run_once(self) -> None:
        for root in self._workspaces():
            requests = root / CHANNEL / "requests"
            if not requests.is_dir():
                continue
            _safe(root, requests)
            for path in sorted(requests.glob("*.json")):
                try:
                    request = _read(root, path)
                    if path.name == _identifier(request.get("job_id")) + ".json":
                        self.dispatch_once(root, request)
                except Exception:
                    # A partially written or invalid record is never dispatched.
                    continue

    def run(self) -> None:
        self.probe()
        def pulse() -> None:
            while not self._stop.is_set():
                self.heartbeat()
                self._stop.wait(2)
        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            while not self._stop.is_set():
                self.run_once()
                self._stop.wait(0.25)
        finally:
            self._stop.set()
            thread.join(timeout=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Antigravity app-owned Sharebus sidecar")
    parser.add_argument("command", choices=("sidecar",))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--workspace", type=Path)
    selection.add_argument("--registry", type=Path)
    args = parser.parse_args()
    SidecarWorker(args.workspace, args.registry).run()


if __name__ == "__main__":
    main()
