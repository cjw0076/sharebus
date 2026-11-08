"""Bounded, read-only native inventory and selected human-history features.

The adapters name their supported on-disk layouts. Neither a configured tool
nor a running process establishes usable capability. History is weak usage
evidence, never an instruction, success receipt, or current human permission.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import tomllib

from inventory_vault import InventoryError, PrivateVault, _ordinary_directory, _regular_file, opaque


SOURCE_KEYS = ("device", "codex_config", "claude_config", "antigravity_config",
               "workspace_config", "codex_history", "claude_history", "antigravity_history")
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_METADATA_ENTRIES = 4096
MAX_HISTORY_FILES = 256
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}\Z")
_SENSITIVE = re.compile(r"https?://|[/\\]|\b(?:sk-|gh[pousr]_|github_pat_)|"
                        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:bearer|password|secret|token)\b", re.I)
_PURPOSES = {
    "ideation": r"\b(?:idea|ideation|brainstorm)\b|아이디어|기획",
    "research": r"\b(?:research|investigate|reference)\b|조사|리서치|레퍼런스",
    "frontend": r"\b(?:frontend|react|css|html|layout)\b|프론트|레이아웃",
    "design": r"\b(?:design|figma|ux|ui)\b|디자인|시안",
    "backend": r"\b(?:backend|database|sql|server)\b|백엔드|데이터베이스",
    "infra": r"\b(?:infra|deploy|docker|ci/cd|packaging)\b|배포|인프라|패키징",
    "visual": r"\b(?:image|visual|animation|shader|three\.js)\b|이미지|애니메이션",
    "computer_use": r"\b(?:computer use|playwright|browser automation)\b|브라우저.*조작",
    "qa": r"\b(?:test|qa|verify|review)\b|테스트|검증|리뷰",
    "repair": r"\b(?:repair|fix|debug)\b|오류|고쳐|수리",
}


def _safe_label(value) -> str | None:
    if not isinstance(value, str) or not _LABEL.fullmatch(value) or _SENSITIVE.search(value):
        return None
    return value


def _timestamp(value) -> float:
    if value is None:
        # Missing time has no fabricated recency. Explicit invalid time is rejected.
        return 0.0
    try:
        if type(value) in (int, float):
            stamp = float(value)
        elif isinstance(value, str) and len(value) <= 40:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("ambiguous timestamp")
            stamp = parsed.timestamp()
        else:
            raise ValueError("invalid timestamp")
        if not math.isfinite(stamp) or not 0 <= stamp <= time.time() + 300:
            raise ValueError("invalid timestamp")
        return stamp
    except (ValueError, OverflowError, OSError):
        raise InventoryError("history_timestamp") from None


def _local_path(path: Path) -> None:
    """Reject UNC/mapped-network sources before resolving or opening them."""
    raw = str(path)
    if raw.lower().startswith(("\\\\?\\unc\\", "\\??\\unc\\")):
        raise InventoryError("remote_source")
    if raw.startswith("\\\\") and not raw.startswith("\\\\?\\"):
        raise InventoryError("remote_source")
    if raw.startswith("//"):
        raise InventoryError("remote_source")
    if os.name == "nt":
        import ctypes
        local = raw[4:] if raw.startswith("\\\\?\\") else raw
        if ctypes.windll.kernel32.GetDriveTypeW(str(Path(local).anchor)) == 4:
            raise InventoryError("remote_source")


class InventoryCollector:
    def __init__(self, vault: PrivateVault, *, home: Path | None = None, device_probe=None):
        self.vault = vault
        self.home = Path(home).absolute() if home is not None else vault.home
        _ordinary_directory(self.home)
        self._fixture_home = home is not None or vault.home_explicit
        self._device_probe = device_probe
        self._closed = threading.Event()
        self._roots: dict[str, tuple[str, int, int, int, int]] = {}
        self._known_evidence: set[str] | None = None

    def close(self) -> None:
        """Cooperative cancellation; safe while the worker owns a scan/vault."""
        self._closed.set()

    def _root(self, path: Path, *, allow_alias=False) -> Path:
        """Only explicit native-home/history roots may be a local directory alias."""
        _local_path(path)
        _ordinary_directory(path.parent)
        lexical = path.lstat()
        linked = path.is_symlink() or bool(getattr(lexical, "st_file_attributes", 0) & 0x400)
        if linked:
            if not allow_alias:
                raise InventoryError("linked_directory")
            target = Path(os.readlink(path))
            target = target if target.is_absolute() else path.parent / target
            _local_path(target)
            # Do not resolve a chain that can conceal another network/reparse root.
            _ordinary_directory(target)
        else:
            _ordinary_directory(path)
        resolved = path.resolve(strict=True)
        _local_path(resolved)
        _ordinary_directory(resolved)
        info = resolved.stat()
        identity = (str(resolved), info.st_dev, info.st_ino, lexical.st_dev, lexical.st_ino)
        key = str(path.absolute())
        if key in self._roots and self._roots[key] != identity:
            raise InventoryError("source_changed")
        self._roots[key] = identity
        return resolved

    def _native_home(self, provider: str) -> Path:
        variable, fallback = {"codex": ("CODEX_HOME", ".codex"),
                              "claude": ("CLAUDE_CONFIG_DIR", ".claude")}[provider]
        configured = None if self._fixture_home else os.environ.get(variable)
        return Path(configured).absolute() if configured else self.home / fallback

    @staticmethod
    def _read_config(path: Path) -> tuple[dict, str]:
        before = _regular_file(path)
        if before.st_size > MAX_CONFIG_BYTES:
            raise InventoryError("source_too_large")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        after = _regular_file(path)
        fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if fingerprint(before) != fingerprint(opened) or fingerprint(before) != fingerprint(after):
            raise InventoryError("source_changed")
        if len(raw) > MAX_CONFIG_BYTES:
            raise InventoryError("source_too_large")
        try:
            data = tomllib.loads(raw.decode("utf-8-sig")) if path.suffix == ".toml" else json.loads(raw)
        except (UnicodeError, ValueError, RecursionError):
            raise InventoryError("source_format") from None
        if not isinstance(data, dict):
            raise InventoryError("source_format")
        return data, opaque(raw)

    def _observation(self, source: str, provider: str, kind: str, label: str,
                     status: str, digest: str, *, reference: str | None = None,
                     observed_at: float | None = None) -> dict:
        now = time.time() if observed_at is None else observed_at
        source_ref = reference or opaque(source)
        return {"id": opaque(source_ref + "\0" + provider + "\0" + kind + "\0" + label),
                "kind": kind, "provider": provider, "label": label,
                "device_key": self.vault.device_key, "workspace_key": self.vault.workspace_key,
                "status": status, "observed_at": now, "expires_at": now + 24 * 3600,
                "verified": False,
                "provenance": {"source_kind": "local_metadata", "source_ref": source_ref, "source_hash": digest}}

    def _save(self, source: str, rows: list[dict], status: str, *, records=0, complete=True, observed_at=None) -> dict:
        self.vault.put_inventory(source, rows, status=status, observed_at=time.time() if observed_at is None else observed_at)
        return {"source": source, "status": status, "records": records, "complete": complete}

    def _device(self) -> dict:
        try:
            if self._device_probe is not None:
                scanned = self._device_probe()
            else:
                from device_discovery import scan_device
                # Reuse process classification without launching native agents/MCP.
                def resolver(provider):
                    return shutil.which({"antigravity": "agy"}.get(provider, provider))
                scanned = scan_device(self.vault.workspace, resolver=resolver,
                    session_reader=lambda *_: ([], {"state": "unavailable", "observed_at": None, "error_code": None}))
            metadata: dict = {}
            if isinstance(scanned, tuple) and len(scanned) == 2:
                scanned, metadata = scanned
            if not isinstance(scanned, list) or len(scanned) > 128:
                raise InventoryError("device_format")
            if not isinstance(metadata, dict):
                raise InventoryError("device_format")
            now = time.time()
            observed = metadata.get("last_success_at", metadata.get("observed_at"))
            if observed is None:
                observed = metadata.get("observed_at")
            if observed is None:
                observed = now
            if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= observed <= now + 300:
                raise InventoryError("device_timestamp")
            stale = metadata.get("stale") is True or metadata.get("state") == "stale" or observed < now - 60
            rows = []
            for item in scanned:
                if not isinstance(item, dict) or item.get("provider") not in {"claude", "codex", "antigravity", "chatgpt", "gemini", "grok", "cursor", "hermes"}:
                    continue
                provider = item["provider"]
                if item.get("running") is True:
                    state = "observed"
                elif item.get("installed") is True or item.get("cli_available") is True:
                    state = "installed"
                else:
                    continue
                state = "stale" if stale else state
                projection = {"provider": provider, "status": state}
                rows.append(self._observation("device", provider, "agent_app", provider, state,
                                              opaque(json.dumps(projection, sort_keys=True)), observed_at=observed))
            source_state = metadata.get("state")
            if source_state in {"failed", "error", "unavailable"}:
                status = "unavailable"
            elif stale:
                status = "stale"
            elif source_state in {"pending", "scanning", "stale"}:
                status = "partial" if rows else "unavailable"
            else:
                status = "ready" if rows else "empty"
            return self._save("device", rows, status, records=len(rows), complete=status in {"ready", "empty"}, observed_at=observed)
        except (OSError, ValueError, RuntimeError, ImportError):
            return self._save("device", [], "unavailable", complete=False)

    def _configs(self, source: str, candidates: list[tuple[str, Path]]) -> dict:
        rows: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        failed, missing = False, True
        for provider, candidate in candidates:
            if self._closed.is_set():
                return self._save(source, rows, "partial", records=len(rows), complete=False)
            try:
                # A supported native-home junction is resolved, not a config-file link.
                path = self._root(candidate.parent, allow_alias=source != "workspace_config") / candidate.name
                data, digest = self._read_config(path)
                missing = False
                reference = opaque(os.path.normcase(str(path)))
                rows.append(self._observation(source, provider, "configuration", provider + "_configuration",
                                              "configured", digest, reference=reference))
                for field, kind in (("mcpServers", "mcp_server"), ("mcp_servers", "mcp_server"),
                                    ("enabledPlugins", "plugin"), ("plugins", "plugin"),
                                    ("connectors", "connector"), ("apps", "connector"), ("devices", "device_candidate")):
                    table = data.get(field)
                    if not isinstance(table, dict):
                        continue
                    for name, settings in list(table.items())[:256]:
                        label = _safe_label(name)
                        if label is None or (kind == "plugin" and settings is False):
                            continue
                        identity = (reference, kind, label)
                        if identity in seen:
                            continue
                        seen.add(identity)
                        # No command/args/env/URL/headers/authentication values survive.
                        state = "unavailable" if isinstance(settings, dict) and settings.get("disabled") is True else "configured"
                        rows.append(self._observation(source, provider, kind, label, state, digest, reference=reference))
            except FileNotFoundError:
                continue
            except (OSError, ValueError, RuntimeError):
                failed = True
        status = "partial" if failed and rows else "error" if failed else "missing" if missing else "ready"
        return self._save(source, rows, status, records=len(rows), complete=not failed)

    def _history_files(self, root: Path, depth: int) -> tuple[list[Path], bool]:
        """Bound metadata work as well as payload work; disclose finite coverage."""
        found, entries, limited = [], 0, False
        pending = [(root, 0)]
        while pending and not self._closed.is_set():
            directory, level = pending.pop()
            _ordinary_directory(directory)
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > MAX_METADATA_ENTRIES:
                        limited = True
                        break
                    if child.is_symlink():
                        limited = True
                        continue
                    # DirEntry.stat reports zero inode/link counts on Windows.
                    # Path.lstat supplies the real file identity used by guards.
                    info = Path(child.path).lstat()
                    if getattr(info, "st_file_attributes", 0) & 0x400:
                        limited = True
                        continue
                    if stat.S_ISDIR(info.st_mode) and level < depth:
                        pending.append((Path(child.path), level + 1))
                    elif stat.S_ISREG(info.st_mode) and child.name.endswith(".jsonl"):
                        if info.st_nlink == 1:
                            found.append(Path(child.path))
                        else:
                            limited = True
            if entries > MAX_METADATA_ENTRIES:
                break
        return sorted(found), limited or self._closed.is_set()

    def _matches_workspace(self, value) -> bool:
        # Compare only. Never probe a historical path or treat it as a new root.
        return isinstance(value, str) and os.path.normcase(os.path.normpath(value)) == os.path.normcase(str(self.vault.workspace))

    @staticmethod
    def _human_text(provider: str, row: dict) -> str | None:
        content = None
        if provider == "codex":
            payload = row.get("payload")
            if not isinstance(payload, dict):
                return None
            if row.get("type") == "event_msg" and payload.get("type") == "user_message":
                content = payload.get("message")
            elif (row.get("type") == "response_item" and payload.get("type") == "message"
                  and payload.get("role") == "user"):
                content = payload.get("content")
        elif (row.get("type") == "user" and row.get("isMeta") is not True and row.get("isSidechain") is not True
              and isinstance(row.get("message"), dict) and row["message"].get("role") == "user"):
            content = row["message"].get("content")
        if isinstance(content, list):
            # A tool_result supplied under role=user is not selected human input.
            if any(not isinstance(block, dict) or block.get("type") not in {"text", "input_text"} for block in content):
                return None
            content = "\n".join(block.get("text", "") for block in content if isinstance(block.get("text"), str))
        if not isinstance(content, str) or not 1 <= len(content) <= 12000:
            return None
        if any(marker.casefold() in content.casefold() for marker in ("[sharebus", "native completion binding",
                                                "<codex_internal_context", "<external_codex", "You are a participant in",
                                                "# AGENTS.md instructions", "<environment_context>",
                                                "<skills_instructions>", "<system-reminder>", "<permissions instructions>")):
            return None
        return content

    def _evidence(self, provider: str, source_ref: str, row: dict, workspace_match: bool,
                  start: int, end: int, previous: dict | None) -> tuple[int, dict | None]:
        text = self._human_text(provider, row)
        if text is None:
            return 0, previous
        observed_at = _timestamp(row.get("timestamp"))
        text_hash = opaque(text)
        wrapper = row["type"]
        if (provider == "codex" and isinstance(previous, dict) and not previous.get("mirror_used")
                and previous.get("text_hash") == text_hash and previous.get("end") == start
                and {previous.get("wrapper"), wrapper} == {"event_msg", "response_item"}
                and (not observed_at or not previous.get("observed_at")
                     or abs(observed_at - previous["observed_at"]) <= 2)):
            return 0, {**previous, "end": end, "mirror_used": True}
        from preference_learning import featureize, FEATURE_SCHEMA_VERSION
        known = self._known_evidence
        if known is None:
            raise InventoryError("collector_uninitialized")
        # Explicit taxonomy of observed use, not a learned semantic task parser.
        purposes = [name for name, expression in _PURPOSES.items() if re.search(expression, text, re.I)] or ["general"]
        # A stable occurrence and timestamp preserve distinct repeated requests;
        # neither a file locator nor an historical ID becomes native authority.
        event_ref = opaque(json.dumps([provider, source_ref, text_hash, observed_at, start]))
        added = 0
        for purpose in purposes:
            if self._closed.is_set():
                break
            identity = opaque(event_ref + "\0" + purpose + "\0" + str(FEATURE_SCHEMA_VERSION))
            if identity in known:
                continue
            # Other workspaces contribute generic purpose/style only, not their lexical content.
            selected = text if workspace_match else "observed human usage for " + purpose
            sample = {"id": identity, "source_kind": "observed_usage", "source_ref": source_ref,
                      "event_ref": event_ref, "observed_at": observed_at,
                      "scope": self.vault.workspace_key if workspace_match else "global_style",
                      "provider": provider, "purpose": purpose, "capability": None,
                      "features": featureize(selected, purpose), "target": 1, "weight": 0.1}
            added += self.vault.put_evidence(sample)
            known.add(identity)
        return added, {"text_hash": text_hash, "observed_at": observed_at, "wrapper": wrapper,
                       "end": end, "mirror_used": False}

    def _history(self, provider: str, budget: dict) -> dict:
        source = provider + "_history"
        scanned = added = 0
        limited = malformed = unsupported = False
        try:
            home = self._root(self._native_home(provider), allow_alias=True)
            root = self._root(home / ("sessions" if provider == "codex" else "projects"), allow_alias=True)
            files, limited = self._history_files(root, 3 if provider == "codex" else 1)
            page = self.vault.get_cursor(source + "_page") or {"offset": 0}
            position = page.get("offset", 0)
            if type(position) is not int or not 0 <= position < len(files):
                position = 0
            selected = files[position:position + MAX_HISTORY_FILES]
            next_position = position + len(selected)
            # Rotate bounded pages; a finite metadata ceiling remains explicit.
            limited = limited or len(files) > len(selected)
            digest = hashlib.sha256()
            visited = 0
            for path in selected:
                if self._closed.is_set() or budget["records"] <= 0 or budget["bytes"] <= 0:
                    limited = True
                    break
                before = _regular_file(path)
                source_ref = opaque(os.path.normcase(str(path)))
                cursor = self.vault.get_cursor(source_ref) or {}
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise InventoryError("source_changed")
                    prefix_size = min(before.st_size, 4096)
                    prefix = opaque(stream.read(prefix_size))
                    previous_prefix_size = cursor.get("prefix_size", 0)
                    stream.seek(0)
                    same_prefix = (previous_prefix_size <= before.st_size and
                                   opaque(stream.read(previous_prefix_size)) == cursor.get("prefix_hash"))
                    unchanged = (cursor.get("device") == before.st_dev and cursor.get("inode") == before.st_ino
                                 and 0 <= cursor.get("offset", -1) <= before.st_size and same_prefix)
                    offset = cursor.get("offset", 0) if unchanged else 0
                    match = cursor.get("workspace_match") is True if unchanged else False
                    discard = cursor.get("discard") is True if unchanged else False
                    file_malformed = cursor.get("malformed") is True if unchanged else False
                    file_unsupported = cursor.get("unsupported") is True if unchanged else False
                    previous = cursor.get("previous_human") if unchanged else None
                    stream.seek(offset)
                    while budget["records"] > 0 and budget["bytes"] > 0 and not self._closed.is_set():
                        start = stream.tell()
                        raw = stream.readline(min(MAX_LINE_BYTES + 1, budget["bytes"] + 1))
                        if not raw:
                            break
                        budget["bytes"] -= len(raw)
                        if discard:
                            offset = stream.tell()
                            discard = not raw.endswith(b"\n")
                            file_malformed = True
                            continue
                        if not raw.endswith(b"\n"):
                            if len(raw) > MAX_LINE_BYTES:
                                # Drop an over-size record in bounded chunks,
                                # including its tail on subsequent refreshes.
                                discard = True
                                offset = stream.tell()
                                file_malformed = True
                                continue
                            # Keep an incomplete/oversize record pending; never parse a fragment.
                            stream.seek(start)
                            limited = True
                            break
                        offset = stream.tell()
                        scanned += 1
                        budget["records"] -= 1
                        if len(raw) > MAX_LINE_BYTES:
                            file_malformed = True
                            continue
                        try:
                            row = json.loads(raw)
                        except (ValueError, UnicodeError, RecursionError):
                            file_malformed = True
                            continue
                        if not isinstance(row, dict):
                            file_malformed = True
                            continue
                        if provider == "codex" and row.get("type") == "session_meta":
                            payload = row.get("payload")
                            match = isinstance(payload, dict) and self._matches_workspace(payload.get("cwd"))
                        elif provider == "claude" and isinstance(row.get("cwd"), str):
                            match = self._matches_workspace(row["cwd"])
                        if row.get("type") not in ({"session_meta", "event_msg", "response_item", "turn_context"} if provider == "codex"
                                                   else {"user", "assistant", "progress", "system", "summary", "file-history-snapshot", "queue-operation"}):
                            file_unsupported = True
                            continue
                        try:
                            count, previous = self._evidence(provider, source_ref, row, match, start, offset, previous)
                            added += count
                        except InventoryError as error:
                            if error.code not in {"history_timestamp", "future_evidence"}:
                                raise
                            file_malformed = True
                        except (ValueError, UnicodeError, OverflowError):
                            # A malformed selected input never blocks valid later rows.
                            file_malformed = True
                    ended = offset >= before.st_size
                    malformed = malformed or file_malformed
                    unsupported = unsupported or file_unsupported
                after = _regular_file(path)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                    # Evidence is deduplicated; do not advance a cursor for a changing file.
                    limited = True
                    continue
                self.vault.set_cursor(source_ref, {"device": before.st_dev, "inode": before.st_ino,
                    "offset": offset, "size": before.st_size, "prefix_size": prefix_size,
                    "prefix_hash": prefix, "workspace_match": match, "discard": discard,
                    "malformed": file_malformed, "unsupported": file_unsupported, "previous_human": previous})
                digest.update(bytes.fromhex(prefix))
                if not ended:
                    limited = True
                    break
                visited += 1
            # Resume the interrupted page/file, not a later page that can starve it.
            next_position = position + visited if visited < len(selected) else next_position
            self.vault.set_cursor(source + "_page", {"offset": next_position if next_position < len(files) else 0})
            status = "partial" if limited or malformed else "unsupported" if unsupported else "ready" if files else "empty"
            rows = [self._observation(source, provider, "history_source", source, "observed", digest.hexdigest(),
                                     reference=opaque(str(root)))] if files else []
            result = self._save(source, rows, status, records=scanned, complete=not (limited or malformed or unsupported))
        except FileNotFoundError:
            result = self._save(source, [], "missing")
        except ImportError:
            result = self._save(source, [], "unavailable", records=scanned, complete=False)
        except (OSError, ValueError, RuntimeError):
            result = self._save(source, [], "error", records=scanned, complete=False)
        result["evidence_added"] = added
        return result

    def collect_once(self, *, max_records: int = 500) -> dict:
        if type(max_records) is not int or not 1 <= max_records <= 5000:
            raise InventoryError("invalid_batch_size")
        self.vault.guard_workspace()
        if self._closed.is_set():
            return {"state": "closed", "complete": False, "records": 0, "evidence_added": 0, "sources": []}
        self._known_evidence = {row["id"] for row in self.vault.preference_samples()}
        codex, claude = self._native_home("codex"), self._native_home("claude")
        sources = [self._device(),
            self._configs("codex_config", [("codex", codex / "config.toml")]),
            self._configs("claude_config", [("claude", self.home / ".claude.json"), ("claude", claude / "settings.json"), ("claude", claude / "settings.local.json")]),
            self._configs("antigravity_config", [("antigravity", self.home / ".gemini/config/mcp_config.json"), ("antigravity", self.home / ".gemini/config/config.json")]),
            self._configs("workspace_config", [("claude", self.vault.workspace / ".mcp.json"), ("antigravity", self.vault.workspace / ".agents/mcp_config.json")])]
        budget = {"records": max_records, "bytes": MAX_BATCH_BYTES}
        turn = self.vault.get_cursor("history_turn") or {"next": 0}
        providers = ("codex", "claude") if turn.get("next") == 0 else ("claude", "codex")
        sources.extend(self._history(provider, budget) for provider in providers)
        self.vault.set_cursor("history_turn", {"next": 1 if providers[0] == "codex" else 0})
        # No documented safe AGY human-history decoder is admitted in this version.
        sources.append(self._save("antigravity_history", [], "unsupported", complete=False))
        complete = all(source["complete"] for source in sources)
        return {"state": "ready" if complete else "partial", "complete": complete,
                "records": sum(source["records"] for source in sources),
                "evidence_added": sum(source.get("evidence_added", 0) for source in sources),
                "sources": sources, "revision": self.vault.summary()["revision"]}
