"""Read-only discovery of local clients; process presence is never session access."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess


PROVIDERS = ("codex", "claude", "antigravity", "gemini", "chatgpt", "cursor", "hermes")
MANAGED_PROVIDERS = ("codex", "claude", "antigravity")
_PROCESS_NAMES = {
    "codex": {"codex", "codex.exe"},
    "claude": {"claude", "claude.exe"},
    "antigravity": {"agy", "agy.exe", "antigravity", "antigravity.exe"},
    "gemini": {"gemini", "gemini.exe"},
    "chatgpt": {"chatgpt", "chatgpt.exe"},
    "cursor": {"cursor", "cursor.exe"},
    "hermes": {"hermes", "hermes.exe"},
}


def resolve_executable(provider: str) -> str | None:
    """Locate an executable without opening configuration, auth, or transcripts."""
    provider = "antigravity" if provider == "agy" else provider
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local")))
    candidates: list[Path] = []
    if provider == "codex":
        directory = local / "OpenAI/Codex/bin"
        if directory.is_dir():
            candidates.extend(sorted(directory.glob("*/codex.exe"), key=lambda path: path.stat().st_mtime, reverse=True))
    elif provider == "claude":
        candidates.append(home / ".local/bin/claude.exe")
    elif provider == "antigravity":
        candidates.append(local / "agy/bin/agy.exe")
    elif provider == "cursor":
        candidates.append(local / "Programs/cursor/Cursor.exe")
    elif provider == "chatgpt":
        candidates.append(local / "Microsoft/WindowsApps/ChatGPT.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    command = "agy" if provider == "antigravity" else provider
    return shutil.which(command + ".exe") or shutil.which(command)


def _running_processes() -> list[dict]:
    """Only inspect PID, process name, and executable path, never command lines."""
    try:
        if os.name == "nt":
            powershell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
            if not powershell:
                return []
            names = sorted({name for values in _PROCESS_NAMES.values() for name in values if name.endswith(".exe")})
            literals = ",".join("'" + name + "'" for name in names)
            script = (
                "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @("
                + literals
                + ") } | Select-Object ProcessId,Name,ExecutablePath | ConvertTo-Json -Compress"
            )
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode or not result.stdout.strip():
                return []
            rows = json.loads(result.stdout.lstrip("\ufeff"))
            rows = [rows] if isinstance(rows, dict) else rows
            return [
                {"pid": int(row["ProcessId"]), "name": str(row["Name"]), "executable": row.get("ExecutablePath")}
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("ProcessId"), int) and isinstance(row.get("Name"), str)
            ]
        result = subprocess.run(["ps", "-eo", "pid=,comm="], capture_output=True, text=True, timeout=5, check=False)
        rows = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                rows.append({"pid": int(parts[0]), "name": Path(parts[1]).name, "executable": None})
        return rows
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def discover_native_sessions(provider: str, workspace: Path | None = None, timeout: float | None = None) -> list[dict]:
    """Inspect supported session metadata without reading conversations or resuming."""
    if provider != "claude":
        return []
    executable = resolve_executable(provider)
    if not executable:
        return []
    try:
        deadline = float(os.environ.get("SHAREBUS_DISCOVERY_TIMEOUT", "30")) if timeout is None else float(timeout)
        if not 0 < deadline <= 120:
            raise ValueError("Session discovery timeout must be between 0 and 120 seconds.")
        result = subprocess.run(
            [executable, "agents", "--json"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=deadline, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return []
        data = json.loads(result.stdout.lstrip("\ufeff"))
        if isinstance(data, dict):
            data = data.get("agents", data.get("sessions", []))
        if not isinstance(data, list):
            return []
        sessions = []
        wanted = os.path.normcase(str(Path(workspace).resolve())) if workspace else None
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("sessionId"), str):
                continue
            directory = item.get("cwd")
            if wanted and (not isinstance(directory, str) or os.path.normcase(str(Path(directory).resolve())) != wanted):
                continue
            sessions.append({
                "native_session_id": item["sessionId"], "name": item.get("name"),
                "cwd": directory, "pid": item.get("pid"), "kind": item.get("kind"),
                "status": item.get("status"), "waiting_for": item.get("waitingFor"),
                "requires_attach": True, "capability_verified": False,
                "notes": "Existing session metadata only. Resuming a busy session can clone it; no session was resumed.",
            })
        return sessions
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def discover(workspace: Path | None = None) -> list[dict]:
    """Installed/running candidates; only explicit adapters can create owned sessions."""
    processes = _running_processes()
    candidates = []
    for provider in PROVIDERS:
        executable = resolve_executable(provider)
        running = [row for row in processes if row["name"].lower() in _PROCESS_NAMES[provider]]
        sessions = discover_native_sessions(provider, workspace)
        managed = provider in MANAGED_PROVIDERS and bool(executable)
        if os.name == "nt" and executable and Path(executable).suffix.lower() in {".cmd", ".bat"}:
            managed = False
        candidates.append({
            "provider": provider,
            "installed": bool(executable or running),
            "available": managed,
            "runnable": managed,
            "running": running,
            "native_sessions": sessions,
            "executable": executable,
            "managed_supported": managed,
            "cli_interface_supported": managed,
            "capability_verified": False,
            "workspace_verified": False,
            "desktop_capabilities_verified": False,
            "existing_session_access": "metadata_only",
            "notes": "Process presence does not reveal a workspace or permit writing into an existing GUI session."
            if running else "No model request was made during discovery.",
        })
    return candidates


discover_agents = discover
