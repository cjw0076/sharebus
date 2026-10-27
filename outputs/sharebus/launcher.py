"""Click-only ShareBus setup. Run with pythonw; --help also works without Tk."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import queue
import stat
import sys
import threading
import traceback
from typing import Any

from discovery import resolve_executable
from install import SUPPORTED_PROVIDERS, apply, prepare, private_backup_root, select_providers
from sharebus import Client


APP_NAMES = {"codex": "Codex", "claude": "Claude", "antigravity": "Antigravity"}
CONFIG_SCOPES = {
    "codex": ".codex/config.toml · .codex/hooks.json · .agents/skills/sharebus",
    "claude": ".claude.json · .claude/settings.json · .claude/skills/sharebus · .claude/commands/sharebus.md",
    "antigravity": ".gemini/config (MCP · hooks · sidecar · skills)",
}
HOME_CONFIG_FILES = {
    "codex": (".codex/config.toml", ".codex/hooks.json", ".agents/skills/sharebus/SKILL.md"),
    "claude": (".claude.json", ".claude/settings.json", ".claude/skills/sharebus/SKILL.md", ".claude/commands/sharebus.md"),
    "antigravity": (".gemini/config/mcp_config.json", ".gemini/config/config.json", ".gemini/config/hooks.json",
                    ".gemini/config/sidecars/sharebus/sidecar.json", ".gemini/config/skills/sharebus/SKILL.md"),
}
MESSAGES = {
    "workspace": "선택한 작업실을 확인할 수 없습니다. 폴더를 다시 선택해 주세요.",
    "selection": "연결할 앱을 하나 이상 선택해 주세요.",
    "identity": "기존 연결의 신원을 확인하지 못했습니다. 고급 보기에서 확인해 주세요.",
    "configuration": "기존 설정을 보존하기 위해 연결을 멈췄습니다. 고급 보기에서 충돌 범위를 확인해 주세요.",
    "connection": "연결을 마치지 못했습니다. 잠시 후 다시 연결해 주세요.",
}


class LauncherError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(MESSAGES[code])


@dataclass(frozen=True)
class WorkspaceSelection:
    """The directory picked by the human, held fixed for the whole action."""
    path: Path
    device: int
    inode: int

    @classmethod
    def capture(cls, directory: str) -> WorkspaceSelection:
        try:
            path = Path(directory)
            if not path.is_absolute():
                raise ValueError
            resolved = path.resolve(strict=True)
            info = path.stat()
            selection = cls(path, info.st_dev, info.st_ino)
            selection.check()
            if resolved != path:
                raise ValueError
            return selection
        except (OSError, ValueError):
            raise LauncherError("workspace") from None

    def check(self) -> None:
        try:
            for part in (self.path, *self.path.parents):
                info = part.lstat()
                if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ValueError
            info = self.path.stat()
            if self.path.resolve(strict=True) != self.path or (info.st_dev, info.st_ino) != (self.device, self.inode):
                raise ValueError
            for suffix in (".sharebus", ".sharebus/private", ".claude", ".codex"):
                child = self.path / suffix
                if child.is_symlink():
                    raise ValueError
                if child.exists():
                    child_info = child.lstat()
                    if not stat.S_ISDIR(child_info.st_mode) or getattr(child_info, "st_file_attributes", 0) & 0x400:
                        raise ValueError
        except (OSError, ValueError):
            raise LauncherError("workspace") from None


def available_apps() -> dict[str, bool]:
    """Executable lookup only: never inspect sessions, start clients, or probe models."""
    found = {}
    for provider in SUPPORTED_PROVIDERS:
        try:
            found[provider] = resolve_executable(provider) is not None
        except (OSError, ValueError):
            found[provider] = False
    return found


def connection_summary(state: dict[str, Any]) -> str:
    auto = state.get("auto", {})
    if state.get("paused") or auto.get("paused") or auto.get("suspended"):
        return "잠시 멈춤"
    agents = state.get("agents", [])
    if any(agent.get("state") in {"needs_human", "stopped", "error"} for agent in agents):
        return "확인 필요"
    for status, label in (("offline", "연결 끊김"), ("unavailable", "사용 불가"),
                          ("starting", "연결 중"), ("waiting_for_user", "사용자 답변 대기")):
        if any(agent.get("state") == status for agent in agents):
            return label
    if agents and all(agent.get("state") == "paused" for agent in agents):
        return "잠시 멈춤"
    if any(agent.get("state") in {"busy", "running", "working", "awaiting_native_reply"} for agent in agents):
        return "작업 중"
    if not agents:
        return "앱 연결 대기"
    return "연결됨" if all(agent.get("state") == "idle" for agent in agents) else "확인 필요"


def diagnostic_details(error: BaseException, scope: tuple[str, ...] = ()) -> str:
    """Keep useful stack locations, excluding exception values, locals, and paths."""
    lines = ["설정 범위: " + (", ".join(scope) if scope else "설정 변경 전"),
             "오류 메시지·설정 내용·인증 정보는 표시하지 않습니다."]
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        lines.append("오류 형식: " + type(current).__name__)
        if isinstance(current, LauncherError):
            lines.append("단계: " + current.code)
        for frame in traceback.extract_tb(current.__traceback__):
            lines.append(f"  {Path(frame.filename).name}:{frame.lineno} ({frame.name})")
        current = current.__cause__
    return "\n".join(lines)


class WorkspaceActions:
    def __init__(self, selection: WorkspaceSelection, *, home: Path | None = None, python: Path | None = None):
        self.selection = selection
        self.home = home
        self.python = python
        self.scope: tuple[str, ...] = ()
        self.guard: Callable[[], None] = selection.check

    def _client(self) -> tuple[Client, bool]:
        self.guard()
        client = Client(self.selection.path)
        metadata = client.private / "runtime.json"
        try:
            info = metadata.lstat()
        except FileNotFoundError:
            return client, False
        if (not stat.S_ISREG(info.st_mode) or metadata.is_symlink() or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 65536):
            raise LauncherError("identity")
        try:
            client.runtime(verify=False)
            self.guard()
            try:
                client.runtime()
            except RuntimeError as error:
                if str(error) == "Sharebus daemon is unavailable; use start":
                    # Supported start retains the existing task ownership/OS lease
                    # checks. Never delete metadata, stop a PID, or force an upgrade.
                    return client, False
                raise
        except (OSError, ValueError, RuntimeError) as error:
            raise LauncherError("identity") from error
        return client, True

    def _start(self, client: Client, running: bool) -> None:
        self.guard()
        if not running:
            client.start("none", register_sidecar=False)
        self.guard()

    def connect(self, providers: tuple[str, ...], *, auto_enroll: bool = True) -> str:
        try:
            selected = select_providers(providers)
        except ValueError as error:
            raise LauncherError("selection") from error
        if type(auto_enroll) is not bool:
            raise LauncherError("selection")
        self.scope = tuple(CONFIG_SCOPES[provider] for provider in selected)
        home = (self.home or Path.home()).absolute()
        targets = [home / suffix for provider in selected for suffix in HOME_CONFIG_FILES[provider]]
        directories = {home, *home.parents, *(parent for path in targets for parent in path.parents if parent.is_relative_to(home))}
        if "antigravity" in selected:
            directories.add(home / ".gemini/config/sidecars/sharebus/workspaces")
        identities: dict[Path, tuple[int, int]] = {}

        def guard() -> None:
            self.selection.check()
            try:
                for directory in sorted(directories, key=lambda path: len(path.parts)):
                    try:
                        info = directory.lstat()
                    except FileNotFoundError:
                        if directory in identities or directory == home:
                            raise ValueError
                        continue
                    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                            or getattr(info, "st_file_attributes", 0) & 0x400):
                        raise ValueError
                    identity = info.st_dev, info.st_ino
                    if directory in identities and identities[directory] != identity:
                        raise ValueError
                    identities[directory] = identity
                for path in targets:
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue
                    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1
                            or getattr(info, "st_file_attributes", 0) & 0x400):
                        raise ValueError
            except (OSError, ValueError):
                raise LauncherError("configuration") from None

        self.guard = guard
        self.guard()
        client, running = self._client()
        python = self.python or Path(sys.executable)
        self.guard()
        try:
            changes = prepare(home, python, providers=selected, hooks_workspace=self.selection.path, auto_hooks=True,
                              workspace_guard=self.guard)
            self.guard()
            self.scope = tuple(("작업실: " + str(path.relative_to(self.selection.path))
                                if path.is_relative_to(self.selection.path) else "사용자: " + str(path.relative_to(home)))
                               for path in changes)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = private_backup_root(workspace_guard=self.guard) / stamp
            apply(changes, backup, workspace_guard=self.guard)
        except (OSError, ValueError) as error:
            raise LauncherError("configuration") from error
        self._start(client, running)
        self.guard()
        if "antigravity" in selected:
            from sidecar_registry import register_workspace
            register_workspace(self.selection.path, client.runtime(),
                               registry_path=home / ".gemini/config/sidecars/sharebus/workspaces", workspace_guard=self.guard)
        self.guard()
        client.request("/api/auto/config", {"enabled": auto_enroll})
        self.guard()
        state = client.request("/api/state")
        self.guard()
        client.open()
        return connection_summary(state)

    def open_console(self) -> str:
        client, running = self._client()
        self._start(client, running)
        self.guard()
        state = client.request("/api/state")
        self.guard()
        client.open()
        return connection_summary(state)

    def resume(self) -> str:
        client, running = self._client()
        self._start(client, running)
        self.guard()
        result = client.request("/api/control", {"action": "resume"})
        return connection_summary(result["state"])


class LauncherApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.root, self.dialogs, self.folder_dialog = root, messagebox, filedialog.askdirectory
        self.selection: WorkspaceSelection | None = None
        self.busy = False
        self.events: queue.Queue = queue.Queue()
        self.details = "아직 실행한 연결 작업이 없습니다."
        self.controls: list[Any] = []
        root.title("ShareBus")
        root.minsize(580, 470)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = lambda kind, value, trace: self.show_error(value)
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="함께 일할 작업실을 연결하세요", font=("", 18, "bold")).pack(anchor="w", pady=(0, 18))
        self.folder_name = tk.StringVar(value="1. 작업실 폴더를 선택하세요")
        ttk.Label(frame, textvariable=self.folder_name).pack(anchor="w")
        self.button(frame, "폴더 선택", self.choose_workspace).pack(anchor="w", pady=(6, 18))
        ttk.Label(frame, text="2. 연결할 앱을 선택하세요").pack(anchor="w")
        self.providers = {}
        for provider, found in available_apps().items():
            value = tk.BooleanVar(value=found)
            self.providers[provider] = value
            widget = ttk.Checkbutton(frame, text=APP_NAMES[provider] + ("" if found else " — 설치를 찾지 못함"), variable=value)
            widget.pack(anchor="w", pady=3)
            if found:
                self.controls.append(widget)
            else:
                widget.state(["disabled"])
        self.auto_enroll = tk.BooleanVar(value=True)
        automatic = ttk.Checkbutton(frame, text="다음 대화부터 자동 참여 준비", variable=self.auto_enroll)
        automatic.pack(anchor="w", pady=(12, 4))
        self.controls.append(automatic)
        ttk.Label(frame, text="연결하기는 선택 앱의 설정을 준비하고 작업실을 엽니다.\n기존 멈춤은 유지합니다. 앱을 다시 열거나 훅 신뢰를 확인해야 할 수 있습니다.\n앱의 참여·실제 응답은 작업실에서 확인하세요.", wraplength=530).pack(anchor="w", pady=(2, 14))
        self.button(frame, "3. 연결하기 / 다시 연결", lambda: self.act("connect")).pack(fill="x")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=8)
        self.button(row, "작업실 화면 열기", lambda: self.act("open")).pack(side="left")
        self.button(row, "전체 일시정지 재개", lambda: self.act("resume")).pack(side="left", padx=8)
        ttk.Button(row, text="고급 보기", command=self.show_details).pack(side="right")
        self.status = tk.StringVar(value="폴더와 앱을 선택한 뒤 연결하기를 눌러 주세요.")
        ttk.Label(frame, textvariable=self.status, wraplength=530).pack(anchor="w", pady=(10, 0))
        self.root.after(100, self.poll)

    def button(self, parent, text, command):
        from tkinter import ttk
        widget = ttk.Button(parent, text=text, command=command)
        self.controls.append(widget)
        return widget

    def choose_workspace(self) -> None:
        chosen = self.folder_dialog(parent=self.root, title="함께 일할 작업실 폴더 선택", mustexist=True)
        if not chosen:
            return
        try:
            self.selection = WorkspaceSelection.capture(chosen)
            self.folder_name.set("작업실: " + (self.selection.path.name or "선택한 폴더"))
        except LauncherError as error:
            self.show_error(error)

    def act(self, action: str) -> None:
        if self.busy:
            return
        selection = self.selection
        if selection is None:
            self.show_error(LauncherError("workspace"))
            return
        selected = tuple(provider for provider, value in self.providers.items() if value.get())
        auto_enroll = bool(self.auto_enroll.get())
        if action == "connect" and not selected:
            self.show_error(LauncherError("selection"))
            return
        messages = {
            "connect": "선택 앱의 연결 설정을 준비하고 작업실을 엽니다. 기존 멈춤은 유지합니다.",
            "open": "이 작업실의 화면을 엽니다. 연결이 끊겼다면 안전한 시작 절차로 다시 연결합니다.",
            "resume": "전체 일시정지만 해제합니다. 개별 앱의 멈춤·확인 필요 작업은 그대로 둡니다.",
        }
        if action not in messages:
            return
        names = " · ".join(APP_NAMES[provider] for provider in selected)
        if not self.dialogs.askokcancel("선택한 작업실 확인", f"작업실: {selection.path.name}\n{names}\n\n{messages[action]}", parent=self.root):
            return
        self.busy = True
        for widget in self.controls:
            widget.state(["disabled"])
        self.status.set("연결을 확인하고 있습니다. 처음 시작할 때는 잠시 걸릴 수 있습니다.")
        operations = WorkspaceActions(selection)

        def work():
            try:
                result = (operations.connect(selected, auto_enroll=auto_enroll) if action == "connect" else
                          operations.open_console() if action == "open" else operations.resume())
                self.events.put((result, None, operations.scope))
            except Exception as error:  # noqa: BLE001 - native errors must reach only the sanitized UI boundary.
                self.events.put((None, error, operations.scope))

        try:
            threading.Thread(target=work, name="sharebus-launcher-action", daemon=True).start()
        except Exception as error:  # noqa: BLE001 - restore the GUI and keep startup errors sanitized.
            self.busy = False
            for widget in self.controls:
                widget.state(["!disabled"])
            self.show_error(error, operations.scope)

    def poll(self) -> None:
        try:
            result, error, scope = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            for widget in self.controls:
                widget.state(["!disabled"])
            if error is not None:
                self.show_error(error, scope)
            else:
                self.status.set(result + " — 앱에서 연결 승인을 마친 뒤 작업실에서 일을 말해 주세요.")
                self.details = "작업 완료. 호스트 재시작·신뢰 확인이 필요할 수 있습니다.\n" + "\n".join(scope)
        self.root.after(100, self.poll)

    def show_error(self, error: BaseException, scope: tuple[str, ...] = ()) -> None:
        message = str(error) if isinstance(error, LauncherError) else MESSAGES["connection"]
        self.details = diagnostic_details(error, scope)
        self.status.set(message)
        self.dialogs.showerror("연결 확인", message, parent=self.root)

    def show_details(self) -> None:
        import tkinter as tk
        from tkinter import ttk
        window = tk.Toplevel(self.root)
        window.title("ShareBus 고급 보기")
        text = tk.Text(window, width=86, height=22, wrap="word")
        text.insert("1.0", self.details)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Button(window, text="닫기", command=window.destroy).pack(pady=(0, 12))

    def close(self) -> None:
        if self.busy:
            self.status.set("현재 연결 작업이 끝나면 창을 닫을 수 있습니다.")
            return
        self.root.destroy()


def run_gui() -> None:
    import tkinter as tk
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ShareBus 클릭 연결: 1. 폴더 선택  2. 앱 선택  3. 연결하기",
                                     epilog="GUI/Tk가 없는 환경에서도 --help를 볼 수 있습니다. 도움말은 설치·서비스·앱·모델을 실행하지 않습니다.")
    parser.parse_args(argv)
    try:
        run_gui()
    except Exception:  # noqa: BLE001 - pythonw must never show raw startup exception values.
        if sys.stderr is not None:
            print("ShareBus 창을 열 수 없습니다. 화면과 Python Tk 지원을 확인해 주세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
