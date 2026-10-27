"""Prepare or explicitly apply additive Sharebus MCP, skills, and project hooks."""

from __future__ import annotations

import argparse
import copy
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tomllib
import uuid

HERE = Path(__file__).resolve().parent
SUPPORTED_PROVIDERS = ("codex", "claude", "antigravity")


class ConfigurationConflict(ValueError):
    """A prepared configuration no longer matches its exact-byte baseline."""


def select_providers(providers: Iterable[str] | None) -> tuple[str, ...]:
    """Validate an explicit selection before inspecting or changing configuration."""
    if providers is None:
        return SUPPORTED_PROVIDERS
    if isinstance(providers, (str, bytes)):
        raise ValueError("Select one or more supported providers.")
    try:
        requested = tuple(providers)
    except TypeError:
        raise ValueError("Select one or more supported providers.") from None
    if not requested or any(type(item) is not str or item not in SUPPORTED_PROVIDERS for item in requested):
        raise ValueError("Select one or more supported providers.")
    return tuple(provider for provider in SUPPORTED_PROVIDERS if provider in requested)


def installed_resources_root() -> Path | None:
    """Recognize this helper's installed layout without trusting shell state."""
    source = Path(__file__).resolve()
    resources = source.parent.parent
    executable = Path(sys.executable)
    if (source.name != "install.py" or source.parent.name != "sharebus" or not executable.is_absolute()
            or not executable.resolve().is_relative_to(resources / "python")):
        return None
    try:
        metadata_path = resources / "PYTHON-RUNTIME.json"
        metadata_info = metadata_path.lstat()
        if (not stat.S_ISREG(metadata_info.st_mode) or metadata_path.is_symlink()
                or getattr(metadata_info, "st_file_attributes", 0) & 0x400 or metadata_info.st_size > 65536):
            raise ValueError
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        pattern = r"python\.exe" if sys.platform == "win32" else r"bin/python3\.(11|12|13|14)"
        if (not isinstance(metadata, dict) or metadata.get("schema") != "avalokita-python-runtime/1"
                or not isinstance(metadata.get("executable"), str)
                or not re.fullmatch(pattern, metadata["executable"])):
            raise ValueError
        declared = resources / "python" / metadata["executable"]
        expected = declared.with_name("pythonw.exe") if sys.platform == "win32" and executable.name.lower() == "pythonw.exe" else declared
        if executable.resolve() != expected.resolve():
            raise ValueError
        for file in (source, metadata_path, declared, executable):
            info = file.lstat()
            if (not stat.S_ISREG(info.st_mode) or file.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400
                    or not file.resolve().is_relative_to(resources)):
                raise ValueError
        return resources
    except (OSError, ValueError, TypeError):
        raise ValueError("Installed runtime metadata could not be validated.") from None


def _guard_workspace(workspace_guard: Callable[[], None] | None) -> None:
    if workspace_guard is not None:
        workspace_guard()


def private_backup_root(override: Path | None = None, *,
                        workspace_guard: Callable[[], None] | None = None) -> Path:
    selected = override if override is not None else os.environ.get("SHAREBUS_INSTALL_BACKUP_ROOT")
    if selected is not None:
        root = Path(selected)
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming") / "Avalokita/private/sharebus-install"
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/Avalokita/private/sharebus-install"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Avalokita/private/sharebus-install"
    if not root.is_absolute() or "\0" in str(root):
        raise ValueError("Installer backup root must be an absolute directory.")
    for part in (root, *root.parents):
        _guard_workspace(workspace_guard)
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Installer backup directories must be ordinary directories, not links.")
    _guard_workspace(workspace_guard)
    root = root.resolve()
    resources = os.environ.get("AVALOKITA_RESOURCES_ROOT")
    if resources:
        _guard_workspace(workspace_guard)
        if not Path(resources).is_absolute() or root.is_relative_to(Path(resources).resolve()):
            raise ValueError("Installer backups must be outside packaged resources.")
    installed = installed_resources_root()
    if installed is not None and root.is_relative_to(installed):
        raise ValueError("Installer backups must be outside packaged resources.")
    return root


def checksum(path: Path, *, workspace_guard: Callable[[], None] | None = None) -> str | None:
    _guard_workspace(workspace_guard)
    exists = path.exists()
    _guard_workspace(workspace_guard)
    return hashlib.sha256(path.read_bytes()).hexdigest() if exists else None


