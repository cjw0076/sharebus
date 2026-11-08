"""Independent, version-pinned client for the installed Codex local tool pipe.

This experimental internal protocol is not a public OpenAI API. Construction
does no I/O. Discovery only opens known-prefix pipes, verifies the server on the
same handle, then closes them. Metadata probing calls tools/list and the current
thread's list_artifacts once; it never reads messages or starts a model.

Observed contracts are recorded in SUPPORTED_PROVENANCES and the inspection
manifest. Only the current exact installation is enabled for native I/O;
the prior inspected versions remain metadata for sealed routing migration.
No proprietary implementation is copied here. Frames are independently encoded.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import time
from typing import Any, Protocol
import uuid

MAX_FRAME = 8 * 1024 * 1024
MAX_CANDIDATES = 16
PIPE_PREFIX = "\\\\.\\pipe\\codex-app-tools"
PIPE_PATTERN = re.compile(re.escape(PIPE_PREFIX) + r"-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
INSTALLED_EXECUTABLE = Path(
    r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1016.8622.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
)
EXECUTABLE_SHA256 = "f7f605cceb2e75dca52e03362329a889f917cc1f8d1036cd26386727a2319415"
SERVER_SHA256 = "3ea311dd9070524ca9047c50832a1bbd8d4fc38efcc66624899739348b6fb652"
MCP_CONFIG_SHA256 = "05e680f62cb92a861f29dd0dadcefd6239d62facf22e8bf0f2b944de776ceba4"
APP_MANIFEST_SHA256 = "d2f593a2b94c0258ec30feff2623ca896f3209378ef8c6b838364a5ac97e85ef"
MAIN_MODULE_PATH = ".vite/build/main-CT5a1yCd.js"
MAIN_MODULE_SHA256 = "cdac659a830908ec5202a887e5a37b302074bfc9e9ab0f52fed3e8abbdb9e713"
PROVENANCE = {
    "application_version": "26.1016.8622.0",
    "bundled_server_sha256": SERVER_SHA256,
    "package_manifest_sha256": APP_MANIFEST_SHA256,
    "main_module_sha256": MAIN_MODULE_SHA256,
    "public_api": False,
    "metadata_is_native_turn_attestation": False,
}
# Old pins establish previously inspected routing metadata, not permission to
# use an arbitrary installed version or replay an attempted native dispatch.
SUPPORTED_PROVENANCES = (
    {
        "application_version": "26.1013.4894.0",
        "bundled_server_sha256": "fb70354aa2de84fea546c4d8caf210f62a48fd970ece87c8351216e76330a760",
        "package_manifest_sha256": "f4d8bba5ac3ec8801077b9ab8cc8f403284ab785ce22cd5fc1a30a7ea1837937",
        "main_module_sha256": "c8a2af504b21314fa6a64ad1b4ec83dbad80033ede902caf9978fd9c74a2c175",
        "public_api": False,
        "metadata_is_native_turn_attestation": False,
    },
    {
        "application_version": "26.1016.6573.0",
        "bundled_server_sha256": "3ea311dd9070524ca9047c50832a1bbd8d4fc38efcc66624899739348b6fb652",
        "package_manifest_sha256": "842bdd3746adf3588802b78d4acf588b1af7b19b019c7c90f20d357ffbf20514",
        "main_module_sha256": "cdac659a830908ec5202a887e5a37b302074bfc9e9ab0f52fed3e8abbdb9e713",
        "public_api": False,
        "metadata_is_native_turn_attestation": False,
    },
    PROVENANCE,
)


class CodexPipeError(ValueError):
    """A redacted transport/contract error; never embeds a pipe or response."""

    def __init__(self, message: str, *, reason_code: str = "protocol_unavailable"):
        super().__init__(message)
        self.reason_code = reason_code


class PipeTimeout(CodexPipeError):
    def __init__(self, message: str):
        super().__init__(message, reason_code="native_timeout")


def _hash(value: bytes | str) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _installed_main_hash(archive: Path) -> str:
    """Hash one exact packed module without executing or extracting the ASAR."""
    with archive.open("rb") as stream:
        header = stream.read(16)
        if len(header) != 16:
            raise CodexPipeError("Invalid pinned archive header", reason_code="pin_mismatch")
        _, header_size, _, json_size = struct.unpack("<IIII", header)
        if not 0 < json_size <= header_size <= 16 * 1024 * 1024:
            raise CodexPipeError("Invalid pinned archive header", reason_code="pin_mismatch")
        try:
            node = _parse(stream.read(json_size))
            for part in MAIN_MODULE_PATH.split("/"):
                node = node["files"][part]
            size, offset = node["size"], int(node["offset"])
            if (type(size) is not int or not 0 < size <= 8 * 1024 * 1024 or offset < 0
                    or node.get("unpacked") or node.get("link")):
                raise ValueError
            stream.seek(8 + header_size + offset)
            content = stream.read(size)
            if len(content) != size:
                raise ValueError
            return _hash(content)
        except (KeyError, TypeError, ValueError, OverflowError):
            raise CodexPipeError("Invalid pinned archive module", reason_code="pin_mismatch") from None


def encode_frame(value: Mapping[str, Any]) -> bytes:
    payload = _json(dict(value))
    if not 0 < len(payload) <= MAX_FRAME:
        raise CodexPipeError("Frame exceeds the protocol limit")
    return struct.pack("<I", len(payload)) + payload


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CodexPipeError("Duplicate JSON property")
        result[key] = value
    return result


def _parse(payload: bytes) -> dict[str, Any]:
    try:
        result = json.loads(payload.decode("utf-8"), object_pairs_hook=_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise CodexPipeError("Malformed JSON frame") from None


@dataclass(frozen=True)
class ServerIdentity:
    pid: int
    executable: str
    sid: str


class PipeIO(Protocol):
    identity: ServerIdentity

    def read_exact(self, count: int, deadline: float) -> bytes: ...
    def write_all(self, data: bytes, deadline: float) -> None: ...
    def close(self) -> None: ...


class PipeSystem(Protocol):
    def candidates(self, limit: int) -> list[str]: ...
    def current_sid(self) -> str: ...
    def open(self, name: str) -> PipeIO: ...


# A cancelled Windows operation's buffer must live until completion. Only our
# operation/handle is cancelled. An uncooperative driver cannot block shutdown.
_PENDING_IO: list[tuple[Any, ...]] = []


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class _FindData(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("creation", wintypes.FILETIME),
                ("access", wintypes.FILETIME), ("write", wintypes.FILETIME),
                ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                ("reserved0", wintypes.DWORD), ("reserved1", wintypes.DWORD),
                ("name", wintypes.WCHAR * 260), ("alternate", wintypes.WCHAR * 14)]


class Win32PipeSystem:
    """Own-handle Win32 transport. Loading these DLLs happens on explicit use."""

    def __init__(self):
        if os.name != "nt":
            raise CodexPipeError("This native adapter requires Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        k, a = self.kernel, self.security
        declarations = {
            "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetNamedPipeServerProcessId": ([wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "CreateEventW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetOverlappedResult": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL], wintypes.BOOL),
            "CancelIoEx": ([wintypes.HANDLE, ctypes.POINTER(_Overlapped)], wintypes.BOOL),
            "FindFirstFileW": ([wintypes.LPCWSTR, ctypes.POINTER(_FindData)], wintypes.HANDLE),
            "FindNextFileW": ([wintypes.HANDLE, ctypes.POINTER(_FindData)], wintypes.BOOL),
            "FindClose": ([wintypes.HANDLE], wintypes.BOOL),
            "LocalFree": ([ctypes.c_void_p], ctypes.c_void_p),
        }
        for name, (arguments, result) in declarations.items():
            function = getattr(k, name)
            function.argtypes, function.restype = arguments, result
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        a.GetTokenInformation.restype = wintypes.BOOL
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        a.ConvertSidToStringSidW.restype = wintypes.BOOL

    def candidates(self, limit: int) -> list[str]:
        explicit = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
        if explicit:
            if not PIPE_PATTERN.fullmatch(explicit):
                raise CodexPipeError("Unexpected configured pipe scope")
            return [explicit]
        data = _FindData()
        search = self.kernel.FindFirstFileW(PIPE_PREFIX + "-*", ctypes.byref(data))
        if search == ctypes.c_void_p(-1).value:
            if ctypes.get_last_error() in (2, 3):
                return []
            raise CodexPipeError("Known-prefix discovery is unavailable")
        found: list[str] = []
        try:
            while True:
                candidate = "\\\\.\\pipe\\" + data.name
                if PIPE_PATTERN.fullmatch(candidate):
                    found.append(candidate)
                    if len(found) > limit:
                        raise CodexPipeError("Too many scoped pipe candidates")
                if not self.kernel.FindNextFileW(search, ctypes.byref(data)):
                    if ctypes.get_last_error() != 18:
                        raise CodexPipeError("Incomplete known-prefix discovery")
                    return found
        finally:
            self.kernel.FindClose(search)

    def _sid(self, process: Any) -> str:
        token = wintypes.HANDLE()
        if not self.security.OpenProcessToken(process, 8, ctypes.byref(token)):
            raise CodexPipeError("Process owner is unavailable")
        try:
            size = wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not 0 < size.value <= 65536:
                raise CodexPipeError("Process owner is unavailable")
            buffer = ctypes.create_string_buffer(size.value)
            if not self.security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise CodexPipeError("Process owner is unavailable")
            sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not self.security.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
                raise CodexPipeError("Process owner is unavailable")
            try:
                value = text.value
                if not value:
                    raise CodexPipeError("Process owner is unavailable")
                return value
            finally:
                self.kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            self.kernel.CloseHandle(token)

    def current_sid(self) -> str:
        return self._sid(self.kernel.GetCurrentProcess())

    def open(self, name: str) -> PipeIO:
        if not PIPE_PATTERN.fullmatch(name):
            raise CodexPipeError("Unexpected pipe scope")
        # OVERLAPPED + SECURITY_SQOS_PRESENT + SECURITY_IDENTIFICATION prevents
        # the pipe server from impersonating this client at a higher level.
        handle = self.kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40110000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise CodexPipeError("Scoped native pipe is unavailable")
        try:
            pid = wintypes.ULONG()
            if not self.kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)) or pid.value <= 0:
                raise CodexPipeError("Native pipe server identity is unavailable")
            process = self.kernel.OpenProcess(0x1000, False, pid)
            if not process:
                raise CodexPipeError("Native pipe server identity is unavailable")
            try:
                size = wintypes.DWORD(32768)
                executable = ctypes.create_unicode_buffer(size.value)
                if not self.kernel.QueryFullProcessImageNameW(process, 0, executable, ctypes.byref(size)):
                    raise CodexPipeError("Native pipe server identity is unavailable")
                sid = self._sid(process)
                exit_code = wintypes.DWORD()
                second_pid = wintypes.ULONG()
                if (not self.kernel.GetExitCodeProcess(process, ctypes.byref(exit_code)) or exit_code.value != 259
                        or not self.kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(second_pid))
                        or second_pid.value != pid.value):
                    raise CodexPipeError("Native pipe server changed during verification")
                identity = ServerIdentity(pid.value, executable.value, sid)
            finally:
                self.kernel.CloseHandle(process)
            return _Win32Pipe(self.kernel, handle, identity)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise


class _Win32Pipe:
    def __init__(self, kernel: Any, handle: Any, identity: ServerIdentity):
        self.kernel, self.handle, self.identity = kernel, handle, identity
        self.diagnostics: dict[str, Any] = {"bytes_read_completed": 0, "bytes_written_completed": 0,
                            "io_calls": 0, "last_io_operation": None,
                            "last_win32_code": None, "last_wait_result": None,
                            "last_cancel_succeeded": None, "last_cancel_win32_code": None}

    def _io(self, buffer: Any, count: int, deadline: float, *, writing: bool) -> int:
        self.diagnostics.update(io_calls=self.diagnostics["io_calls"] + 1,
                                last_io_operation="write" if writing else "read",
                                last_win32_code=None, last_wait_result=None,
                                last_cancel_succeeded=None, last_cancel_win32_code=None)
        if self.handle is None or deadline <= time.monotonic():
            raise PipeTimeout("Native pipe operation timed out")
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise CodexPipeError("Native pipe event is unavailable")
        operation = _Overlapped()
        operation.hEvent = event
        transferred = wintypes.DWORD()
        retained = False
        try:
            function = self.kernel.WriteFile if writing else self.kernel.ReadFile
            completed = function(self.handle, buffer, count, None, ctypes.byref(operation))
            if not completed:
                self.diagnostics["last_win32_code"] = ctypes.get_last_error()
                if self.diagnostics["last_win32_code"] != 997:
                    raise CodexPipeError("Native pipe I/O failed")
            remaining = max(0, min(60000, math.ceil((deadline - time.monotonic()) * 1000)))
            # A TRUE return already completed synchronously. Only an
            # ERROR_IO_PENDING operation requires a completion-event wait.
            wait = 0 if completed else self.kernel.WaitForSingleObject(event, remaining)
            self.diagnostics["last_wait_result"] = wait
            if wait != 0:
                cancelled = self.kernel.CancelIoEx(self.handle, ctypes.byref(operation))
                self.diagnostics["last_cancel_succeeded"] = bool(cancelled)
                if not cancelled:
                    self.diagnostics["last_cancel_win32_code"] = ctypes.get_last_error()
                # Wait briefly for only this operation's cancellation. Never free
                # a still-pending OVERLAPPED or its buffer on an uncertain driver.
                if self.kernel.WaitForSingleObject(event, 250) != 0:
                    _PENDING_IO.append((self.kernel, event, operation, buffer))
                    retained = True
                raise PipeTimeout("Native pipe operation timed out")
            if not self.kernel.GetOverlappedResult(self.handle, ctypes.byref(operation), ctypes.byref(transferred), False):
                self.diagnostics["last_win32_code"] = ctypes.get_last_error()
                raise CodexPipeError("Native pipe I/O failed")
            if transferred.value <= 0 or transferred.value > count:
                raise CodexPipeError("Native pipe closed or returned an invalid length")
            key = "bytes_written_completed" if writing else "bytes_read_completed"
            self.diagnostics[key] += transferred.value
            return transferred.value
        finally:
            if not retained:
                self.kernel.CloseHandle(event)

    def read_exact(self, count: int, deadline: float) -> bytes:
        result = bytearray()
        while len(result) < count:
            buffer = ctypes.create_string_buffer(count - len(result))
            received = self._io(buffer, len(buffer), deadline, writing=False)
            result.extend(buffer.raw[:received])
        return bytes(result)

    def write_all(self, data: bytes, deadline: float) -> None:
        offset = 0
        while offset < len(data):
            buffer = ctypes.create_string_buffer(data[offset:], len(data) - offset)
            offset += self._io(buffer, len(buffer), deadline, writing=True)

    def close(self) -> None:
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class CodexAppToolsAdapter:
    def __init__(self, *, installed_executable: Path = INSTALLED_EXECUTABLE,
                 system: PipeSystem | None = None, timeout: float = 5,
                 ledger_path: Path | None = None,
                 authorize_send: Callable[[Mapping[str, Any]], bool] | None = None,
                 validate_source: Callable[[str], bool] | None = None):
        if not 0 < timeout <= 60:
            raise CodexPipeError("Invalid native operation timeout")
        self.installed_executable = installed_executable
        self.system = system
        self.timeout = timeout
        self.ledger_path = ledger_path
        self.authorize_send = authorize_send
        self.validate_source = validate_source
        self.last_probe_diagnostics: dict[str, Any] = {}

    def _source(self, source_thread_id: str) -> None:
        actual = os.environ.get("CODEX_THREAD_ID")
        if (not isinstance(source_thread_id, str) or not ((actual and source_thread_id == actual)
                or (self.validate_source is not None and self.validate_source(source_thread_id) is True))):
            raise CodexPipeError("Source must be observed native routing metadata validated by the trusted runtime")
        self._identifier(source_thread_id)

    @staticmethod
    def _identifier(value: str) -> None:
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 33 for c in value):
            raise CodexPipeError("Invalid native identifier")

    def _connect(self) -> tuple[PipeIO, dict[str, Any]]:
        if os.path.normcase(str(self.installed_executable)) != os.path.normcase(str(INSTALLED_EXECUTABLE)):
            raise CodexPipeError("Native installation is not in the inspected allowlist", reason_code="pin_unavailable")
        if self.system is None:
            plugin = self.installed_executable.parent / "resources" / "plugins" / "openai-bundled" / "plugins" / "codex-app-tools"
            try:
                if (_hash(self.installed_executable.read_bytes()) != EXECUTABLE_SHA256
                        or _hash((plugin / "server.mjs").read_bytes()) != SERVER_SHA256
                        or _hash((plugin / ".mcp.json").read_bytes()) != MCP_CONFIG_SHA256
                        or _hash((self.installed_executable.parent.parent / "AppxManifest.xml").read_bytes()) != APP_MANIFEST_SHA256
                        or _installed_main_hash(self.installed_executable.parent / "resources" / "app.asar") != MAIN_MODULE_SHA256):
                    raise CodexPipeError("Installed native protocol version changed", reason_code="pin_mismatch")
            except OSError:
                raise CodexPipeError("Pinned installed native protocol is unavailable", reason_code="pin_unavailable") from None
        system = self.system if self.system is not None else Win32PipeSystem()
        candidates = system.candidates(MAX_CANDIDATES)
        if len(candidates) > MAX_CANDIDATES or len(set(candidates)) != len(candidates):
            raise CodexPipeError("Ambiguous scoped discovery", reason_code="owner_mismatch")
        own_sid = system.current_sid()
        if not own_sid:
            raise CodexPipeError("Current owner identity is unavailable", reason_code="owner_mismatch")
        accepted: list[tuple[PipeIO, str]] = []
        owner_mismatch = False
        try:
            for candidate in candidates:
                if not PIPE_PATTERN.fullmatch(candidate):
                    raise CodexPipeError("Unexpected discovered pipe scope")
                pipe = None
                try:
                    pipe = system.open(candidate)
                    identity = pipe.identity
                    if (type(identity.pid) is not int or identity.pid <= 0 or identity.sid != own_sid
                            or os.path.normcase(identity.executable) != os.path.normcase(str(self.installed_executable))):
                        owner_mismatch = True
                        raise CodexPipeError("Scoped native pipe owner did not match", reason_code="owner_mismatch")
                    accepted.append((pipe, _hash(candidate)))
                    pipe = None
                except (CodexPipeError, OSError):
                    pass
                finally:
                    if pipe is not None:
                        pipe.close()
            if len(accepted) != 1:
                raise CodexPipeError("No unique verified installed native pipe",
                                     reason_code="owner_mismatch" if owner_mismatch or accepted else "native_unavailable")
            pipe, path_hash = accepted.pop()
            return pipe, {"status": "available", "candidate_count": len(candidates),
                          "pipe_path_sha256": path_hash, "server_pid": pipe.identity.pid,
                          "server_executable_path_sha256": _hash(pipe.identity.executable),
                          "same_user_verified": True, "same_handle_server_verified": True,
                          "owner_sid_sha256": _hash(own_sid),
                          "native_message_sent": False, "model_invoked": False, **PROVENANCE}
        finally:
            for pipe, _ in accepted:
                pipe.close()

    def discover(self) -> dict[str, Any]:
        pipe, metadata = self._connect()
        pipe.close()
        return metadata

    def _rpc(self, pipe: PipeIO, request_id: int, method: str, params: dict[str, Any], deadline: float,
             trace: dict[str, Any] | None = None) -> dict[str, Any]:
        phase = "tools_list" if method == "tools/list" else "tool_call"
        if trace is not None:
            trace["phase"] = phase + "_write"
        pipe.write_all(encode_frame({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}), deadline)
        if trace is not None:
            trace["phase"] = phase + "_header_read"
        size = struct.unpack("<I", pipe.read_exact(4, deadline))[0]
        if not 0 < size <= MAX_FRAME:
            raise CodexPipeError("Invalid native frame length")
        if trace is not None:
            trace["phase"] = phase + "_body_read"
        response = _parse(pipe.read_exact(size, deadline))
        if trace is not None:
            trace["phase"] = phase + "_response_validation"
        if (response.get("jsonrpc") != "2.0" or type(response.get("id")) is not int
                or response["id"] != request_id or ("result" in response) == ("error" in response)):
            raise CodexPipeError("Unexpected native RPC response")
        if "error" in response:
            raise CodexPipeError("Native RPC explicitly rejected the request")
        result = response["result"]
        if not isinstance(result, dict):
            raise CodexPipeError("Invalid native RPC result")
        return result

    def _tools(self, pipe: PipeIO, deadline: float, trace: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        result = self._rpc(pipe, 1, "tools/list", {"threadStartKind": "all"}, deadline, trace)
        tools = result.get("tools")
        if not isinstance(tools, list) or len(tools) > 1024:
            raise CodexPipeError("Invalid native tool inventory")
        seen: set[tuple[str, str]] = set()
        for tool in tools:
            if not isinstance(tool, dict) or not all(isinstance(tool.get(k), str) for k in ("namespace", "name")) or not isinstance(tool.get("inputSchema"), dict):
                raise CodexPipeError("Invalid native tool schema")
            key = (tool["namespace"], tool["name"])
            if key in seen:
                raise CodexPipeError("Duplicate native tool identity")
            seen.add(key)
        return tools

    @staticmethod
    def _call(tool: str, arguments: dict[str, Any], source: str) -> dict[str, Any]:
        return {"arguments": arguments, "callerSource": "codex", "callId": "mcp-call-" + str(uuid.uuid4()),
                "namespace": "codex_app", "threadId": source, "tool": tool, "turnId": "mcp-turn-2"}

    def probe_metadata(self, source_thread_id: str) -> dict[str, Any]:
        started = time.monotonic()
        self.last_probe_diagnostics = {"phase": "source_validation", "tools_list_calls": 0,
                                       "list_artifacts_calls": 0, "native_send_calls": 0}
        pipe = None
        try:
            self._source(source_thread_id)
            self.last_probe_diagnostics["phase"] = "owner_verification"
            pipe, metadata = self._connect()
            deadline = time.monotonic() + self.timeout
            self.last_probe_diagnostics["tools_list_calls"] = 1
            tools = self._tools(pipe, deadline, self.last_probe_diagnostics)
            selected = [t for t in tools if (t["namespace"], t["name"]) == ("codex_app", "list_artifacts")]
            if not selected:
                raise CodexPipeError("Current native metadata tool is unavailable")
            schema = selected[0]["inputSchema"]
            if schema.get("type") != "object" or schema.get("required", []) or schema.get("properties", {}):
                raise CodexPipeError("Current native metadata schema changed", reason_code="schema_changed")
            self.last_probe_diagnostics["list_artifacts_calls"] = 1
            result = self._rpc(pipe, 2, "tools/call", self._call("list_artifacts", {}, source_thread_id), deadline, self.last_probe_diagnostics)
            items = result.get("contentItems")
            if result.get("success") is not True or not isinstance(items, list) or len(items) != 1:
                raise CodexPipeError("Native metadata query did not succeed")
            item = items[0]
            if not isinstance(item, dict) or item.get("type") != "inputText" or not isinstance(item.get("text"), str):
                raise CodexPipeError("Unexpected native metadata response")
            value = _parse(item["text"].encode("utf-8"))
            if not isinstance(value.get("artifacts"), list):
                raise CodexPipeError("Unexpected native artifact metadata")
            self.last_probe_diagnostics["phase"] = "complete"
            return {**metadata, "status": "metadata_verified", "source_thread_sha256": _hash(source_thread_id),
                    "tool_count": len(tools), "selected_schema_sha256": _hash(_json(schema)),
                    "tool_inventory_sha256": _hash(_json(tools)), "artifact_count": len(value["artifacts"]),
                    "artifact_metadata_sha256": _hash(item["text"]), "native_tool_success": True,
                    "tools_list_calls": 1, "list_artifacts_calls": 1,
                    "message_history_read": False, "native_result_verified": False}
        except CodexPipeError as error:
            self.last_probe_diagnostics["reason_code"] = error.reason_code
            raise
        finally:
            if pipe is not None:
                self.last_probe_diagnostics.update(getattr(pipe, "diagnostics", {}))
                pipe.close()
            self.last_probe_diagnostics["elapsed_seconds"] = round(time.monotonic() - started, 6)

    def _authorize(self, record: Mapping[str, Any], *, target: str, prompt: str, source: str,
                   root: str, dispatch: str, deadline: float) -> None:
        self._source(source)
        for value in (target, root, dispatch):
            self._identifier(value)
        if not isinstance(prompt, str) or not 1 <= len(prompt.encode("utf-8")) <= 65536:
            raise CodexPipeError("Invalid bounded native prompt")
        if type(deadline) not in (int, float) or not math.isfinite(deadline) or deadline <= time.time():
            raise CodexPipeError("Native dispatch deadline expired")
        expected = {"source_thread_id": source, "target_thread_id": target, "root_id": root,
                    "dispatch_id": dispatch, "prompt_sha256": _hash(prompt), "deadline": deadline}
        if (not isinstance(record, Mapping) or any(record.get(k) != v for k, v in expected.items())
                or record.get("human_authorized") is not True or record.get("root_active") is not True
                or record.get("recipient_registered") is not True or record.get("workspace_paused") is not False
                or record.get("recipient_paused") is not False or record.get("claim_state") != "claimed"
                or type(record.get("budget_remaining")) is not int or record["budget_remaining"] <= 0):
            raise CodexPipeError("Native dispatch lacks the exact active human authorization")
        if self.ledger_path is None or self.authorize_send is None or self.authorize_send(record) is not True:
            raise CodexPipeError("A durable native authorization gate is required")

    @contextmanager
    def _ledger(self):
        path = self.ledger_path
        if path is None or not path.parent.is_dir():
            raise CodexPipeError("Native dispatch ledger is unavailable")
        for part in [path, *path.parents]:
            if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
                raise CodexPipeError("Native dispatch ledger cannot be linked")
        if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
            raise CodexPipeError("Native dispatch ledger cannot be linked")
        db = sqlite3.connect(path, timeout=3)
        try:
            db.execute("CREATE TABLE IF NOT EXISTS codex_dispatch (dispatch_id TEXT PRIMARY KEY,target_id TEXT NOT NULL,root_id TEXT NOT NULL,prompt_sha256 TEXT NOT NULL,receipt TEXT NOT NULL)")
            with db:
                yield db
        finally:
            db.close()

    def send_authorized(self, target_thread_id: str, prompt: str, *, source_thread_id: str,
                        root_id: str, dispatch_id: str, deadline: float,
                        authorization_record: Mapping[str, Any]) -> dict[str, Any]:
        """One native tool call after two exact durable human/control checks.

        Never retries, reconnects, resumes, creates a conversation, or falls back
        to a CLI/API. Acceptance is not a result or a completed native GUI turn.
        """
        arguments = dict(target=target_thread_id, prompt=prompt, source=source_thread_id,
                         root=root_id, dispatch=dispatch_id, deadline=deadline)
        self._authorize(authorization_record, **arguments)
        receipt = {"state": "prepared", "native_accepted": False, "native_result_verified": False,
                   "native_input_attempted": False,
                   "automatic_retry": False, "fallback_attempted": False,
                   "dispatch_id": dispatch_id, "root_id": root_id,
                   "target_thread_sha256": _hash(target_thread_id), "prompt_sha256": _hash(prompt)}
        with self._ledger() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT target_id,root_id,prompt_sha256,receipt FROM codex_dispatch WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if prior:
                if prior[:3] != (target_thread_id, root_id, _hash(prompt)):
                    raise CodexPipeError("Native dispatch identity was reused with different content")
                return {**json.loads(prior[3]), "duplicate_suppressed": True}
            db.execute("INSERT INTO codex_dispatch VALUES (?,?,?,?,?)", (dispatch_id, target_thread_id, root_id, _hash(prompt), json.dumps(receipt)))
        pipe = None
        trace: dict[str, Any] = {"phase": "owner_verification"}
        try:
            pipe, _ = self._connect()
            io_deadline = time.monotonic() + min(self.timeout, max(0, deadline - time.time()))
            tools = self._tools(pipe, io_deadline, trace)
            selected = [t for t in tools if (t["namespace"], t["name"]) == ("codex_app", "send_message_to_thread")]
            if not selected:
                raise CodexPipeError("Native messaging tool is unavailable")
            schema = selected[0]["inputSchema"]
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            trace["phase"] = "schema_validation"
            if (schema.get("type") != "object" or not isinstance(properties, dict)
                    or not {"threadId", "prompt"}.issubset(properties)
                    or not isinstance(required, list) or any(type(item) is not str for item in required)
                    or set(required) != {"threadId", "prompt"}
                    or any(not isinstance(properties[k], dict) or properties[k].get("type") != "string"
                           for k in ("threadId", "prompt"))):
                raise CodexPipeError("Native messaging schema changed", reason_code="schema_changed")
            # The trusted caller must read current durable controls here, after
            # schema discovery and immediately before marking the one attempt.
            trace["phase"] = "authorization_recheck"
            self._authorize(authorization_record, **arguments)
            receipt["state"] = "delivery_unknown"
            receipt["native_input_attempted"] = True
            with self._ledger() as db:
                db.execute("UPDATE codex_dispatch SET receipt=? WHERE dispatch_id=?", (json.dumps(receipt), dispatch_id))
            result = self._rpc(pipe, 2, "tools/call", self._call("send_message_to_thread", {"threadId": target_thread_id, "prompt": prompt}, source_thread_id), io_deadline, trace)
            if result.get("success") is True:
                items = result.get("contentItems")
                if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
                        or items[0].get("type") != "inputText" or not isinstance(items[0].get("text"), str)):
                    raise CodexPipeError("Native acknowledgement omitted the exact effective target")
                acknowledgement = _parse(items[0]["text"].encode("utf-8"))
                if acknowledgement.get("threadId") != target_thread_id:
                    receipt["acknowledgement"] = "effective_target_mismatch"
                    raise CodexPipeError("Native acknowledgement did not match the exact effective target")
                receipt.update(state="native_accepted", native_accepted=True)
            elif result.get("success") is False:
                receipt.update(state="native_tool_failed")
            else:
                raise CodexPipeError("Invalid native messaging acknowledgement")
        except (CodexPipeError, OSError) as error:
            if receipt["state"] == "prepared":
                receipt["state"] = "not_sent"
            receipt["reason_code"] = error.reason_code if isinstance(error, CodexPipeError) else "native_unavailable"
            receipt["failure_phase"] = trace["phase"]
            receipt["error"] = "Native delivery is unavailable or uncertain; automatic replay is disabled"
        finally:
            if pipe is not None:
                pipe.close()
        with self._ledger() as db:
            db.execute("UPDATE codex_dispatch SET receipt=? WHERE dispatch_id=?", (json.dumps(receipt), dispatch_id))
        return receipt
