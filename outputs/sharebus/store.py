"""Durable events, work ownership, budgets, and selected artifact snapshots.

Capabilities enforce ownership through this API. They do not isolate processes
running as the same operating-system user from direct filesystem access.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

WAKE_KINDS = {"task", "question", "answer", "critique", "proposal", "message"}
KINDS = WAKE_KINDS | {"progress", "result", "artifact", "notice"}
PROVIDERS = {"codex", "claude", "antigravity", "chatgpt", "gemini", "cursor", "hermes", "other"}
MAX_BODY = 64 * 1024
MAX_ARTIFACT = 20 * 1024 * 1024


class BusError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class WorkspaceRetired(BusError):
    """Confirmed replacement of the originally captured workspace identity."""
    def __init__(self):
        super().__init__("Workspace identity changed; this runtime is retired", 403)


def _id() -> str:
    return str(uuid.uuid4())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class BusStore:
    def __init__(self, workspace: str | Path, *, timeout: float = 15,
                 workspace_guard: Callable[[], None] | None = None):
        self._workspace_guard = workspace_guard
        self.workspace_retired = False
        self._initialized = False
        self._database_initialized = False
        self.guard_workspace()
        self.workspace = self.checked_workspace(workspace)
        self.sqlite_timeout = timeout
        self.guard_workspace()
        if not self.workspace.is_dir():
            raise BusError("Workspace must be a directory")
        self.directory = self.workspace / ".sharebus"
        self.private = self.directory / "private"
        self.credentials = self.private / "credentials"
        self._manifest_lock = threading.RLock()
        for folder in (self.directory, self.private, self.credentials):
            self.guard_workspace()
            if folder.is_symlink():
                raise BusError("Sharebus directory cannot be a symlink or reparse point")
            self.guard_workspace()
            if self._reparse(folder):
                raise BusError("Sharebus directory cannot be a symlink or reparse point")
            self.guard_workspace()
            folder.mkdir(exist_ok=True)
        try:
            self.guard_workspace()
            os.chmod(self.private, 0o700)
        except OSError:
            pass
        private_ignore = self.private / ".gitignore"
        self.guard_workspace()
        if private_ignore.is_symlink():
            raise BusError("Private ignore policy cannot be a link")
        self.guard_workspace()
        if self._reparse(private_ignore):
            raise BusError("Private ignore policy cannot be a link")
        try:
            self.guard_workspace()
            with private_ignore.open("x", encoding="utf-8") as stream:
                self.guard_workspace()
                stream.write("*\n")
        except FileExistsError:
            pass
        self.db_path = self.private / "bus.sqlite3"
        self.guard_workspace()
        if self.db_path.is_symlink():
            raise BusError("Database cannot be a symlink or reparse point")
        self.guard_workspace()
        if self._reparse(self.db_path):
            raise BusError("Database cannot be a symlink or reparse point")
        self.guard_workspace()
        if self.db_path.exists():
            self.guard_workspace()
            if not self.db_path.is_file():
                raise BusError("Database cannot be a symlink or reparse point")
            self.guard_workspace()
            if self.db_path.stat().st_nlink != 1:
                raise BusError("Database cannot be a symlink or reparse point")
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,provider TEXT NOT NULL,label TEXT NOT NULL,
                    capabilities TEXT NOT NULL,mode TEXT NOT NULL,token_hash TEXT NOT NULL UNIQUE,role TEXT NOT NULL DEFAULT '',
                    native_session_id TEXT,native_address TEXT,state TEXT NOT NULL,pid INTEGER,generation INTEGER NOT NULL DEFAULT 0,channel_verified INTEGER NOT NULL DEFAULT 0,
                    active_root TEXT,error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS roots (
                    id TEXT PRIMARY KEY,human_event_id TEXT NOT NULL,budget INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,deadline REAL NOT NULL,created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,sender TEXT NOT NULL,recipient TEXT NOT NULL,
                    body TEXT NOT NULL,root_id TEXT,created_at REAL NOT NULL,metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,event_id TEXT NOT NULL,agent_id TEXT NOT NULL,
                    root_id TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,
                    started_at REAL,finished_at REAL,generation INTEGER,error TEXT,priority INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(event_id,agent_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(agent_id,state,created_at);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,owner TEXT NOT NULL,filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,
                    created_at REAL NOT NULL,root_id TEXT
                );
                CREATE TABLE IF NOT EXISTS recovery_codes (
                    code_hash TEXT PRIMARY KEY,agent_id TEXT NOT NULL,provider TEXT NOT NULL,
                    native_session_id TEXT,expires_at REAL NOT NULL,used_at REAL
                );
                CREATE TABLE IF NOT EXISTS auto_contexts (
                    context_id TEXT PRIMARY KEY,agent_id TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,native_session_id TEXT NOT NULL,
                    hook_source TEXT NOT NULL,native_hook_source TEXT,transport_binding TEXT,updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auto_proofs (
                    code_hash TEXT PRIMARY KEY,context_id TEXT NOT NULL,agent_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,provider TEXT NOT NULL,native_session_id TEXT NOT NULL,
                    hook_source TEXT NOT NULL,transport_binding TEXT,claim_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL,consumed_at REAL
                );
                CREATE TABLE IF NOT EXISTS auto_controls (agent_id TEXT PRIMARY KEY,epoch REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auto_observations (
                    agent_id TEXT PRIMARY KEY,state TEXT NOT NULL,note TEXT NOT NULL,updated_at REAL NOT NULL
                );
            """)
            if "role" not in {row[1] for row in db.execute("PRAGMA table_info(agents)")}:
                db.execute("ALTER TABLE agents ADD COLUMN role TEXT NOT NULL DEFAULT ''")
            if "channel_verified" not in {row[1] for row in db.execute("PRAGMA table_info(agents)")}:
                db.execute("ALTER TABLE agents ADD COLUMN channel_verified INTEGER NOT NULL DEFAULT 0")
            if "native_address" not in {row[1] for row in db.execute("PRAGMA table_info(agents)")}:
                db.execute("ALTER TABLE agents ADD COLUMN native_address TEXT")
            if "priority" not in {row[1] for row in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
            if "native_hook_source" not in {row[1] for row in db.execute("PRAGMA table_info(auto_contexts)")}:
                db.execute("ALTER TABLE auto_contexts ADD COLUMN native_hook_source TEXT")
            try:
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS agents_native_owner ON agents(provider,native_session_id) WHERE native_session_id IS NOT NULL")
            except sqlite3.IntegrityError:
                raise BusError("Existing duplicate native bindings require human review", 409) from None
            db.execute("INSERT OR IGNORE INTO config VALUES ('workspace_id',?)", (_id(),))
            db.execute("INSERT OR IGNORE INTO config VALUES ('paused','false')")
            db.execute("INSERT OR IGNORE INTO config VALUES ('auto_enabled','false')")
            db.execute("INSERT OR IGNORE INTO config VALUES ('auto_suspended','false')")
            db.execute("INSERT OR IGNORE INTO config VALUES ('auto_codex_delivery','')")
            db.execute("INSERT OR IGNORE INTO config VALUES ('auto_updated_at','0')")
            db.execute("INSERT OR IGNORE INTO config SELECT 'auto_epoch',value FROM config WHERE key='auto_updated_at'")
            self.workspace_id = db.execute("SELECT value FROM config WHERE key='workspace_id'").fetchone()[0]
        self._database_initialized = True
        token_path = self.private / "operator.token"
        self.guard_workspace()
        if token_path.is_symlink():
            raise BusError("Operator token cannot be a link")
        self.guard_workspace()
        if self._reparse(token_path):
            raise BusError("Operator token cannot be a link")
        try:
            self.guard_workspace()
            with token_path.open("x", encoding="utf-8") as stream:
                self.guard_workspace()
                stream.write(secrets.token_urlsafe(32))
            self.guard_workspace()
            os.chmod(token_path, 0o600)
        except FileExistsError:
            pass
        self.guard_workspace()
        self.operator_token = token_path.read_text(encoding="utf-8").strip()
        if len(self.operator_token) < 32:
            raise BusError("Invalid private operator token")
        self._initialized = True

    def _guard_workspace_identity(self) -> None:
        """Owned runtimes reuse their captured identity; standalone stores opt out."""
        if self.workspace_retired:
            raise WorkspaceRetired()
        if self._workspace_guard is not None:
            try:
                self._workspace_guard()
            except WorkspaceRetired:
                self.workspace_retired = True
                raise
            except BusError:
                raise
            except Exception:
                raise BusError("Workspace identity could not be checked", 503) from None

    def guard_workspace(self) -> None:
        self._guard_workspace_identity()
        if self._workspace_guard is not None and hasattr(self, "directory"):
            for path in (self.directory, self.private, self.credentials):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if not self._initialized:
                        continue
                    raise BusError("Workspace storage directory is unavailable", 409) from None
                except OSError:
                    raise BusError("Workspace storage could not be checked", 503) from None
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not stat.S_ISDIR(info.st_mode)):
                    raise BusError("Workspace storage must use real directories", 403)

    def _check_database_files(self) -> None:
        if self._workspace_guard is None:
            self.guard_workspace()
            return
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            optional = path != self.db_path
            for sample in range(3):
                # Root and storage parents are checked outside the optional-file
                # catch, including every bounded metadata-only resample.
                self.guard_workspace()
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if optional or not self._database_initialized:
                        break
                    raise BusError("Workspace database is unavailable", 409) from None
                except OSError:
                    raise BusError("Workspace database could not be checked", 503) from None
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not stat.S_ISREG(info.st_mode)):
                    raise BusError("Workspace database must use regular nonlinked files", 403)
                if info.st_nlink == 1:
                    break
                if os.name == "nt" and info.st_nlink == 0:
                    if not optional or sample == 2:
                        raise BusError("Workspace database metadata is unavailable", 503)
                    time.sleep(0.025)
                    continue
                raise BusError("Workspace database must use regular nonlinked files", 403)

    @staticmethod
    def _reparse(path: Path) -> bool:
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except FileNotFoundError:
            return False

    @classmethod
    def checked_workspace(cls, workspace: str | Path) -> Path:
        path = Path(workspace).absolute()
        cls.reject_linked_path(path)
        return path.resolve(strict=True)

    @classmethod
    def reject_linked_path(cls, path: Path) -> None:
        # Check before resolve(): resolving first erases a junction/symlink in
        # the workspace itself or any ancestor above it.
        for current in (*reversed(path.parents), path):
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise BusError("Sharebus paths cannot contain links or reparse points", 403)
            if current != path and not stat.S_ISDIR(info.st_mode):
                raise BusError("Sharebus path ancestors must be directories", 403)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self._check_database_files()
        owned_existing = self._workspace_guard is not None and self._database_initialized
        target = self.db_path.as_uri() + "?mode=rw" if owned_existing else self.db_path
        self._guard_workspace_identity()
        db = sqlite3.connect(target, uri=owned_existing, timeout=self.sqlite_timeout)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=" + str(max(1, int(self.sqlite_timeout * 1000))))
            with db:
                self._check_database_files()
                yield db
                self._check_database_files()
                self._guard_workspace_identity()
        finally:
            db.close()

    @staticmethod
    def _public_agent(row: sqlite3.Row) -> dict[str, Any]:
        result = {key: row[key] for key in ("id", "provider", "label", "role", "mode", "native_session_id", "native_address", "state", "pid", "generation", "active_root", "error", "updated_at")}
        result["capabilities"] = json.loads(row["capabilities"])
        result["channel_verified"] = bool(row["channel_verified"])
        result["folder"] = f".sharebus/{row['provider']}/{row['id']}"
        return result

    def agents(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [self._public_agent(row) for row in db.execute("SELECT * FROM agents ORDER BY created_at,id")]

    def agent(self, agent_id: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if row is None:
                raise BusError("Unknown agent", 404)
            return self._public_agent(row)

    def authenticate(self, token: str) -> str:
        self.guard_workspace()
        if not token:
            raise BusError("Authentication required", 401)
        if secrets.compare_digest(token, self.operator_token):
            return "user"
        with self.connection() as db:
            row = db.execute("SELECT id FROM agents WHERE token_hash=?", (_hash(token),)).fetchone()
        if row is None:
            raise BusError("Invalid capability", 401)
        return row[0]

    def register(self, provider: str, label: str = "", capabilities: list[str] | None = None,
                 mode: str = "external", role: str = "", native_session_id: str | None = None,
                 native_address: str | None = None) -> tuple[dict[str, Any], str]:
        if provider not in PROVIDERS or mode not in {"external", "managed", "managed_new", "managed_resume", "external_attached", "native_attached"}:
            raise BusError("Unsupported provider or mode")
        if not isinstance(label, str) or len(label) > 160 or not isinstance(role, str) or len(role) > 80:
            raise BusError("Invalid label")
        if native_session_id is not None and (not isinstance(native_session_id, str) or not 1 <= len(native_session_id) <= 256):
            raise BusError("Invalid native conversation ID")
        if native_address is not None and (not isinstance(native_address, str) or not 1 <= len(native_address) <= 256 or any(ch in native_address for ch in "\n\r\x00")):
            raise BusError("Invalid native address")
        capabilities = capabilities or []
        if not isinstance(capabilities, list) or len(capabilities) > 32 or any(not isinstance(c, str) or len(c) > 100 for c in capabilities):
            raise BusError("Invalid capabilities")
        agent_id, token, now = _id(), secrets.token_urlsafe(32), time.time()
        credential = self.credentials / f"{agent_id}.token"
        try:
            with self.connection() as db:
                db.execute("INSERT INTO agents (id,provider,label,capabilities,mode,token_hash,role,native_session_id,native_address,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'idle',?,?)",
                           (agent_id, provider, label or provider, _json(capabilities), mode, _hash(token), role, native_session_id, native_address, now, now))
                self.guard_workspace()
                with credential.open("x", encoding="utf-8") as stream:
                    self.guard_workspace()
                    stream.write(token)
                self.guard_workspace()
                os.chmod(credential, 0o600)
        except sqlite3.IntegrityError:
            raise BusError("Native conversation is already registered; use its original private session token", 409) from None
        except OSError:
            self.guard_workspace()
            if credential.exists():
                self.guard_workspace()
                credential.unlink()
            raise
        self._manifest(agent_id)
        return self.agent(agent_id), token

    def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"label", "role", "capabilities", "mode", "native_session_id", "native_address", "state", "pid", "generation", "active_root", "error", "channel_verified"}
        if set(fields) - allowed:
            raise BusError("Invalid agent fields")
        for field, maximum in (("label", 160), ("role", 80)):
            if field in fields and (not isinstance(fields[field], str) or len(fields[field]) > maximum):
                raise BusError("Invalid agent metadata")
        if "native_session_id" in fields and (not isinstance(fields["native_session_id"], str) or not 1 <= len(fields["native_session_id"]) <= 256):
            raise BusError("Invalid native conversation ID")
        if "native_address" in fields and (not isinstance(fields["native_address"], str) or not 1 <= len(fields["native_address"]) <= 256 or any(ch in fields["native_address"] for ch in "\n\r\x00")):
            raise BusError("Invalid native address")
        if "capabilities" in fields:
            capabilities = fields["capabilities"]
            if not isinstance(capabilities, list) or len(capabilities) > 32 or any(not isinstance(item, str) or len(item) > 100 for item in capabilities):
                raise BusError("Invalid capabilities")
            fields["capabilities"] = _json(fields["capabilities"])
        fields["updated_at"] = time.time()
        try:
            with self.connection() as db:
                count = db.execute("UPDATE agents SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", [*fields.values(), agent_id]).rowcount
                if not count:
                    raise BusError("Unknown agent", 404)
        except sqlite3.IntegrityError:
            raise BusError("Native conversation is already bound to another agent", 409) from None
        self._manifest(agent_id)
        return self.reconcile_auto_codex(agent_id)

    def heartbeat(self, agent_id: str, state: str | None = None,
                  native_session_id: str | None = None) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None:
                raise BusError("Unknown agent", 404)
            if native_session_id is not None:
                if not isinstance(native_session_id, str) or not 1 <= len(native_session_id) <= 256:
                    raise BusError("Invalid native conversation ID")
                if agent["native_session_id"] and native_session_id != agent["native_session_id"]:
                    raise BusError("Bound native conversation cannot be changed by heartbeat", 403)
                if agent["mode"] in {"external", "external_attached"} and not agent["native_session_id"]:
                    try:
                        db.execute("UPDATE agents SET native_session_id=? WHERE id=?", (native_session_id, agent_id))
                    except sqlite3.IntegrityError:
                        raise BusError("Native conversation is already bound to another agent", 409) from None
            # A model heartbeat cannot undo operator control or a durable job's
            # running/uncertain state. Only human controls resume those states.
            if state in {"idle", "needs_human"} and agent["state"] == "idle":
                db.execute("UPDATE agents SET state=? WHERE id=?", (state, agent_id))
            db.execute("UPDATE agents SET updated_at=? WHERE id=?", (time.time(), agent_id))
        self._manifest(agent_id)
        return self.reconcile_auto_codex(agent_id)

    def create_recovery(self, agent_id: str) -> dict[str, Any]:
        agent = self.agent(agent_id)
        if agent["mode"] not in {"external", "external_attached", "native_attached"}:
            raise BusError("Managed identities remain bound to their owned adapters", 403)
        code, expires_at = secrets.token_urlsafe(24), time.time() + 300
        with self.connection() as db:
            db.execute("INSERT INTO recovery_codes (code_hash,agent_id,provider,native_session_id,expires_at) VALUES (?,?,?,?,?)",
                       (_hash(code), agent_id, agent["provider"], agent["native_session_id"], expires_at))
        return {"recovery_code": code, "expires_at": expires_at, "agent_id": agent_id,
                "provider": agent["provider"], "native_session_id": agent["native_session_id"]}

    def redeem_recovery(self, provider: str, native_session_id: str | None,
                        code: str) -> tuple[dict[str, Any], str]:
        if not isinstance(code, str) or not 20 <= len(code) <= 128:
            raise BusError("Invalid or expired recovery code", 403)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            recovery = db.execute("SELECT * FROM recovery_codes WHERE code_hash=?", (_hash(code),)).fetchone()
            if recovery is None or recovery["used_at"] is not None or recovery["expires_at"] <= time.time():
                raise BusError("Invalid, expired, or consumed recovery code", 403)
            agent = db.execute("SELECT * FROM agents WHERE id=?", (recovery["agent_id"],)).fetchone()
            if agent is None or agent["mode"] not in {"external", "external_attached", "native_attached"} or agent["provider"] != provider or recovery["provider"] != provider or recovery["native_session_id"] != agent["native_session_id"]:
                raise BusError("Recovery code does not match this provider or native identity", 403)
            if native_session_id is not None and native_session_id != agent["native_session_id"]:
                raise BusError("Recovery code belongs to another native conversation", 403)
            credential = self.credentials / f"{agent['id']}.token"
            self.guard_workspace()
            if credential.is_symlink():
                raise BusError("Private credential cannot be a link", 409)
            self.guard_workspace()
            if self._reparse(credential):
                raise BusError("Private credential cannot be a link", 409)
            try:
                self.guard_workspace()
                token = credential.read_text(encoding="utf-8").strip()
            except OSError:
                raise BusError("Original native credential requires human repair", 409) from None
            if not secrets.compare_digest(_hash(token), agent["token_hash"]):
                raise BusError("Original native credential integrity failed", 409)
            db.execute("UPDATE recovery_codes SET used_at=? WHERE code_hash=?", (time.time(), _hash(code)))
            public = self._public_agent(agent)
        return public, token

    @staticmethod
    def _auto_flags(db: sqlite3.Connection) -> dict[str, Any]:
        values = dict(db.execute("SELECT key,value FROM config WHERE key IN ('auto_enabled','auto_suspended','auto_updated_at','auto_epoch','auto_codex_delivery','paused')").fetchall())
        enabled, suspended, paused = (values.get(key) == "true" for key in ("auto_enabled", "auto_suspended", "paused"))
        delivery = values.get("auto_codex_delivery", "")
        return {"enabled": enabled, "suspended": suspended, "paused": paused,
                "active": enabled and not suspended and not paused,
                "codex_delivery": delivery if delivery in {"native_queue", "stop_hook"} else "stop_hook",
                "codex_delivery_configured": delivery in {"native_queue", "stop_hook"},
                "updated_at": float(values.get("auto_updated_at", "0")),
                "epoch": float(values.get("auto_epoch") or values.get("auto_updated_at") or "0")}

    def auto_state(self) -> dict[str, Any]:
        with self.connection() as db:
            result = self._auto_flags(db)
            result["registered_count"] = db.execute("SELECT COUNT(*) FROM auto_contexts").fetchone()[0]
            result["observations"] = [dict(row) for row in db.execute("SELECT agent_id,state,note,updated_at FROM auto_observations ORDER BY agent_id")]
        return result

    def _write_auto_mirror(self) -> None:
        state = self.auto_state()
        mirror = self.directory / "auto.json"
        self.guard_workspace()
        self.reject_linked_path(mirror)
        self.guard_workspace()
        if mirror.is_symlink():
            raise BusError("Automatic-mode mirror cannot be a link", 409)
        self.guard_workspace()
        if self._reparse(mirror):
            raise BusError("Automatic-mode mirror cannot be a link", 409)
        self.guard_workspace()
        if mirror.exists():
            self.guard_workspace()
            if mirror.stat().st_nlink != 1:
                raise BusError("Automatic-mode mirror cannot be a link", 409)
        value = {"version": 1, "workspace": str(self.workspace), "workspace_id": self.workspace_id,
                 **{key: state[key] for key in ("enabled", "suspended", "paused", "updated_at", "epoch", "codex_delivery", "codex_delivery_configured")}}
        temporary = self.directory / (".auto-" + _id() + ".tmp")
        self.guard_workspace()
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        self.guard_workspace()
        os.replace(temporary, mirror)

    def configure_auto(self, enabled: bool | None = None, suspended: bool | None = None,
                       codex_delivery: str | None = None) -> dict[str, Any]:
        if (enabled is None and suspended is None and codex_delivery is None) or any(value is not None and not isinstance(value, bool) for value in (enabled, suspended)):
            raise BusError("Automatic settings require boolean enabled or suspended")
        if codex_delivery is not None and (not isinstance(codex_delivery, str) or codex_delivery not in {"native_queue", "stop_hook"}):
            raise BusError("Codex delivery must be native_queue or stop_hook")
        changed_agents = []
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._auto_flags(db)
            changed = (enabled is not None and enabled != previous["enabled"]) or (suspended is not None and suspended != previous["suspended"]) or (codex_delivery is not None and (not previous["codex_delivery_configured"] or codex_delivery != previous["codex_delivery"]))
            for key, value in (("auto_enabled", enabled), ("auto_suspended", suspended)):
                if value is not None:
                    db.execute("UPDATE config SET value=? WHERE key=?", ("true" if value else "false", key))
            if codex_delivery is not None:
                db.execute("UPDATE config SET value=? WHERE key='auto_codex_delivery'", (codex_delivery,))
            if changed:
                now = str(time.time())
                db.execute("UPDATE config SET value=? WHERE key='auto_updated_at'", (now,))
                if enabled is True and not previous["enabled"]:
                    db.execute("UPDATE config SET value=? WHERE key='auto_epoch'", (now,))
            for agent in db.execute("SELECT * FROM agents WHERE provider='codex'").fetchall():
                if self._reconcile_auto_codex(db, agent):
                    changed_agents.append(agent["id"])
        for agent_id in changed_agents:
            self._manifest(agent_id)
        self._write_auto_mirror()
        return self.auto_state()

    def _auto_codex_attested(self, db: sqlite3.Connection, agent: sqlite3.Row) -> bool:
        if agent["provider"] != "codex" or not agent["native_session_id"] or agent["mode"] not in {"external", "external_attached", "native_attached"}:
            return False
        context_id = _hash(self.workspace_id + "\0codex\0" + agent["native_session_id"])
        context = db.execute("SELECT * FROM auto_contexts WHERE context_id=? AND agent_id=? AND provider='codex' AND native_session_id=?", (context_id, agent["id"], agent["native_session_id"])).fetchone()
        sources = {"codex:SessionStart", "codex:UserPromptSubmit", "codex:Stop", "codex:native_environment"}
        if context is None or context["native_hook_source"] not in sources:
            return False
        return any(row["hook_source"] in sources | {"codex:mcp_transport"} for row in db.execute(
            "SELECT hook_source FROM auto_proofs WHERE context_id=? AND agent_id=? AND workspace_id=? AND provider='codex' AND native_session_id=? AND consumed_at IS NOT NULL",
            (context_id, agent["id"], self.workspace_id, agent["native_session_id"])))

    def auto_codex_attested(self, agent_id: str) -> bool:
        with self.connection() as db:
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            return agent is not None and self._auto_codex_attested(db, agent)

    def _reconcile_auto_codex(self, db: sqlite3.Connection, agent: sqlite3.Row) -> bool:
        if not self._auto_codex_attested(db, agent):
            return False
        flags = self._auto_flags(db)
        if db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply','waiting_user') LIMIT 1", (agent["id"],)).fetchone():
            return False
        mode = None
        if flags["active"] and flags["codex_delivery"] == "native_queue" and agent["state"] == "idle" and agent["mode"] in {"external", "external_attached"}:
            mode = "native_attached"
        elif flags["codex_delivery"] == "stop_hook" and agent["mode"] == "native_attached":
            mode = "external_attached"
        if mode is None:
            return False
        db.execute("UPDATE agents SET mode=?,channel_verified=0,updated_at=? WHERE id=?", (mode, time.time(), agent["id"]))
        return True

    def reconcile_auto_codex(self, agent_id: str) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None:
                raise BusError("Unknown agent", 404)
            changed = self._reconcile_auto_codex(db, agent)
        if changed:
            self._manifest(agent_id)
        return self.agent(agent_id)

    def touch_auto_control(self, agent_id: str | None = None) -> None:
        with self.connection() as db:
            if agent_id is None:
                db.execute("UPDATE config SET value=? WHERE key='auto_updated_at'", (str(time.time()),))
            else:
                db.execute("INSERT INTO auto_controls (agent_id,epoch) VALUES (?,?) ON CONFLICT(agent_id) DO UPDATE SET epoch=excluded.epoch", (agent_id, time.time()))
        if agent_id is None:
            self._write_auto_mirror()

    def observe_auto(self, actor: str, state: str, note: str = "") -> dict[str, Any]:
        if not isinstance(state, str) or not isinstance(note, str) or state not in {"watching", "waiting", "blocked", "offline"} or note not in {"", "ready", "idle", "paused", "suspended", "disabled", "cancelled", "offline", "no_pending", "channel_unavailable", "native_busy", "already_notified", "error"}:
            raise BusError("Invalid automatic watcher metadata")
        self.agent(actor)
        with self.connection() as db:
            db.execute("INSERT INTO auto_observations (agent_id,state,note,updated_at) VALUES (?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET state=excluded.state,note=excluded.note,updated_at=excluded.updated_at", (actor, state, note, time.time()))
        return {"auto": self.auto_state()}

    def auto_pending(self, actor: str) -> dict[str, Any]:
        agent = self.agent(actor)
        with self.connection() as db:
            auto = self._auto_flags(db)
            control = db.execute("SELECT epoch FROM auto_controls WHERE agent_id=?", (actor,)).fetchone()
            epoch = max(auto["updated_at"], control[0] if control else 0)
            active = db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply','waiting_user') LIMIT 1", (actor,)).fetchone() is not None
            rows = db.execute("SELECT j.id,j.event_id,j.root_id,j.state,j.created_at,(r.budget-r.used) AS budget_remaining,r.deadline FROM jobs j JOIN roots r ON r.id=j.root_id WHERE j.agent_id=? AND j.state='queued' AND r.deadline>? AND r.used<r.budget ORDER BY j.priority DESC,j.created_at,j.id", (actor, time.time())).fetchall()
        return {"agent": agent, "paused": auto["paused"], "auto": auto, "control_epoch": epoch,
                "wake_allowed": bool(auto["active"] and agent["mode"] in {"external", "external_attached"} and agent["state"] == "idle" and not active and rows),
                "queued": [dict(row) for row in rows]}

    def checked_auto_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        self.guard_workspace()
        candidate = Path(path)
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise BusError("Automatic context must use an absolute private path", 403)
        try:
            candidate.relative_to(self.workspace)
            candidate.relative_to(self.private / "auto")
        except ValueError:
            raise BusError("Automatic context belongs outside this workspace", 403) from None
        self.guard_workspace()
        self.reject_linked_path(candidate)
        self.guard_workspace()
        resolved_candidate = candidate.resolve()
        self.guard_workspace()
        resolved_private = (self.private / "auto").resolve()
        self.guard_workspace()
        if not resolved_candidate.is_relative_to(resolved_private):
            raise BusError("Automatic context escaped its private directory", 403)
        self.guard_workspace()
        if candidate.exists():
            self.guard_workspace()
            if not candidate.is_file():
                raise BusError("Invalid automatic context file", 403)
            self.guard_workspace()
            if candidate.stat().st_nlink != 1:
                raise BusError("Invalid automatic context file", 403)
            self.guard_workspace()
            if candidate.stat().st_size > 65536:
                raise BusError("Invalid automatic context file", 403)
        self.guard_workspace()
        if must_exist and not candidate.is_file():
            raise BusError("Automatic context file is unavailable", 409)
        return candidate

    def write_auto_private(self, path: str | Path, value: dict[str, Any]) -> None:
        target = self.checked_auto_path(path)
        current = self.private
        for part in target.parent.relative_to(self.private).parts:
            folder = current / part
            self.guard_workspace()
            self.reject_linked_path(folder)
            self.guard_workspace()
            folder.mkdir(exist_ok=True)
            current = folder
        temporary = target.parent / (".auto-" + _id() + ".tmp")
        self.guard_workspace()
        with temporary.open("x", encoding="utf-8") as stream:
            self.guard_workspace()
            stream.write(json.dumps(value, indent=2))
        self.guard_workspace()
        os.chmod(temporary, 0o600)
        self.guard_workspace()
        os.replace(temporary, target)

    def _auto_credential(self, agent: sqlite3.Row) -> str:
        self.guard_workspace()
        credential = self.credentials / (agent["id"] + ".token")
        self.reject_linked_path(credential)
        self.guard_workspace()
        if credential.is_symlink():
            raise BusError("Automatic credential requires human repair", 409)
        self.guard_workspace()
        if self._reparse(credential):
            raise BusError("Automatic credential requires human repair", 409)
        self.guard_workspace()
        if not credential.is_file():
            raise BusError("Automatic credential requires human repair", 409)
        self.guard_workspace()
        if credential.stat().st_nlink != 1:
            raise BusError("Automatic credential requires human repair", 409)
        self.guard_workspace()
        token = credential.read_text(encoding="utf-8").strip()
        if not secrets.compare_digest(_hash(token), agent["token_hash"]):
            raise BusError("Automatic credential integrity check failed", 409)
        return token

    def prepare_auto_context(self, provider: str, native_session_id: str, hook_source: str,
                             transport_binding: str | None = None) -> Path:
        events = {"claude": {"SessionStart", "UserPromptSubmit", "Stop"},
                  "codex": {"SessionStart", "UserPromptSubmit", "Stop", "mcp_transport", "native_environment"},
                  "antigravity": {"SessionStart", "PreInvocation", "BeforeAgent", "AfterAgent", "ConversationStart", "UserPromptSubmit", "Stop"}}
        if provider not in events or not isinstance(native_session_id, str) or not 1 <= len(native_session_id) <= 256 or any(ord(ch) < 32 for ch in native_session_id) or not isinstance(hook_source, str) or hook_source not in {provider + ":" + event for event in events[provider]} or (transport_binding is not None and (not isinstance(transport_binding, str) or not 1 <= len(transport_binding) <= 256 or any(ord(ch) < 32 for ch in transport_binding))):
            raise BusError("Invalid native automatic context")
        context_id = _hash(self.workspace_id + "\0" + provider + "\0" + native_session_id)
        context_file = self.private / "auto/contexts" / (context_id + ".json")
        self.checked_auto_path(context_file)
        created_credential: Path | None = None
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                if not self._auto_flags(db)["active"]:
                    raise BusError("Automatic participation is disabled, suspended, or paused", 409)
                existing_context = db.execute("SELECT * FROM auto_contexts WHERE context_id=?", (context_id,)).fetchone()
                if hook_source == "codex:mcp_transport" and (existing_context is None or not existing_context["native_hook_source"]):
                    raise BusError("Transport metadata lacks verified native workspace evidence", 403)
                agent = db.execute("SELECT * FROM agents WHERE provider=? AND native_session_id=?", (provider, native_session_id)).fetchone()
                if agent is not None and (agent["mode"] not in {"external", "external_attached", "native_attached"} or agent["state"] in {"paused", "stopped", "needs_human"}):
                    raise BusError("Existing owner or human control prevents automatic participation", 409)
                if agent is None:
                    from policy import default_role
                    agent_id, token, now = _id(), secrets.token_urlsafe(32), time.time()
                    self.guard_workspace()
                    db.execute("INSERT INTO agents (id,provider,label,capabilities,mode,token_hash,role,native_session_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'idle',?,?)", (agent_id, provider, provider + " " + native_session_id[:8], _json(["native-session", "automatic-context"]), "external_attached", _hash(token), default_role(self.workspace, provider, workspace_guard=self.guard_workspace) or "", native_session_id, now, now))
                    created_credential = self.credentials / (agent_id + ".token")
                    self.guard_workspace()
                    with created_credential.open("x", encoding="utf-8") as stream:
                        self.guard_workspace()
                        stream.write(token)
                    self.guard_workspace()
                    os.chmod(created_credential, 0o600)
                    agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
                self._auto_credential(agent)
                reused = False
                self.guard_workspace()
                if context_file.exists():
                    try:
                        checked_context = self.checked_auto_path(context_file, must_exist=True)
                        self.guard_workspace()
                        old = json.loads(checked_context.read_text(encoding="utf-8"))
                        proof = db.execute("SELECT * FROM auto_proofs WHERE code_hash=?", (_hash(old.get("code", "")),)).fetchone()
                        if proof is not None and proof["context_id"] == context_id and proof["agent_id"] == agent["id"] and proof["claim_hash"] == _hash(old.get("claim_id", "")) and proof["expires_at"] > time.time() and proof["transport_binding"] == transport_binding and proof["hook_source"] == hook_source:
                            if self._reconcile_auto_codex(db, agent):
                                reused = True
                            else:
                                return context_file
                    except BusError:
                        raise
                    except (ValueError, TypeError, AttributeError):
                        pass
                if not reused:
                    code, claim_id, expires_at = secrets.token_urlsafe(32), secrets.token_urlsafe(24), time.time() + 300
                    native_hook_source = hook_source if hook_source != "codex:mcp_transport" else existing_context["native_hook_source"]
                    db.execute("INSERT INTO auto_contexts (context_id,agent_id,provider,native_session_id,hook_source,native_hook_source,transport_binding,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(context_id) DO UPDATE SET hook_source=excluded.hook_source,native_hook_source=COALESCE(auto_contexts.native_hook_source,excluded.native_hook_source),transport_binding=excluded.transport_binding,updated_at=excluded.updated_at", (context_id, agent["id"], provider, native_session_id, hook_source, native_hook_source, transport_binding, time.time()))
                    db.execute("INSERT INTO auto_proofs (code_hash,context_id,agent_id,workspace_id,provider,native_session_id,hook_source,transport_binding,claim_hash,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (_hash(code), context_id, agent["id"], self.workspace_id, provider, native_session_id, hook_source, transport_binding, _hash(claim_id), expires_at))
                    self.write_auto_private(context_file, {"version": 1, "purpose": "auto_bootstrap", "workspace": str(self.workspace), "workspace_id": self.workspace_id, "provider": provider, "native_session_id": native_session_id, "hook_source": hook_source, "native_hook_source": native_hook_source, "transport_binding": transport_binding, "context_id": context_id, "agent_id": agent["id"], "code": code, "claim_id": claim_id, "expires_at": expires_at})
                    self._reconcile_auto_codex(db, agent)
        except Exception:
            self.guard_workspace()
            if created_credential is not None and created_credential.exists():
                self.guard_workspace()
                created_credential.unlink()
            raise
        self._manifest(agent["id"])
        return context_file

    def redeem_auto(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("purpose") != "auto_bootstrap" or any(not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > 512 for key in ("workspace_id", "provider", "native_session_id", "hook_source", "context_id", "code", "claim_id")):
            raise BusError("Invalid automatic bootstrap proof", 403)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._auto_flags(db)["active"]:
                raise BusError("Automatic participation is disabled, suspended, or paused", 409)
            proof = db.execute("SELECT * FROM auto_proofs WHERE code_hash=?", (_hash(payload["code"]),)).fetchone()
            keys = ("workspace_id", "provider", "native_session_id", "hook_source", "context_id")
            if proof is None or proof["expires_at"] <= time.time() or any(proof[key] != payload[key] for key in keys) or proof["workspace_id"] != self.workspace_id or proof["claim_hash"] != _hash(payload["claim_id"]) or proof["transport_binding"] != payload.get("transport_binding"):
                raise BusError("Automatic proof is expired or belongs to another native context", 403)
            agent = db.execute("SELECT * FROM agents WHERE id=?", (proof["agent_id"],)).fetchone()
            if agent is None or agent["provider"] != proof["provider"] or agent["native_session_id"] != proof["native_session_id"] or agent["mode"] not in {"external", "external_attached", "native_attached"} or agent["state"] in {"paused", "stopped", "needs_human"}:
                raise BusError("Automatic proof cannot adopt this owner", 403)
            token = self._auto_credential(agent)
            if proof["consumed_at"] is None:
                db.execute("UPDATE auto_proofs SET consumed_at=? WHERE code_hash=?", (time.time(), proof["code_hash"]))
            changed = self._reconcile_auto_codex(db, agent)
            if changed:
                agent = db.execute("SELECT * FROM agents WHERE id=?", (agent["id"],)).fetchone()
            result = {"agent": self._public_agent(agent), "token": token, "session_token": token}
        if changed:
            self._manifest(agent["id"])
        return result

    def _manifest(self, agent_id: str) -> None:
        with self._manifest_lock:
            agent = self.agent(agent_id)
            folder = self.directory / agent["provider"] / agent_id
            for path in (folder.parent, folder, folder / "exports"):
                self.guard_workspace()
                if path.is_symlink():
                    raise BusError("Agent directory cannot be a link")
                self.guard_workspace()
                if self._reparse(path):
                    raise BusError("Agent directory cannot be a link")
                self.guard_workspace()
                path.mkdir(exist_ok=True)
            temporary = folder / f".manifest-{_id()}.tmp"
            self.guard_workspace()
            temporary.write_text(json.dumps(agent, ensure_ascii=False, indent=2), encoding="utf-8")
            self.guard_workspace()
            os.replace(temporary, folder / "manifest.json")

    def paused(self) -> bool:
        with self.connection() as db:
            return db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "true"

    def set_paused(self, value: bool) -> None:
        with self.connection() as db:
            db.execute("UPDATE config SET value=? WHERE key='paused'", ("true" if value else "false",))

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        event = dict(row)
        event["metadata"] = json.loads(event["metadata"])
        return event

    def events(self, after: int = 0, limit: int = 200) -> dict[str, Any]:
        if after < 0:
            raise BusError("Invalid event cursor")
        with self.connection() as db:
            rows = db.execute("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, max(1, min(limit, 500)))).fetchall()
            maximum = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        return {"events": [self._event(row) for row in rows], "last_seq": rows[-1]["seq"] if rows else max(after, maximum)}

    def _insert_event(self, db: sqlite3.Connection, kind: str, sender: str, recipient: str,
                      body: str, root_id: str | None, metadata: dict[str, Any] | None = None,
                      event_id: str | None = None) -> dict[str, Any]:
        event_id = event_id or _id()
        db.execute("INSERT INTO events (id,kind,sender,recipient,body,root_id,created_at,metadata) VALUES (?,?,?,?,?,?,?,?)",
                   (event_id, kind, sender, recipient, body, root_id, time.time(), _json(metadata or {})))
        return self._event(db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone())

    def notice(self, body: str, recipient: str = "user", root_id: str | None = None,
               sender: str = "system", kind: str = "notice", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.connection() as db:
            return self._insert_event(db, kind, sender, recipient, body, root_id, metadata)

    def send(self, actor: str, to: str, body: str, kind: str = "message", *, budget: int = 24,
             lifetime: float = 3600, native_origin: str | None = None,
             in_reply_to: str | None = None) -> dict[str, Any]:
        if kind not in KINDS or not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > MAX_BODY:
            raise BusError("Invalid message kind or body (maximum 64 KiB)")
        if not isinstance(to, str):
            raise BusError("Invalid recipient")
        if not isinstance(budget, int) or isinstance(budget, bool) or not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool) or not 1 <= budget <= 200 or not 1 <= lifetime <= 86400:
            raise BusError("Invalid root budget")
        now, event_id = time.time(), _id()
        human_continuations: set[str] = set()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sender = None if actor == "user" else db.execute("SELECT * FROM agents WHERE id=?", (actor,)).fetchone()
            if actor != "user" and sender is None:
                raise BusError("Unknown sender", 401)
            if native_origin:
                if actor != "user":
                    raise BusError("Native human inputs need operator authentication", 403)
                origin = db.execute("SELECT state FROM agents WHERE id=?", (native_origin,)).fetchone()
                if origin is None or origin["state"] in {"paused", "stopped"} or db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "true":
                    raise BusError("Human control paused or stopped this native participant", 409)
                if db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply')", (native_origin,)).fetchone():
                    raise BusError("This native session already has an active correlated turn", 409)
            if to == "all":
                recipients = db.execute("SELECT id FROM agents WHERE id<>?", (actor,)).fetchall()
            elif to == "user":
                recipients = []
            else:
                recipients = db.execute("SELECT id FROM agents WHERE id=? OR provider=?", (to, to)).fetchall()
                if not recipients:
                    raise BusError("Recipient is not registered", 404)
                if len(recipients) > 1:
                    raise BusError("Provider matches multiple roles; use the exact agent ID", 409)
            reply_metadata: dict[str, Any] = {}
            replied = None
            if in_reply_to:
                replied = db.execute("SELECT * FROM events WHERE id=?", (in_reply_to,)).fetchone()
                if replied is None:
                    raise BusError("Reply event does not exist", 404)
                reply_metadata["in_reply_to"] = in_reply_to
            if actor == "user":
                if kind in WAKE_KINDS:
                    waiting_reply = json.loads(replied["metadata"]).get("waiting_job_id") if replied is not None else None
                    if waiting_reply and replied is not None:
                        waiting = db.execute("SELECT * FROM jobs WHERE id=?", (waiting_reply,)).fetchone()
                        owner = db.execute("SELECT * FROM agents WHERE id=?", (replied["sender"],)).fetchone()
                        if len(recipients) != 1 or recipients[0]["id"] != replied["sender"] or waiting is None or waiting["state"] != "waiting_user" or waiting["agent_id"] != replied["sender"] or owner is None or owner["active_root"] != waiting["root_id"]:
                            raise BusError("Human answer does not match a currently waiting native question", 409)
                    for recipient in recipients:
                        waiting = db.execute("SELECT id FROM jobs WHERE agent_id=? AND state='waiting_user'", (recipient["id"],)).fetchall()
                        if waiting and in_reply_to and not waiting_reply:
                            raise BusError("Reply to the exact waiting question or send a new human instruction", 409)
                        if waiting:
                            human_continuations.add(recipient["id"])
                            db.execute("UPDATE jobs SET state='input_received',finished_at=? WHERE agent_id=? AND state='waiting_user'", (now, recipient["id"]))
                            db.execute("UPDATE agents SET state=CASE WHEN state='waiting_for_user' THEN 'idle' ELSE state END,active_root=NULL,updated_at=? WHERE id=?", (now, recipient["id"]))
                root_id = _id()
                db.execute("INSERT INTO roots (id,human_event_id,budget,deadline,created_at) VALUES (?,?,?,?,?)",
                           (root_id, event_id, budget, now + lifetime, now))
                for recipient in recipients:
                    db.execute("UPDATE agents SET state='idle',error=NULL WHERE id=? AND state='needs_human'", (recipient["id"],))
            else:
                if sender is None:
                    raise BusError("Unknown sender", 401)
                root_id = sender["active_root"]
                if kind in WAKE_KINDS and to != "user" and not root_id:
                    raise BusError("Agent tasks require an active human root; ask the user to start work", 409)
                if root_id:
                    root = db.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
                    if root is None or (root["deadline"] <= now and kind in WAKE_KINDS):
                        raise BusError("Human root expired; ask the user for a new instruction", 409)
            if actor != "user" and to == "user" and kind == "question" and sender is not None and sender["mode"] in {"external_attached", "native_attached"} and root_id:
                waiting = db.execute("SELECT * FROM jobs WHERE agent_id=? AND root_id=? AND state IN ('running','awaiting_native_reply','waiting_user')", (actor, root_id)).fetchone()
                if waiting is not None and sender["state"] in {"running", "awaiting_native_reply", "waiting_for_user"}:
                    db.execute("UPDATE jobs SET state='waiting_user',finished_at=? WHERE id=?", (now, waiting["id"]))
                    db.execute("UPDATE agents SET state='waiting_for_user',updated_at=? WHERE id=?", (now, actor))
                    reply_metadata.update({"waiting_job_id": waiting["id"], "native_session_id": sender["native_session_id"]})
            if native_origin:
                reply_metadata["native_origin"] = native_origin
            event = self._insert_event(db, kind, actor, to, body, root_id, reply_metadata, event_id=event_id)
            jobs = []
            if kind in WAKE_KINDS:
                for recipient in recipients:
                    if recipient["id"] == actor:
                        continue
                    job_id = _id()
                    state = "awaiting_native_reply" if recipient["id"] == native_origin else "queued"
                    db.execute("INSERT INTO jobs (id,event_id,agent_id,root_id,state,created_at,priority) VALUES (?,?,?,?,?,?,?)",
                               (job_id, event_id, recipient["id"], root_id, state, now, int(recipient["id"] in human_continuations)))
                    if recipient["id"] == native_origin:
                        db.execute("UPDATE roots SET used=used+1 WHERE id=?", (root_id,))
                        db.execute("UPDATE agents SET state='awaiting_native_reply',active_root=?,updated_at=? WHERE id=?", (root_id, now, native_origin))
                    jobs.append({"id": job_id, "agent_id": recipient["id"], "state": state})
        result = {"event": event, "jobs": jobs, "root_id": root_id}
        if native_origin:
            result["origin_job_id"] = next(job["id"] for job in jobs if job["agent_id"] == native_origin)
            human_continuations.add(native_origin)
        if event["metadata"].get("waiting_job_id"):
            human_continuations.add(actor)
        for agent_id in human_continuations:
            self._manifest(agent_id)
        return result

    def jobs(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id,event_id,agent_id,root_id,state,created_at,started_at,finished_at,error FROM jobs ORDER BY created_at DESC LIMIT 100")]

    def has_active_job(self, agent_id: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply') LIMIT 1", (agent_id,)).fetchone() is not None

    def active_jobs(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT j.id,j.event_id,j.agent_id,j.root_id,j.state,j.created_at,j.started_at,j.finished_at,j.error,r.deadline AS root_deadline FROM jobs j JOIN roots r ON r.id=j.root_id WHERE j.state IN ('running','awaiting_native_reply') ORDER BY j.created_at")]

    def claim(self, agent_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "true":
                return None
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None or agent["state"] in {"stopped", "paused", "needs_human", "starting", "unavailable", "awaiting_native_reply", "waiting_for_user"}:
                return None
            if agent["provider"] == "codex" and agent["mode"] == "native_attached":
                auto = self._auto_flags(db)
                if not auto["active"] or auto["codex_delivery"] != "native_queue" or not self._auto_codex_attested(db, agent):
                    return None
            if db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply','waiting_user')", (agent_id,)).fetchone():
                return None
            while True:
                job = db.execute("SELECT * FROM jobs WHERE agent_id=? AND state='queued' ORDER BY priority DESC,created_at,id LIMIT 1", (agent_id,)).fetchone()
                if job is None:
                    return None
                root = db.execute("SELECT * FROM roots WHERE id=?", (job["root_id"],)).fetchone()
                if root is None or root["used"] >= root["budget"] or root["deadline"] <= time.time():
                    reason = "Human root automatic-turn budget exhausted or deadline expired"
                    db.execute("UPDATE jobs SET state='needs_human',error=?,finished_at=? WHERE id=?", (reason, time.time(), job["id"]))
                    self._insert_event(db, "notice", "system", "user", reason, job["root_id"], {"job_id": job["id"], "agent_id": agent_id})
                    continue
                now = time.time()
                db.execute("UPDATE roots SET used=used+1 WHERE id=?", (root["id"],))
                db.execute("UPDATE jobs SET state='running',started_at=?,generation=? WHERE id=?", (now, agent["generation"], job["id"]))
                db.execute("UPDATE agents SET state='running',active_root=?,updated_at=? WHERE id=?", (root["id"], now, agent_id))
                result = dict(job)
                result["state"] = "running"
                result["event"] = self._event(db.execute("SELECT * FROM events WHERE id=?", (job["event_id"],)).fetchone())
                result["remaining_turns"] = root["budget"] - root["used"] - 1
                return result

    def finish(self, job_id: str, state: str, *, text: str = "", error: str | None = None,
               native_session_id: str | None = None,
               expected_native: tuple[str, str, str] | None = None) -> None:
        if state not in {"completed", "failed", "interrupted", "needs_human"}:
            raise BusError("Invalid job terminal state")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["state"] not in {"running", "awaiting_native_reply"}:
                raise BusError("Job is not running", 409)
            if expected_native:
                actor, expected_event, expected_session = expected_native
                owner = db.execute("SELECT * FROM agents WHERE id=?", (actor,)).fetchone()
                if owner is None or job["agent_id"] != actor or job["event_id"] != expected_event or owner["active_root"] != job["root_id"] or owner["native_session_id"] != expected_session:
                    raise BusError("Native result no longer owns this active input", 409)
                if owner["state"] in {"paused", "stopped"} or db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "true":
                    raise BusError("Human control paused or stopped this native input", 409)
            now = time.time()
            db.execute("UPDATE jobs SET state=?,finished_at=?,error=? WHERE id=?", (state, now, error, job_id))
            current = db.execute("SELECT state FROM agents WHERE id=?", (job["agent_id"],)).fetchone()[0]
            agent_state = current if current in {"paused", "stopped"} else "idle" if state == "completed" else "needs_human"
            db.execute("UPDATE agents SET state=?,active_root=NULL,error=?,native_session_id=COALESCE(?,native_session_id),updated_at=? WHERE id=?",
                       (agent_state, error, native_session_id, now, job["agent_id"]))
            if expected_native and state == "completed":
                db.execute("UPDATE agents SET channel_verified=1 WHERE id=?", (job["agent_id"],))
            if text:
                self._insert_event(db, "result", job["agent_id"], "user", text[:MAX_BODY], job["root_id"], {"job_id": job_id, "state": state})
            if error:
                self._insert_event(db, "notice", "system", "user", error, job["root_id"], {"job_id": job_id, "agent_id": job["agent_id"], "state": state})
        self._manifest(job["agent_id"])

    def await_native(self, job_id: str, receipt: str, bridge_session_id: str | None = None) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is not None and job["state"] in {"completed", "waiting_user", "input_received"}:
                return
            if job is not None and job["state"] not in {"running", "awaiting_native_reply"}:
                job = None
            if job is None:
                raise BusError("Native dispatch job is not running", 409)
            owner = db.execute("SELECT state,active_root FROM agents WHERE id=?", (job["agent_id"],)).fetchone()
            paused = db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "true"
            if paused or owner is None or owner["state"] not in {"running", "awaiting_native_reply"} or owner["active_root"] != job["root_id"]:
                raise BusError("Native delivery no longer owns an active, unpaused input", 409)
            db.execute("UPDATE jobs SET state='awaiting_native_reply' WHERE id=?", (job_id,))
            db.execute("UPDATE agents SET state='awaiting_native_reply',updated_at=? WHERE id=?", (time.time(), job["agent_id"]))
            self._insert_event(db, "notice", "system", "user", receipt[:4000], job["root_id"],
                               {"job_id": job_id, "event_id": job["event_id"], "agent_id": job["agent_id"],
                                "bridge_session_id": bridge_session_id, "state": "awaiting_native_reply"})
        self._manifest(job["agent_id"])

    def native_result(self, actor: str, native_session_id: str, text: str,
                      job_id: str | None = None, event_id: str | None = None,
                      requires_review_if_uncorrelated: bool = False) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT state FROM agents WHERE id=?", (actor,)).fetchone()
            paused = db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "true"
            if current is None or current["state"] in {"paused", "stopped"} or paused:
                raise BusError("Human control paused or stopped this native input", 409)
        agent = self.agent(actor)
        if not native_session_id or native_session_id != agent["native_session_id"] or agent["mode"] not in {"external_attached", "native_attached"}:
            raise BusError("Native result must match the authenticated attached conversation", 403)
        if not isinstance(text, str) or not text or len(text.encode("utf-8")) > MAX_BODY:
            raise BusError("Native result must contain 1 byte to 64 KiB of text")
        if not (job_id and event_id):
            result = self.send(actor, "all", text, "result")
            if requires_review_if_uncorrelated:
                with self.connection() as db:
                    waiting = db.execute("SELECT * FROM jobs WHERE agent_id=? AND root_id=? AND state='awaiting_native_reply'", (actor, agent["active_root"])).fetchone()
                if waiting is not None:
                    reason = "Native output has no exact input correlation; work completion is unverified and requires human review"
                    self.finish(waiting["id"], "needs_human", error=reason,
                                expected_native=(actor, waiting["event_id"], native_session_id))
                    return {"completed": False, "needs_human": True, "reason": reason, "job_id": waiting["id"], **result}
            return {"completed": False, "reason": "No exact input correlation; recorded a passive result only", **result}
        with self.connection() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None or job["agent_id"] != actor or job["event_id"] != event_id or job["root_id"] != agent["active_root"] or job["state"] != "awaiting_native_reply":
            raise BusError("Native result correlation does not match an active owned input", 409)
        self.finish(job_id, "completed", text=text, native_session_id=native_session_id,
                    expected_native=(actor, event_id, native_session_id))
        return {"completed": True, "job_id": job_id, "event_id": event_id, "root_id": job["root_id"]}

    def recover(self) -> int:
        """Never replay an uncertain turn that may already have side effects."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM jobs WHERE state IN ('running','awaiting_native_reply')").fetchall()
            for job in rows:
                reason = "Daemon exited during a turn; outcome unknown. Review before sending a new instruction."
                db.execute("UPDATE jobs SET state='needs_human',finished_at=?,error=? WHERE id=?", (time.time(), reason, job["id"]))
                db.execute("UPDATE agents SET state=CASE WHEN state IN ('paused','stopped') THEN state ELSE 'needs_human' END,active_root=NULL,pid=NULL,error=? WHERE id=?", (reason, job["agent_id"]))
                self._insert_event(db, "notice", "system", "user", reason, job["root_id"], {"job_id": job["id"], "agent_id": job["agent_id"]})
            db.execute("UPDATE agents SET pid=NULL,state=CASE WHEN state IN ('starting','running','awaiting_native_reply') THEN 'needs_human' ELSE state END")
        for agent in self.agents():
            self._manifest(agent["id"])
        return len(rows)

    def external_inbox(self, actor: str, after: int = 0, acknowledgements: list[str] | None = None,
                       *, claim_work: bool = True) -> dict[str, Any]:
        if not isinstance(claim_work, bool):
            raise BusError("Inbox claim setting must be boolean")
        agent = self.agent(actor)
        if agent["mode"] not in {"external", "external_attached", "native_attached"}:
            raise BusError("Managed agent inbox is dispatched by the supervisor", 409)
        for job_id in acknowledgements or []:
            with self.connection() as db:
                row = db.execute("SELECT agent_id,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["agent_id"] != actor:
                raise BusError("Cannot acknowledge another agent's work", 403)
            if row["state"] == "running":
                self.finish(job_id, "completed")
        job = self.claim(actor) if claim_work else None
        with self.connection() as db:
            rows = db.execute("SELECT e.* FROM events e JOIN jobs j ON j.event_id=e.id WHERE j.agent_id=? AND e.seq>? ORDER BY e.seq LIMIT 100", (actor, after)).fetchall()
        return {"messages": [self._event(row) for row in rows], "jobs": [job] if job else []}

    def safe_source(self, source: str) -> Path:
        self.guard_workspace()
        if not isinstance(source, str) or not source or len(source) > 1000:
            raise BusError("Invalid artifact source")
        windows = PureWindowsPath(source)
        if windows.is_absolute() or windows.drive or source.startswith(("/", "\\")) or ":" in source:
            raise BusError("Artifact source must be workspace-relative without drive, UNC, or ADS")
        parts = source.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} or part.rstrip(" .") != part or "\x00" in part or PureWindowsPath(part).is_reserved() for part in parts) or parts[0].casefold() == ".sharebus":
            raise BusError("Artifact traversal and internal sharebus paths are forbidden")
        target = self.workspace
        for part in parts:
            target = target / part
            self.guard_workspace()
            if target.is_symlink():
                raise BusError("Artifact symlinks and reparse points are forbidden")
            self.guard_workspace()
            if self._reparse(target):
                raise BusError("Artifact symlinks and reparse points are forbidden")
        try:
            self.guard_workspace()
            resolved = target.resolve(strict=True)
            self.guard_workspace()
            relative = resolved.relative_to(self.workspace)
            if relative.parts and relative.parts[0].casefold() == ".sharebus":
                raise ValueError("Internal Sharebus path")
        except BusError:
            raise
        except (OSError, ValueError):
            raise BusError("Artifact must exist inside this workspace") from None
        self.guard_workspace()
        if not resolved.is_file():
            raise BusError("Artifact must be a regular file")
        return resolved

    @staticmethod
    def _filename(filename: str) -> str:
        if not isinstance(filename, str) or not filename or len(filename) > 160 or any(ch in filename for ch in '/\\:\x00') or filename in {".", ".."}:
            raise BusError("Invalid artifact filename")
        if filename.rstrip(" .") != filename or PureWindowsPath(filename).is_reserved():
            raise BusError("Invalid Windows artifact filename")
        return filename

    def publish(self, actor: str, *, source: str | None = None, text: str | None = None,
                filename: str | None = None, _copy_id: str | None = None) -> dict[str, Any]:
        if actor == "user":
            raise BusError("Publish using an agent capability", 403)
        agent = self.agent(actor)
        if sum(value is not None for value in (source, text, _copy_id)) != 1:
            raise BusError("Provide exactly one of source or text")
        source_path = self.artifact_file(_copy_id)[1] if _copy_id is not None else self.safe_source(source) if source is not None else None
        name = self._filename(filename or (source_path.name if source_path else "note.txt"))
        if text is not None and not isinstance(text, str):
            raise BusError("Artifact text must be a string")
        self._manifest(actor)
        artifact_id = _id()
        target = self.directory / agent["provider"] / actor / "exports" / f"{artifact_id}-{name}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        self.guard_workspace()
        descriptor = os.open(target, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                if source_path is not None:
                    self.guard_workspace()
                    with source_path.open("rb") as original:
                        info = os.fstat(original.fileno())
                        if info.st_nlink != 1:
                            raise BusError("Artifact source hardlinks are forbidden")
                        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT:
                            raise BusError("Artifact maximum size is 20 MiB")
                        data = original.read(MAX_ARTIFACT + 1)
                        if len(data) > MAX_ARTIFACT:
                            raise BusError("Artifact maximum size is 20 MiB")
                        self.guard_workspace()
                        output.write(data)
                else:
                    data = (text or "").encode("utf-8")
                    if len(data) > MAX_ARTIFACT:
                        raise BusError("Artifact maximum size is 20 MiB")
                    self.guard_workspace()
                    output.write(data)
            self.guard_workspace()
            if target.stat().st_size > MAX_ARTIFACT:
                raise BusError("Artifact maximum size is 20 MiB")
            self.guard_workspace()
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            self.guard_workspace()
            os.chmod(target, 0o444)
            self.guard_workspace()
            record = {"id": artifact_id, "owner": actor, "filename": name,
                      "relative_path": target.relative_to(self.workspace).as_posix(), "sha256": digest,
                      "size": target.stat().st_size, "created_at": time.time(), "root_id": agent["active_root"]}
            with self.connection() as db:
                db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)", tuple(record.values()))
                metadata: dict[str, Any] = {"artifact": record}
                if _copy_id:
                    metadata["copied_from"] = _copy_id
                self._insert_event(db, "artifact", actor, "all", name, agent["active_root"], metadata)
            return record
        except Exception:
            self.guard_workspace()
            if target.exists():
                self.guard_workspace()
                os.chmod(target, 0o600)
                self.guard_workspace()
                target.unlink()
            raise

    def artifacts(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM artifacts ORDER BY created_at DESC LIMIT 200")]

    def artifact_file(self, artifact_id: str) -> tuple[dict[str, Any], Path]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise BusError("Unknown exported artifact", 404)
        record = dict(row)
        self.guard_workspace()
        target = self.workspace / record["relative_path"]
        cursor = target
        while cursor != self.workspace:
            self.guard_workspace()
            if cursor.is_symlink():
                raise BusError("Export was replaced by a link", 409)
            self.guard_workspace()
            if self._reparse(cursor):
                raise BusError("Export was replaced by a link", 409)
            cursor = cursor.parent
        try:
            self.guard_workspace()
            info = target.stat()
            self.guard_workspace()
            if info.st_nlink != 1 or info.st_size != record["size"] or hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
                raise BusError("Export snapshot integrity changed", 409)
        except FileNotFoundError:
            raise BusError("Export snapshot is missing", 404) from None
        return record, target