class PreparedChanges(dict[Path, str]):
    def __init__(self, paths: list[Path], *, workspace_guard: Callable[[], None] | None = None):
        super().__init__()
        self.workspace_guard = workspace_guard
        self.baseline = {path: checksum(path, workspace_guard=workspace_guard) for path in paths}


def json_update(path: Path, callback, *, workspace_guard: Callable[[], None] | None = None) -> str:
    _guard_workspace(workspace_guard)
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    _guard_workspace(workspace_guard)
    if not isinstance(value, dict):
        raise ValueError("Configuration root must be an object: " + str(path))
    callback(value)
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def server_entry(python: Path, provider: str) -> dict:
    flags = ["-B"] if os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or sys.dont_write_bytecode or installed_resources_root() is not None else []
    return {"command": str(python), "args": [*flags, str(HERE / "mcp_server.py"), "--provider", provider]}


def add_server(value: dict, entry: dict, replace: bool):
    servers = value.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be an object.")
    previous = servers.get("sharebus")
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("Existing sharebus MCP must be an object.")
    if previous is not None and any(previous.get(key) != entry[key] for key in entry) and not replace:
        raise ValueError("Existing sharebus MCP differs; use --replace-sharebus to replace only that entry.")
    servers["sharebus"] = {**(previous or {}), **entry}


def codex_update(text: str, entry: dict, replace: bool) -> str:
    before = tomllib.loads(text)
    previous = before.get("mcp_servers", {}).get("sharebus")
    if previous is not None and not isinstance(previous, dict):
        raise ValueError("Existing Codex sharebus MCP must be a table.")
    desired = {**(previous or {}), **entry}
    if previous == desired:
        return text
    if previous is not None:
        if not replace:
            raise ValueError("Existing Codex sharebus MCP differs; use --replace-sharebus.")
        lines = text.splitlines(keepends=True)
        kept, skipping, found = [], False, False
        for line in lines:
            if line.lstrip().startswith("["):
                skipping = bool(re.match(r"^\s*\[mcp_servers\.sharebus(?:\.|\])", line))
                found |= skipping
            if not skipping:
                kept.append(line)
        if not found:
            raise ValueError("Cannot safely edit this Sharebus TOML style; update the entry manually.")
        text = "".join(kept)
    def literal(value):
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, dict):
            return "{" + ", ".join(json.dumps(key) + " = " + literal(item) for key, item in value.items()) + "}"
        if isinstance(value, list):
            return "[" + ", ".join(literal(item) for item in value) + "]"
        if isinstance(value, float):
            return repr(value)
        return json.dumps(value, ensure_ascii=False)

    def table(parts, values):
        lines = ["", "[" + ".".join(parts) + "]"]
        lines += [json.dumps(key) + " = " + literal(value) for key, value in values.items() if not isinstance(value, dict)]
        for key, value in values.items():
            if isinstance(value, dict):
                lines += table([*parts, json.dumps(key)], value)
        return lines

    # Preserve user tool permissions, timeouts, environment and disabled flags.
    # Installing a local transport never grants blanket tool approval.
    block = table(["mcp_servers", "sharebus"], desired)
    result = text.rstrip() + "\n" + "\n".join(block) + "\n"
    after = tomllib.loads(result)
    before_other, after_other = copy.deepcopy(before), copy.deepcopy(after)
    before_other.setdefault("mcp_servers", {}).pop("sharebus", None)
    after_other.setdefault("mcp_servers", {}).pop("sharebus", None)
    if before_other != after_other or after["mcp_servers"]["sharebus"] != desired:
        raise ValueError("Codex configuration preservation check failed.")
    return result


def _hook_identity(item):
    command = item.get("commandWindows", item.get("command", ""))
    if not isinstance(command, str):
        return None
    try:
        argv = ([command, *item["args"]] if isinstance(item.get("args"), list)
                else [part.strip('\"\'') for part in shlex.split(command, posix=os.name != "nt")])
        script = next(path for path in (str(HERE / "auto_watch.py"), str(HERE / "hooks_bridge.py")) if path in argv)
        tail = argv[argv.index(script) + 1:]
        provider = tail[tail.index("--provider") + 1]
        workspace = tail[tail.index("--workspace") + 1] if "--workspace" in tail else None
        return script, provider, workspace, "--cancel" in tail, "--bootstrap" in tail
    except (StopIteration, ValueError, IndexError):
        return None


