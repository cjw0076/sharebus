"""Bounded device observations, separate from workspace session authority.

Only process identity metadata is read. No command lines, process memory,
environment, native conversation stores, resume commands, or model calls.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes
import json
import math
import ntpath
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any

PROVIDERS = ("codex", "claude", "antigravity", "gemini", "chatgpt", "cursor", "hermes", "grok")
NAMES = {
    "codex": {"codex", "codex.exe"}, "claude": {"claude", "claude.exe"},
    "antigravity": {"agy", "agy.exe", "antigravity", "antigravity.exe"},
    "gemini": {"gemini", "gemini.exe"}, "chatgpt": {"chatgpt", "chatgpt.exe"},
    "cursor": {"cursor", "cursor.exe"}, "hermes": {"hermes", "hermes.exe"},
    "grok": {"grok", "grok.exe"},
}
MAX_PROCESSES = 512
MAX_SESSIONS = 64
SESSION_TIMEOUT = 3


class DiscoveryError(OSError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _windows_processes(names: set[str]) -> list[dict[str, Any]]:
    """One handle supplies image, creation time and owner; PID reuse is not identity."""
    from ctypes import wintypes as w

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", w.LONG), ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]

    kernel, security = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes, kernel.CreateToolhelp32Snapshot.restype = [w.DWORD, w.DWORD], w.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [w.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32FirstW.restype = kernel.Process32NextW.restype = w.BOOL
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessTimes.restype = w.BOOL
    kernel.GetExitCodeProcess.argtypes, kernel.GetExitCodeProcess.restype = [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
    security.OpenProcessToken.argtypes, security.OpenProcessToken.restype = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL
    security.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    security.GetTokenInformation.restype = w.BOOL
    security.GetLengthSid.argtypes, security.GetLengthSid.restype = [ctypes.c_void_p], w.DWORD

    def owner(handle):
        token = w.HANDLE()
        if not security.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
            raise DiscoveryError("owner_denied")
        try:
            size = w.DWORD()
            security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not 0 < size.value <= 65536:
                raise DiscoveryError("owner_denied")
            buffer = ctypes.create_string_buffer(size.value)
            if not security.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise DiscoveryError("owner_denied")
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            length = security.GetLengthSid(sid)
            if not 0 < length <= 1024:
                raise DiscoveryError("owner_denied")
            return ctypes.string_at(sid, length)
        finally:
            kernel.CloseHandle(token)

    own = owner(kernel.GetCurrentProcess())
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise DiscoveryError("process_snapshot_failed")
    rows = []
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(Entry)
        more = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not more and ctypes.get_last_error() != 18:
            raise DiscoveryError("process_snapshot_failed")
        visited = 0
        while more:
            visited += 1
            if visited > 32768:
                raise DiscoveryError("process_snapshot_limit")
            if entry.szExeFile.casefold() in names:
                if len(rows) >= MAX_PROCESSES:
                    raise DiscoveryError("process_candidate_limit")
                row = {"pid": int(entry.th32ProcessID), "parent_pid": int(entry.th32ParentProcessID),
                       "name": entry.szExeFile, "executable": None, "started_at": None, "owner_state": "denied"}
                handle = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
                if handle:
                    try:
                        row["owner_state"] = "same_user" if owner(handle) == own else "other_user"
                        if row["owner_state"] == "same_user":
                            size, image = w.DWORD(32768), ctypes.create_unicode_buffer(32768)
                            created, exited, cpu, user = (w.FILETIME() for _ in range(4))
                            alive = w.DWORD()
                            if (kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size))
                                    and kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(cpu), ctypes.byref(user))
                                    and kernel.GetExitCodeProcess(handle, ctypes.byref(alive)) and alive.value == 259):
                                row.update(executable=image.value,
                                           started_at=((created.dwHighDateTime << 32) | created.dwLowDateTime) / 10000000 - 11644473600)
                            else:
                                row["owner_state"] = "unknown"
                    except DiscoveryError:
                        row["owner_state"] = "denied"
                    finally:
                        kernel.CloseHandle(handle)
                rows.append(row)
            more = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() not in (0, 18):
            raise DiscoveryError("process_snapshot_failed")
    finally:
        kernel.CloseHandle(snapshot)
    return rows


def read_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        # Other platforms require a reviewed owner/image/start-time adapter.
        raise DiscoveryError("platform_process_adapter_unavailable")
    return _windows_processes(set().union(*NAMES.values()))


def _path_key(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value)) if "\\" in value or re.match(r"^[A-Za-z]:", value) else os.path.normcase(os.path.normpath(value))


def _codex_package(executable: str) -> bool:
    root = _path_key(str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"))
    # ChatGPT.exe is also the Codex desktop image. Match its package family,
    # exact image location and protected install root, never just its basename.
    tail = _path_key(executable)
    return bool(re.fullmatch(re.escape(root) + r"\\openai\.codex_[0-9.]+_(?:x64|arm64)__2p2nqsd0c76g0\\app\\chatgpt\.exe", tail))


def _known_image(provider: str, executable: str, installed: str | None) -> str | None:
    """Classify observed install locations, not an executable's claimed basename."""
    value = _path_key(executable)
    if provider == "codex" and _codex_package(executable):
        return "desktop"
    program_files = _path_key(str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"))
    local = _path_key(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    roaming = _path_key(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
    if provider == "claude":
        if re.fullmatch(re.escape(program_files) + r"\\claude_[0-9.]+_(?:x64|arm64)__pzs8sxrjxfjjc\\app\\claude\.exe", value):
            return "desktop"
        if re.fullmatch(re.escape(roaming) + r"\\claude\\claude-code\\[0-9.]+\\[0-9a-f]+\\claude\.exe", value):
            return "cli"
        if value == _path_key(str(Path.home() / ".local/bin/claude.exe")):
            return "cli"
        versions = _path_key(str(Path.home() / ".local/share/claude/versions"))
        if re.fullmatch(re.escape(versions) + r"[\\/][0-9.]+\.exe", value):
            return "cli"
        # An arbitrary PATH entry is an installation candidate, never a reason
        # to execute that image automatically during metadata discovery.
        return None
    if provider == "antigravity" and value == local + r"\programs\antigravity\antigravity.exe":
        return "desktop"
    if provider == "gemini" and re.fullmatch(re.escape(local) + r"\\google\\gemini\\app-[0-9.]+\\gemini\.exe", value):
        return "desktop"
    if installed and value == _path_key(installed):
        return "desktop" if provider in {"cursor", "chatgpt"} else "cli"
    return None


def _canonical_workspace(value: Any) -> str | None:
    if not isinstance(value, str) or not Path(value).is_absolute():
        return None
    path = Path(value)
    try:
        for ancestor in (path, *path.parents):
            if ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400:
                return None
        return os.path.normcase(str(path.resolve(strict=True))) if path.is_dir() else None
    except OSError:
        return None


def _claude_sessions(executable: str, workspace: Path, processes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stamp = time.time()
    if (not executable or _known_image("claude", executable, None) != "cli"
            or not any(row["provider_verified"] and _path_key(row["executable"]) == _path_key(executable) for row in processes)):
        return [], {"state": "unavailable", "observed_at": stamp, "error_code": "trusted_running_cli_unavailable"}
    if Path(executable).suffix.casefold() in {".cmd", ".bat", ".ps1"}:
        return [], {"state": "unavailable", "observed_at": stamp, "error_code": "shell_cli_not_supported"}
    try:
        completed = subprocess.run([executable, "agents", "--json"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=SESSION_TIMEOUT, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode or len(completed.stdout.encode("utf-8")) > 1024 * 1024:
            raise DiscoveryError("session_scan_failed")
        values = json.loads(completed.stdout.lstrip("\ufeff"))
        values = values.get("agents", values.get("sessions")) if isinstance(values, dict) else values
        if not isinstance(values, list) or len(values) > 4096:
            raise DiscoveryError("session_scan_invalid")
        wanted, sessions = _canonical_workspace(str(workspace)), []
        live = {row["pid"] for row in processes if row["owner_state"] == "same_user" and row["provider_verified"]}
        for value in values:
            if (not isinstance(value, dict) or not isinstance(value.get("sessionId"), str)
                    or not 0 < len(value["sessionId"]) <= 256 or any(ord(c) < 32 for c in value["sessionId"])
                    or _canonical_workspace(value.get("cwd")) != wanted):
                continue
            pid = value.get("pid")
            if type(pid) is not int or pid not in live:
                continue
            sessions.append({"native_session_id": value["sessionId"], "cwd": str(workspace), "pid": pid,
                "kind": str(value.get("kind", "unknown"))[:64], "status": str(value.get("status", "unknown"))[:64],
                "requires_attach": True, "capability_verified": False})
            if len(sessions) >= MAX_SESSIONS:
                break
        return sessions, {"state": "ready", "observed_at": stamp, "error_code": None}
    except subprocess.TimeoutExpired:
        return [], {"state": "error", "observed_at": stamp, "error_code": "session_scan_timeout"}
    except (OSError, ValueError, TypeError):
        return [], {"state": "error", "observed_at": stamp, "error_code": "session_scan_failed"}


def scan_device(workspace: Path, *, process_reader=None, resolver=None, session_reader=None) -> list[dict[str, Any]]:
    if resolver is None:
        from discovery import resolve_executable
        resolver = resolve_executable
    observed = (process_reader or read_processes)()
    if not isinstance(observed, list) or len(observed) > MAX_PROCESSES:
        raise DiscoveryError("process_candidate_limit")
    installed = {provider: resolver(provider) for provider in PROVIDERS}
    by_provider: dict[str, list[dict[str, Any]]] = {provider: [] for provider in PROVIDERS}
    inaccessible = 0
    for value in observed:
        if not isinstance(value, dict) or type(value.get("pid")) is not int or value["pid"] <= 0:
            continue
        name = str(value.get("name", ""))[:260]
        executable = value.get("executable") if value.get("owner_state") == "same_user" else None
        executable = executable if isinstance(executable, str) and 0 < len(executable) <= 32768 else None
        started = value.get("started_at")
        started = started if type(started) in (int, float) and math.isfinite(started) and started > 0 else None
        if value.get("owner_state") != "same_user" or not executable or not started:
            inaccessible += 1
            continue
        packaged_codex = bool(executable and _codex_package(executable))
        provider = "codex" if packaged_codex else next((key for key, names in NAMES.items() if name.casefold() in names), None)
        if not provider:
            continue
        image_kind = _known_image(provider, executable, installed[provider]) if executable else None
        verified = bool(executable and started and value.get("owner_state") == "same_user" and image_kind)
        identity = f"{value['pid']}:{started:.7f}" if executable and started else None
        by_provider[provider].append({"pid": value["pid"], "parent_pid": value.get("parent_pid") if type(value.get("parent_pid")) is int else None,
            "name": name, "executable": executable, "started_at": started if executable else None,
            "identity": identity, "owner_state": value.get("owner_state") if value.get("owner_state") in {"same_user", "denied", "other_user", "unknown"} else "unknown",
            "provider_verified": verified, "image_kind": image_kind, "group_id": identity, "helper": False})
    results = []
    for provider in PROVIDERS:
        processes = by_provider[provider]
        index = {row["pid"]: row for row in processes if row["provider_verified"]}
        for row in processes:
            parent, visited = index.get(row["parent_pid"]), {row["pid"]}
            while row["provider_verified"] and parent and parent["pid"] not in visited:
                if (parent["started_at"] > row["started_at"] or _path_key(parent["executable"]) != _path_key(row["executable"])):
                    break
                row.update(group_id=parent["identity"], helper=True)
                visited.add(parent["pid"])
                parent = index.get(parent["parent_pid"])
        sessions, session_scan = ([], {"state": "unsupported", "observed_at": None, "error_code": None})
        if provider == "claude":
            cli = next((row["executable"] for row in processes if row["provider_verified"] and row["image_kind"] == "cli"), installed[provider])
            sessions, session_scan = (session_reader or _claude_sessions)(cli, workspace, processes)
        running = sum(row["provider_verified"] for row in processes)
        results.append({"provider": provider, "installed": bool(installed[provider] or running), "running": bool(running),
            "device_inaccessible_count": inaccessible,
            "process_count": running, "candidate_count": len(processes) - running,
            "app_count": len({row["group_id"] for row in processes if row["provider_verified"]}), "processes": processes,
            "attachment": "detected" if running else "candidate" if processes else "pending_capability",
            "verified": False, "capabilities": [], "native_sessions": sessions, "session_scan": session_scan,
            "notes": "Device observation only; session metadata and process identity do not grant workspace membership or input authority"})
    return results


class DeviceDiscovery:
    """Server-owned bounded background cache. Snapshot never scans or starts work."""
    def __init__(self, workspace: Path, *, enabled=True, scanner=None, interval=15):
        self.workspace, self.enabled = Path(workspace), bool(enabled)
        self.scanner, self.interval = scanner or scan_device, max(0.05, float(interval))
        self._lock, self._stop, self._wake = threading.Lock(), threading.Event(), threading.Event()
        self._thread = None
        self._rows: list[dict[str, Any]] = []
        self._status = {"state": "pending" if enabled else "disabled", "observed_at": None,
                        "last_success_at": None, "stale": False, "refreshing": False, "error_code": None, "inaccessible_count": 0}

    def start(self):
        with self._lock:
            if not self.enabled or self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="avalokita-device-discovery", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=SESSION_TIMEOUT + 2)

    def refresh(self):
        if self.enabled:
            self._wake.set()

    def snapshot(self):
        with self._lock:
            status = dict(self._status)
            if status["last_success_at"] is not None and time.time() - status["last_success_at"] > self.interval * 2 + SESSION_TIMEOUT:
                status["stale"] = True
            return deepcopy(self._rows), status

    def _scan(self):
        with self._lock:
            self._status["refreshing"] = True
        try:
            rows = self.scanner(self.workspace)
            stamp = time.time()
            with self._lock:
                if self._stop.is_set():
                    return
                # Preserve last-good supported metadata explicitly on a CLI
                # timeout; a successful empty result instead clears old sessions.
                prior = {row["provider"]: row for row in self._rows}
                for row in rows:
                    old = prior.get(row["provider"], {})
                    scan = row["session_scan"]
                    if scan["state"] == "error" and old.get("native_sessions"):
                        row["native_sessions"] = old["native_sessions"]
                        scan.update(stale=True, last_success_at=old.get("session_scan", {}).get("last_success_at"))
                    else:
                        scan.update(stale=False, last_success_at=stamp if scan["state"] == "ready" else None)
                self._rows = rows
                inaccessible = max((row.get("device_inaccessible_count", 0) for row in rows), default=0)
                self._status.update(state="ready", observed_at=stamp, last_success_at=stamp, stale=False,
                    inaccessible_count=inaccessible, error_code="process_identity_unavailable" if inaccessible else None)
        except (OSError, ValueError, TypeError) as error:
            with self._lock:
                self._status.update(state="error", observed_at=time.time(), stale=bool(self._status["last_success_at"]),
                                    error_code=error.code if isinstance(error, DiscoveryError) else "process_scan_failed")
        finally:
            with self._lock:
                self._status["refreshing"] = False

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self._scan()
            self._wake.wait(self.interval)