def add_hook(value: dict, handler: dict, legacy_command: str | None = None,
             events: tuple[str, ...] = ("UserPromptSubmit", "Stop")):

    own_identity = _hook_identity(handler)
    for event in events:
        handlers = value.setdefault("hooks", {}).setdefault(event, [])
        if not isinstance(handlers, list):
            raise ValueError(event + " hooks must be an array.")
        for group in handlers:
            hooks = group.get("hooks", [])
            for index, hook in enumerate(hooks):
                if (hook == handler or (hook.get("command") == handler.get("command") and hook.get("args") == handler.get("args"))
                        or (legacy_command is not None and hook.get("command") == legacy_command)
                        or (own_identity is not None and _hook_identity(hook) == own_identity)):
                    hooks[index] = handler
                    break
            else:
                continue
            break
        else:
            handlers.append({"hooks": [handler]})


def remove_codex_watch(value: dict, workspace: Path) -> None:
    for event, cancel in (("Stop", False), ("Interrupt", True)):
        groups = value.get("hooks", {}).get(event)
        if groups is None:
            continue
        if not isinstance(groups, list):
            raise ValueError(event + " hooks must be an array.")
        identity = (str(HERE / "auto_watch.py"), "codex", str(workspace), cancel, False)
        kept = []
        for group in groups:
            hooks = group.get("hooks", [])
            if not isinstance(hooks, list):
                raise ValueError(event + " hook handlers must be an array.")
            selected = [hook for hook in hooks if _hook_identity(hook) != identity]
            if selected == hooks:
                kept.append(group)
            elif selected:
                kept.append({**group, "hooks": selected})
        value["hooks"][event] = kept


def sidecar_update(value: dict, python: Path, registry: Path, probe_only: bool | None, replace: bool):
    environment = value.get("env", {})
    if not isinstance(environment, dict):
        raise ValueError("Sharebus Sidecar env must be an object.")
    selected = ("1" if probe_only else "0") if probe_only is not None else environment.get("SHAREBUS_SIDECAR_PROBE_ONLY", "1")
    flags = ["-B"] if os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or sys.dont_write_bytecode or installed_resources_root() is not None else []
    desired = {"command": str(python), "args": [*flags, str(HERE / "antigravity_bridge.py"), "sidecar", "--registry", str(registry)],
               "restart_policy": "on-failure", "display_name": "Sharebus",
               "description": "Workspace collaboration channel for explicitly attached native conversations.",
               "env": {**environment, "SHAREBUS_SIDECAR_PROBE_ONLY": selected}}
    if value and any(value.get(key) != desired[key] for key in ("command", "args")) and not replace:
        raise ValueError("Existing sharebus Sidecar differs; use --replace-sharebus.")
    value.update(desired)


def sidecar_enable(value: dict):
    sidecars = value.setdefault("sidecars", {})
    if not isinstance(sidecars, dict):
        raise ValueError("Sidecars settings must be an object.")
    own = sidecars.setdefault("sharebus", {})
    if not isinstance(own, dict):
        raise ValueError("Sharebus Sidecar settings must be an object.")
    own["enabled"] = True


def antigravity_hook_update(value: dict, python: Path, replace: bool):
    flags = " -B" if os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or sys.dont_write_bytecode or installed_resources_root() is not None else ""
    desired = {event: [{"type": "command", "command": f'"{python}"{flags} "{HERE / "hooks_bridge.py"}" --provider antigravity --event {event}',
                        "timeout": 10}] for event in ("SessionStart", "PreInvocation", "Stop")}
    old = value.get("sharebus-native-metadata")
    if old is not None and old != desired and not replace:
        raise ValueError("Existing Sharebus native metadata hook differs; use --replace-sharebus.")
    value["sharebus-native-metadata"] = desired


def prepare(home: Path, python: Path, *, replace: bool = False, desktop: bool = False,
            hooks_workspace: Path | None = None, sidecar_probe_only: bool | None = None,
            auto_hooks: bool = False, watch_timeout: float | None = None,
            codex_native_queue: bool | None = None,
            providers: Iterable[str] | None = None,
            workspace_guard: Callable[[], None] | None = None) -> PreparedChanges:
    selected = select_providers(providers)
    _guard_workspace(workspace_guard)
    if desktop and "claude" not in selected:
        raise ValueError("Claude Desktop installation requires selecting Claude.")
    packaged = os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or installed_resources_root() is not None
    if packaged and python.resolve() != Path(sys.executable).resolve():
        raise ValueError("Packaged installation must use the bundled Python interpreter.")
    python_flags = ["-B"] if packaged or sys.dont_write_bytecode else []
    if codex_native_queue is not None and (not auto_hooks or type(codex_native_queue) is not bool):
        raise ValueError("Codex native queue selection requires automatic hooks and a boolean value.")
    if auto_hooks:
        from auto_watch import DEFAULT_TIMEOUT, MAX_TIMEOUT
        if watch_timeout is not None and (type(watch_timeout) not in (int, float) or not math.isfinite(watch_timeout)
                or not 0 < watch_timeout <= MAX_TIMEOUT):
            raise ValueError("Native watch timeout must be finite, positive and at most seven days.")
    skills = [home / suffix for provider, suffix in (
        ("codex", ".agents/skills/sharebus/SKILL.md"), ("antigravity", ".gemini/config/skills/sharebus/SKILL.md"),
        ("claude", ".claude/skills/sharebus/SKILL.md"), ("claude", ".claude/commands/sharebus.md")) if provider in selected]
    if providers is None:
        skills.append(home / ".cursor/commands/sharebus.md")
    tracked = [home / suffix for provider, suffix in (
        ("claude", ".claude.json"), ("antigravity", ".gemini/config/mcp_config.json"),
        ("codex", ".codex/config.toml"), ("antigravity", ".gemini/config/sidecars/sharebus/sidecar.json"),
        ("antigravity", ".gemini/config/config.json"), ("antigravity", ".gemini/config/hooks.json")) if provider in selected]
    tracked += skills
    if auto_hooks:
        tracked += [home / suffix for provider, suffix in (("claude", ".claude/settings.json"),
                    ("codex", ".codex/hooks.json")) if provider in selected]
    if desktop:
        tracked.append(home / "AppData/Roaming/Claude/claude_desktop_config.json")
    if hooks_workspace:
        root = hooks_workspace.resolve()
        tracked += [root / suffix for provider, suffix in (("claude", ".claude/settings.local.json"),
                    ("codex", ".codex/hooks.json")) if provider in selected]
        tracked.append(root / ".sharebus/enabled")
        if auto_hooks:
            tracked.append(root / ".sharebus/hooks-install.json")
    changes = PreparedChanges(tracked, workspace_guard=workspace_guard)
    if auto_hooks:
        manifest = {}
        if hooks_workspace:
            manifest_path = hooks_workspace.resolve() / ".sharebus/hooks-install.json"
            _guard_workspace(workspace_guard)
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                _guard_workspace(workspace_guard)
                if (not isinstance(manifest, dict) or manifest.get("version") != 1
                        or manifest.get("workspace") != str(hooks_workspace.resolve())):
                    raise ValueError("Unknown native hooks installation manifest; preserve it for review.")
        watch_timeout = manifest.get("watch_timeout_seconds", DEFAULT_TIMEOUT) if watch_timeout is None else watch_timeout
        codex_native_queue = manifest.get("codex_native_queue", False) if codex_native_queue is None else codex_native_queue
        if type(codex_native_queue) is not bool:
            raise ValueError("Stored Codex native queue selection must be a boolean.")
        if (type(watch_timeout) not in (int, float) or not math.isfinite(watch_timeout)
                or not 0 < watch_timeout <= MAX_TIMEOUT):
            raise ValueError("Native watch timeout must be finite, positive and at most seven days.")
        if hooks_workspace:
            changes[manifest_path] = json.dumps({**manifest, "version": 1, "workspace": str(hooks_workspace.resolve()),
                                                "watch_timeout_seconds": watch_timeout,
                                                "codex_native_queue": codex_native_queue}, indent=2) + "\n"
    for path, provider in ((home / ".claude.json", "claude"),
                           (home / ".gemini/config/mcp_config.json", "antigravity")):
        if provider not in selected:
            continue
        entry = server_entry(python, provider)
        changes[path] = json_update(path, lambda value, entry=entry: add_server(value, entry, replace), workspace_guard=workspace_guard)
    if desktop:
        path = home / "AppData/Roaming/Claude/claude_desktop_config.json"
        changes[path] = json_update(path, lambda value: add_server(value, server_entry(python, "claude"), replace), workspace_guard=workspace_guard)
    if "codex" in selected:
        path = home / ".codex/config.toml"
        _guard_workspace(workspace_guard)
        changes[path] = codex_update(path.read_text(encoding="utf-8-sig") if path.exists() else "", server_entry(python, "codex"), replace)
        _guard_workspace(workspace_guard)
    if "antigravity" in selected:
        path = home / ".gemini/config/sidecars/sharebus/sidecar.json"
        registry = path.parent / "workspaces"
        changes[path] = json_update(path, lambda value: sidecar_update(value, python, registry, sidecar_probe_only, replace), workspace_guard=workspace_guard)
        path = home / ".gemini/config/config.json"
        changes[path] = json_update(path, sidecar_enable, workspace_guard=workspace_guard)
        path = home / ".gemini/config/hooks.json"
        changes[path] = json_update(path, lambda value: antigravity_hook_update(value, python, replace), workspace_guard=workspace_guard)
    template = (HERE / "commands/sharebus/SKILL.md").read_text(encoding="utf-8")
    skill = (template.replace("@PYTHON@", str(python)).replace("@SHAREBUS@", str(HERE / "sharebus.py"))
             .replace("@INSTALLER@", str(HERE / "install.py")).replace("@AUTO_ENROLL@", str(HERE / "auto_enroll.py")))
    if python_flags:
        skill = skill.replace(f"& '{python}' '", f"& '{python}' -B '").replace(f"{python} {HERE}", f"{python} -B {HERE}")
    for path in skills:
        _guard_workspace(workspace_guard)
        if path.exists() and path.read_text(encoding="utf-8") != skill and not replace:
            raise ValueError("Existing sharebus command differs; use --replace-sharebus: " + str(path))
        changes[path] = skill
    if hooks_workspace:
        root = hooks_workspace.resolve()
        for provider, path in (("claude", root / ".claude/settings.local.json"),
                               ("codex", root / ".codex/hooks.json")):
            if provider not in selected:
                continue
            legacy = f'"{python}" "{HERE / "hooks_bridge.py"}" --provider {provider} --workspace "{root}"'
            command = f'"{python}"' + (" -B" if python_flags else "") + f' "{HERE / "hooks_bridge.py"}" --provider {provider} --workspace "{root}"'
            if provider == "claude":
                handler = {"type": "command", "command": str(python),
                           "args": [*python_flags, str(HERE / "hooks_bridge.py"), "--provider", provider, "--workspace", str(root)],
                           "timeout": 10, "onFailure": "continue"}
            else:
                # Codex documents commandWindows rather than Claude's exec args.
                handler = {"type": "command", "command": command, "commandWindows": command, "timeout": 10}
            changes[path] = json_update(path, lambda value, handler=handler, legacy=legacy: add_hook(value, handler, legacy), workspace_guard=workspace_guard)
        changes[root / ".sharebus/enabled"] = "Sharebus native lifecycle context and pending-result correlation enabled.\n"
    if auto_hooks:
        assert watch_timeout is not None
        node = shutil.which("node")
        bootstrap = ([node, str(HERE / "commands/sharebus/session_hook_forwarder.cjs")] if node else [])
        for provider, path in (("claude", home / ".claude/settings.json"), ("codex", home / ".codex/hooks.json")):
            if provider not in selected:
                continue
            argv = [*bootstrap, str(python), *python_flags, str(HERE / "hooks_bridge.py"), "--provider", provider, "--bootstrap"]
            handler = _handler(provider, argv, timeout=10)
            _guard_workspace(workspace_guard)
            base = changes.get(path, path.read_text(encoding="utf-8-sig") if path.exists() else "{}")
            _guard_workspace(workspace_guard)
            value = json.loads(base)
            add_hook(value, handler, events=("SessionStart", "UserPromptSubmit"))
            if provider == "codex" and codex_native_queue and hooks_workspace:
                remove_codex_watch(value, hooks_workspace.resolve())
            changes[path] = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        if hooks_workspace:
            root = hooks_workspace.resolve()
            for provider, path in (("claude", root / ".claude/settings.local.json"), ("codex", root / ".codex/hooks.json")):
                if provider not in selected:
                    continue
                value = json.loads(changes[path])
                if provider == "codex" and codex_native_queue:
                    remove_codex_watch(value, root)
                    changes[path] = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
                    continue
                argv = [str(python), *python_flags, str(HERE / "auto_watch.py"), "--provider", provider,
                        "--workspace", str(root), "--timeout", str(watch_timeout)]
                watcher = _handler(provider, argv, timeout=math.ceil(watch_timeout + 15))
                if provider == "claude":
                    watcher["asyncRewake"] = True
                add_hook(value, watcher, events=("Stop",))
                cancel = _handler(provider, [str(python), *python_flags, str(HERE / "auto_watch.py"), "--provider", provider,
                                            "--workspace", str(root), "--cancel"], timeout=3 if provider == "codex" else 5)
                add_hook(value, cancel, events=("Interrupt",) if provider == "codex" else ("SessionEnd", "StopFailure"))
                changes[path] = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            from policy import install_policy
            for item in install_policy(root, apply_changes=False)["files"]:
                if "content" in item:
                    path = Path(item["path"])
                    changes.baseline[path] = item["current_hash"]
                    changes[path] = item["content"]
    _guard_workspace(workspace_guard)
    return changes


def _handler(provider: str, argv: list[str], *, timeout: int) -> dict:
    if provider == "claude":
        return {"type": "command", "command": argv[0], "args": argv[1:], "timeout": timeout, "onFailure": "continue"}
    windows = subprocess.list2cmdline(argv)
    return {"type": "command", "command": windows if os.name == "nt" else shlex.join(argv),
            "commandWindows": windows, "timeout": timeout}


def install_auto(workspace: Path, *, home: Path | None = None, python: Path | None = None,
                 apply_changes: bool = False, replace: bool = False, watch_timeout: float | None = None,
                 codex_native_queue: bool | None = None,
                 providers: Iterable[str] | None = None) -> dict:
    """Install native context and selected wake configuration, without sending work."""
    selected = select_providers(providers)
    root = Path(workspace).resolve(strict=True)
    executable = (python or Path(sys.executable)).resolve(strict=True)
    changes = prepare((home or Path.home()).resolve(), executable, replace=replace,
                      hooks_workspace=root, auto_hooks=True, watch_timeout=watch_timeout,
                      codex_native_queue=codex_native_queue,
                      providers=None if providers is None else selected)
    settings = json.loads(changes[root / ".sharebus/hooks-install.json"])
    timeout = settings["watch_timeout_seconds"]
    if apply_changes:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        files = apply(changes, private_backup_root() / stamp)
    else:
        files = [{"path": str(path), "would_change": not path.exists() or path.read_bytes() != content.encode("utf-8")}
                 for path, content in changes.items()]
    return {"applied": apply_changes, "files": files, "host_reload_required": True,
            "codex_hook_trust_review_required": "codex" in selected, "watch_timeout_seconds": timeout,
            "codex_native_queue": settings["codex_native_queue"],
            "codex_delivery": "native_queue" if settings["codex_native_queue"] else "stop_hook",
            "idle_attachment": "bounded_stop_window", "native_messages_sent": 0, "managed_workers_started": 0}


def apply(changes: dict[Path, str], backup_root: Path, *,
          workspace_guard: Callable[[], None] | None = None) -> list[dict]:
    """Plan first; only then atomically replace each additive configuration."""
    guard_failure: Exception | None = None

    def guard() -> None:
        nonlocal guard_failure
        if guard_failure is not None:
            raise guard_failure
        try:
            if isinstance(changes, PreparedChanges):
                _guard_workspace(changes.workspace_guard)
            if not isinstance(changes, PreparedChanges) or workspace_guard is not changes.workspace_guard:
                _guard_workspace(workspace_guard)
        except Exception as error:
            guard_failure = error
            raise

    backup_root = private_backup_root(backup_root, workspace_guard=guard)
    if not isinstance(changes, PreparedChanges):
        raise ValueError("Sharebus apply requires a prepared configuration baseline.")
    if any(checksum(path, workspace_guard=guard) != changes.baseline[path] for path in changes):
        raise ConfigurationConflict("Configuration changed after preparation; prepare again before applying.")
    receipts = []
    for path, content in changes.items():
        raw = content.encode("utf-8")
        guard()
        exists = path.exists()
        guard()
        if exists and path.read_bytes() == raw:
            receipts.append({"path": str(path), "changed": False})
            continue
        guard()
        if path.exists():
            guard()
            backup_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            guard()
            backup_root.chmod(0o700)
            digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
            backup = backup_root / (digest + "-" + path.name + ".bak")
            guard()
            shutil.copy2(path, backup)
            guard()
            backup.chmod(0o600)
        guard()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".sharebus-install-" + uuid.uuid4().hex + ".tmp")
        try:
            guard()
            with temp.open("xb") as stream:
                guard()
                stream.write(raw)
            if checksum(path, workspace_guard=guard) != changes.baseline[path]:
                raise ConfigurationConflict("Configuration changed during installation; prepare remaining changes again.")
            guard()
            os.replace(temp, path)
        finally:
            guard()
            if temp.exists():
                guard()
                temp.unlink()
        receipts.append({"path": str(path), "changed": True})
    return receipts


def main():
    parser = argparse.ArgumentParser(description="Sharebus installer; default is a read-only preparation audit")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-sharebus", action="store_true")
    parser.add_argument("--desktop", action="store_true", help="Also register Claude Desktop")
    parser.add_argument("--providers", nargs="+", choices=SUPPORTED_PROVIDERS,
                        help="Install only these providers; omitted preserves the legacy all-app installation")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--backup-root", type=Path, help="Absolute private backup directory; defaults to platform user data")
    parser.add_argument("--hooks-workspace", type=Path, help="Opt into native lifecycle context and exact result correlation in this workspace")
    parser.add_argument("--auto", action="store_true", help="Install global bootstrap and this workspace's model-free native Stop watcher")
    parser.add_argument("--watch-timeout", type=float, help="Stop watcher lifetime in seconds (default one day, maximum seven days)")
    queue = parser.add_mutually_exclusive_group()
    queue.add_argument("--codex-native-queue", dest="codex_native_queue", action="store_const", const=True, default=None,
                       help="Use the verified Codex native queue; omit this workspace's Codex Stop watcher")
    queue.add_argument("--codex-stop-hook", dest="codex_native_queue", action="store_const", const=False,
                       help="Use this workspace's bounded Codex Stop watcher fallback")
    sidecar = parser.add_mutually_exclusive_group()
    sidecar.add_argument("--sidecar-probe-only", dest="sidecar_probe_only", action="store_const", const=True, default=None)
    sidecar.add_argument("--enable-sidecar-delivery", dest="sidecar_probe_only", action="store_const", const=False)
    args = parser.parse_args()
    if args.auto and args.hooks_workspace is None:
        parser.error("Automatic hook installation requires --hooks-workspace.")
    if args.watch_timeout is not None and not args.auto:
        parser.error("--watch-timeout requires --auto.")
    if args.codex_native_queue is not None and not args.auto:
        parser.error("Codex delivery selection requires --auto.")
    try:
        python = args.python.resolve()
        if not python.is_file():
            raise ValueError("Python executable does not exist.")
        backup_root = private_backup_root(args.backup_root)
        changes = prepare(args.home.resolve(), python, replace=args.replace_sharebus,
                          desktop=args.desktop, hooks_workspace=args.hooks_workspace,
                          sidecar_probe_only=args.sidecar_probe_only, auto_hooks=args.auto,
                          watch_timeout=args.watch_timeout, codex_native_queue=args.codex_native_queue,
                          providers=args.providers)
        if args.apply:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = backup_root / stamp
            receipt = apply(changes, backup)
        else:
            receipt = [{"path": str(path), "would_change": not path.exists() or path.read_bytes() != text.encode("utf-8")}
                       for path, text in changes.items()]
        print(json.dumps({"applied": args.apply, "files": receipt,
                          "host_reload_required": True,
                          "codex_hook_trust_review_required": bool(args.hooks_workspace) and "codex" in select_providers(args.providers)}, indent=2))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        print("Sharebus installation preparation failed; no configuration contents were printed.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
