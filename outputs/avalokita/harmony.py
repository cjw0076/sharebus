"""Avalokita's local, cooperative peer coordination kernel.

Only explicitly published output is captured. This module neither reads native
conversation stores nor starts models. The HTTP/native adapter authenticates a
caller before passing its registered pane ID here. Same-user filesystem access
is not an isolation boundary; fencing protects writes made through this API.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

PROTOCOL = "avalokita.harmony/1"
KINDS = {"message", "proposal", "claim", "handoff", "result", "question", "status"}
MAX_BODY_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_FILE_BYTES = 512 * 1024
MAX_CONTEXT_FILES = 32
DEFAULT_LEASE_TTL = 120
MAX_LEASE_TTL = 3600
_PRIVATE_PARTS = {
    ".avalokita", ".sharebus", ".git", ".ssh", ".aws", ".azure", ".gnupg",
    ".codex", ".claude", ".gemini", "credentials", "secrets", "keychains",
}
_SECRET_NAMES = {
    "auth.json", "credentials.json", "credentials.db", "cookies", "cookies.sqlite",
    "login data", "id_rsa", "id_ed25519", "token.json", "tokens.json",
}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
_FORBIDDEN_METADATA = {
    "actor", "origin", "native_session_id", "workspace_id", "root_id", "sequence",
    "session_token", "access_token", "refresh_token", "api_key", "authorization",
    "password", "cookie", "cookies", "reasoning", "chain_of_thought", "session_dump",
    "private_reasoning", "native_transcript", "full_transcript",
}
_SENSITIVE_TEXT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,})\b|"
    r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE
)


class HarmonyError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HarmonyError("Payload must be finite JSON data") from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text(value: Any, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise HarmonyError(f"{name} must be a nonempty string of at most {maximum} characters")
    if any(ord(char) < 32 for char in value):
        raise HarmonyError(f"{name} cannot contain control characters")
    return value


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarmonyError(f"{name} must be numeric")
    if not minimum <= value <= maximum or not math.isfinite(value):
        raise HarmonyError(f"{name} is outside the allowed range")
    return float(value)


def _linked(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class HarmonyStore:
    """Durable selected messages, human authority roots and fenced file leases."""

    def __init__(self, workspace: Path):
        candidate = Path(workspace).absolute()
        try:
            info = candidate.stat()
        except (FileNotFoundError, NotADirectoryError):
            raise HarmonyError("Workspace must be an existing directory") from None
        if os.name == "nt" and info.st_ino == 0:
            raise HarmonyError("Workspace identity metadata is unavailable", 503)
        if not stat.S_ISDIR(info.st_mode):
            raise HarmonyError("Workspace must be an existing directory")
        for ancestor in (candidate, *candidate.parents):
            if _linked(ancestor):
                raise HarmonyError("Workspace cannot traverse a symbolic link or reparse point")
        self.workspace = candidate.resolve(strict=True)
        identity = self.workspace.stat()
        if os.name == "nt" and identity.st_ino == 0:
            raise HarmonyError("Workspace identity metadata is unavailable", 503)
        self._workspace_identity = (identity.st_dev, identity.st_ino)
        self._workspace_retired = False
        self._workspace_guard: Callable[[], None] | None = None
        self._database_initialized = False
        self.directory = self.workspace / ".avalokita"
        self.private = self.directory / "private"
        for folder in (self.directory, self.private):
            if _linked(folder) or (folder.exists() and not folder.is_dir()):
                raise HarmonyError("Avalokita state directory must be a real directory")
            folder.mkdir(exist_ok=True)
        try:
            os.chmod(self.private, 0o700)
        except OSError:
            pass
        self.db_path = self.private / "harmony.sqlite3"
        self._check_database_files()
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS panes (
                    id TEXT PRIMARY KEY,provider TEXT NOT NULL,role TEXT NOT NULL,
                    native_session_id TEXT NOT NULL,capabilities TEXT NOT NULL,
                    verified INTEGER NOT NULL,namespace TEXT NOT NULL UNIQUE,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    UNIQUE(provider,native_session_id)
                );
                CREATE TABLE IF NOT EXISTS goals (
                    id TEXT PRIMARY KEY,prompt TEXT NOT NULL,budget INTEGER NOT NULL,
                    used INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,
                    deadline REAL NOT NULL,created_at REAL NOT NULL,
                    idempotency_key TEXT UNIQUE,request_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL,kind TEXT NOT NULL,root_id TEXT,recipient TEXT,
                    envelope TEXT NOT NULL,request_hash TEXT NOT NULL,
                    idempotency_key TEXT,created_at REAL NOT NULL,
                    UNIQUE(actor,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS event_pane_cursor ON events(actor,seq);
                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT PRIMARY KEY,actor TEXT NOT NULL,resources TEXT NOT NULL,
                    resource_keys TEXT NOT NULL,fence INTEGER NOT NULL UNIQUE,
                    expires_at REAL NOT NULL,state TEXT NOT NULL,
                    created_at REAL NOT NULL,updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contexts (
                    id TEXT PRIMARY KEY,actor TEXT NOT NULL,label TEXT NOT NULL,
                    generation INTEGER NOT NULL UNIQUE,files TEXT NOT NULL,
                    total_bytes INTEGER NOT NULL,manifest_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_event_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT,'Events are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_event_delete BEFORE DELETE ON events
                    BEGIN SELECT RAISE(ABORT,'Events are retained'); END;
            """)
            db.execute("INSERT OR IGNORE INTO config VALUES('workspace_id',?)", (str(uuid.uuid4()),))
            db.execute("INSERT OR IGNORE INTO config VALUES('fence','0')")
            db.execute("INSERT OR IGNORE INTO config VALUES('context_generation','0')")
            db.execute("INSERT OR IGNORE INTO config VALUES('paused','0')")
            # Older local prototypes had unbound leases/context. Retain their
            # history, but a null root can never authorize a new mutation.
            for table in ("leases", "contexts"):
                if "root_id" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN root_id TEXT")
            self.workspace_id = db.execute("SELECT value FROM config WHERE key='workspace_id'").fetchone()[0]
        self._database_initialized = True

    def _check_workspace_identity(self) -> None:
        if self._workspace_retired:
            if self._workspace_guard is not None:
                # The owned shared gate checks this latch before any metadata
                # and preserves its typed retirement across component entries.
                self._workspace_guard()
            raise HarmonyError("Workspace identity changed; this runtime is retired", 403)
        if self._workspace_guard is not None:
            self._workspace_guard()
            return
        if os.name == "nt" and self._workspace_identity[1] == 0:
            raise HarmonyError("Workspace identity metadata is unavailable", 503)
        try:
            for path in (*reversed(self.workspace.parents), self.workspace):
                info = path.lstat()
                if os.name == "nt" and info.st_ino == 0:
                    raise HarmonyError("Workspace identity metadata is unavailable", 503)
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not stat.S_ISDIR(info.st_mode)):
                    self._workspace_retired = True
                    raise HarmonyError("Workspace identity changed; this runtime is retired", 403)
            if (info.st_dev, info.st_ino) != self._workspace_identity:
                self._workspace_retired = True
                raise HarmonyError("Workspace identity changed; this runtime is retired", 403)
        except FileNotFoundError:
            self._workspace_retired = True
            raise HarmonyError("Workspace identity is unavailable; this runtime is retired", 403) from None
        except OSError:
            raise HarmonyError("Workspace identity could not be checked", 503) from None

    def _check_storage_parents(self) -> None:
        self._check_workspace_identity()
        for path in (self.directory, self.private):
            try:
                info = path.lstat()
            except FileNotFoundError:
                raise HarmonyError("Avalokita storage is unavailable", 409) from None
            except OSError:
                raise HarmonyError("Avalokita storage could not be checked", 503) from None
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise HarmonyError("Avalokita storage cannot use links or reparse points")
            if not stat.S_ISDIR(info.st_mode):
                raise HarmonyError("Avalokita storage must use real directories")

    def _check_database_files(self) -> None:
        for path in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            optional = path != self.db_path
            for sample in range(3):
                self._check_storage_parents()
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if optional or not self._database_initialized:
                        break
                    raise HarmonyError("Avalokita storage is unavailable", 409) from None
                except OSError:
                    raise HarmonyError("Avalokita storage could not be checked", 503) from None
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not stat.S_ISREG(info.st_mode)):
                    raise HarmonyError("Avalokita database must be an unlinked regular file")
                if info.st_nlink == 1:
                    break
                if os.name == "nt" and info.st_nlink == 0:
                    if not optional or sample == 2:
                        raise HarmonyError("Avalokita database metadata is unavailable", 503)
                    time.sleep(0.025)
                    continue
                raise HarmonyError("Avalokita database must be an unlinked regular file")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._check_database_files()
        target = self.db_path.as_uri() + "?mode=rw" if self._database_initialized else self.db_path
        self._check_workspace_identity()
        db = sqlite3.connect(target, uri=self._database_initialized, timeout=15, isolation_level=None)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=15000")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            self._check_database_files()
            self._check_workspace_identity()
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _pane(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["capabilities"] = json.loads(item["capabilities"])
        item["verified"] = bool(item["verified"])
        return item

    def _actor(self, db: sqlite3.Connection, actor: str) -> sqlite3.Row:
        _text(actor, "actor", 128)
        row = db.execute("SELECT * FROM panes WHERE id=?", (actor,)).fetchone()
        if row is None or not row["verified"]:
            raise HarmonyError("An authenticated, verified native pane is required", 403)
        return row

    def register_pane(self, id: str, provider: str, role: str, native_session_id: str,
                      capabilities: list[str], verified: bool = False) -> dict[str, Any]:
        """Trusted runtime sync; existing roles and native identity are preserved."""
        _text(id, "pane ID", 128)
        if id == "human":
            raise HarmonyError("The human namespace is reserved", 403)
        _text(provider, "provider", 64)
        _text(role, "role", 256)
        _text(native_session_id, "native session ID", 256)
        if not isinstance(verified, bool):
            raise HarmonyError("verified must be boolean")
        if not isinstance(capabilities, list) or len(capabilities) > 64:
            raise HarmonyError("capabilities must contain at most 64 strings")
        capabilities = sorted({_text(value, "capability", 128) for value in capabilities})
        now = time.time()
        namespace = ".avalokita/panes/" + _digest(id.encode("utf-8"))[:24]
        with self._connection(write=True) as db:
            current = db.execute("SELECT * FROM panes WHERE id=?", (id,)).fetchone()
            if current and (current["provider"] != provider or current["native_session_id"] != native_session_id):
                raise HarmonyError("A pane ID cannot be rebound to another native identity", 409)
            try:
                if current:
                    db.execute("UPDATE panes SET capabilities=?,verified=?,updated_at=? WHERE id=?",
                               (_json(capabilities), int(verified), now, id))
                else:
                    db.execute("INSERT INTO panes VALUES(?,?,?,?,?,?,?,?,?)",
                               (id, provider, role, native_session_id, _json(capabilities), int(verified),
                                namespace, now, now))
            except sqlite3.IntegrityError as exc:
                raise HarmonyError("This native conversation already has a pane", 409) from exc
            return self._pane(db.execute("SELECT * FROM panes WHERE id=?", (id,)).fetchone())

    @staticmethod
    def _goal(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item.pop("request_hash", None)
        item["root_id"] = item["id"]
        item["remaining"] = max(0, item["budget"] - item["used"])
        item["expired"] = item["deadline"] <= time.time()
        return item

    @staticmethod
    def _not_paused(db: sqlite3.Connection) -> None:
        if db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "1":
            raise HarmonyError("The human has paused peer mutations", 409)

    def _active_root(self, db: sqlite3.Connection, root_id: str | None,
                     *, require_budget: bool = True) -> sqlite3.Row:
        self._not_paused(db)
        if root_id is None:
            raise HarmonyError("Peer mutations require an existing human authority root", 403)
        _text(root_id, "human root ID", 128)
        root = db.execute("SELECT * FROM goals WHERE id=?", (root_id,)).fetchone()
        if root is None:
            raise HarmonyError("Unknown human root; peers cannot create authority", 403)
        if root["state"] != "active" or root["deadline"] <= time.time():
            raise HarmonyError("The human authority root is no longer active", 409)
        if require_budget and root["used"] >= root["budget"]:
            raise HarmonyError("The human root's peer message budget is exhausted", 409)
        return root

    def set_paused(self, paused: bool) -> dict[str, Any]:
        """Trusted human-control entry point; never expose to a peer credential."""
        if not isinstance(paused, bool):
            raise HarmonyError("paused must be boolean")
        with self._connection(write=True) as db:
            db.execute("UPDATE config SET value=? WHERE key='paused'", ("1" if paused else "0",))
        return {"paused": paused}

    def set_goal_state(self, root_id: str, state: str) -> dict[str, Any]:
        """Trusted human control; stopping is terminal and does not cancel native work."""
        _text(root_id, "human root ID", 128)
        if state not in {"active", "paused", "stopped"}:
            raise HarmonyError("Unknown goal state")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM goals WHERE id=?", (root_id,)).fetchone()
            if row is None:
                raise HarmonyError("Human root not found", 404)
            if row["state"] == "stopped" and state != "stopped":
                raise HarmonyError("A stopped root cannot be resumed", 409)
            db.execute("UPDATE goals SET state=? WHERE id=?", (state, root_id))
            return self._goal(db.execute("SELECT * FROM goals WHERE id=?", (root_id,)).fetchone())

    def get_goal(self, root_id: str) -> dict[str, Any]:
        """Exact authority lookup; UI snapshot pagination is never an auth index."""
        _text(root_id, "human root ID", 128)
        with self._connection() as db:
            row = db.execute("SELECT * FROM goals WHERE id=?", (root_id,)).fetchone()
            if row is None:
                raise HarmonyError("Human root not found", 404)
            return self._goal(row)

    def new_goal(self, prompt: str, budget: int = 24, *, idempotency_key: str | None = None,
                 ttl: float = 3600) -> dict[str, Any]:
        """Operator-only entry point, called only after real human input."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise HarmonyError("prompt must be nonempty selected text")
        self._selected_text(prompt)
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= 10000:
            raise HarmonyError("budget must be an integer between 1 and 10000")
        ttl = _number(ttl, "goal TTL", 1, 7 * 24 * 3600)
        if idempotency_key is not None:
            _text(idempotency_key, "idempotency key", 128)
        request_hash = _digest(_json([prompt, budget, ttl]).encode("utf-8"))
        now = time.time()
        with self._connection(write=True) as db:
            if idempotency_key:
                existing = db.execute("SELECT * FROM goals WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if existing:
                    if existing["request_hash"] != request_hash:
                        raise HarmonyError("Idempotency key already has a different human goal", 409)
                    return self._goal(existing)
            root = str(uuid.uuid4())
            db.execute("INSERT INTO goals VALUES(?,?,?,?,?,?,?,?,?)",
                       (root, prompt, budget, 0, "active", now + ttl, now, idempotency_key, request_hash))
            goal = self._goal(db.execute("SELECT * FROM goals WHERE id=?", (root,)).fetchone())
            goal["event"] = self._append(db, "human", None, "message", prompt, root, None,
                                          {"hop_budget": budget}, origin_type="human")
            return goal

    @staticmethod
    def _selected_text(body: str) -> None:
        if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise HarmonyError("Selected output exceeds the 64 KiB message budget")
        if "\x00" in body or _SENSITIVE_TEXT.search(body):
            raise HarmonyError("Credentials or binary/session dumps cannot be published", 403)

    @staticmethod
    def _metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        if metadata is None:
            return {}
        if not isinstance(metadata, dict):
            raise HarmonyError("metadata must be a JSON object")
        if len(_json(metadata).encode("utf-8")) > 8192:
            raise HarmonyError("metadata exceeds 8 KiB")

        def check(value: Any) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if not isinstance(key, str) or key.casefold() in _FORBIDDEN_METADATA:
                        raise HarmonyError("Identity, credentials and reasoning metadata are reserved", 403)
                    check(nested)
            elif isinstance(value, list):
                for nested in value:
                    check(nested)
            elif isinstance(value, str) and _SENSITIVE_TEXT.search(value):
                raise HarmonyError("Credentials cannot be published", 403)

        check(metadata)
        for name in ("idempotency_key", "correlation_id"):
            if metadata.get(name) is not None:
                _text(metadata[name], name, 128 if name == "idempotency_key" else 256)
        return json.loads(_json(metadata))

    def _append(self, db: sqlite3.Connection, actor: str, native_id: str | None, kind: str,
                body: str, root_id: str | None, recipient: str | None, metadata: dict[str, Any],
                *, origin_type: str = "native", observed_result: bool = False) -> dict[str, Any]:
        request_hash = _digest(_json([kind, body, root_id, recipient, metadata]).encode("utf-8"))
        key = metadata.get("idempotency_key")
        if key is not None:
            _text(key, "idempotency key", 128)
            existing = db.execute("SELECT * FROM events WHERE actor=? AND idempotency_key=?", (actor, key)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise HarmonyError("Idempotency key already has a different event", 409)
                return json.loads(existing["envelope"])
        root = db.execute("SELECT * FROM goals WHERE id=?", (root_id,)).fetchone() if root_id else None
        deadline = metadata.get("deadline", root["deadline"] if root else None)
        if deadline is not None:
            _number(deadline, "deadline", 0, 32503680000)
            if deadline <= time.time() and not observed_result:
                raise HarmonyError("Message deadline has expired", 409)
            if root and deadline > root["deadline"]:
                raise HarmonyError("A peer deadline cannot extend its human authority root", 403)
        budget = metadata.get("hop_budget", max(0, root["budget"] - root["used"]) if root else 0)
        if isinstance(budget, bool) or not isinstance(budget, int) or not 0 <= budget <= 10000:
            raise HarmonyError("hop_budget must be an integer between 0 and 10000")
        if root and budget > max(0, root["budget"] - root["used"]):
            raise HarmonyError("A peer cannot increase its root's remaining hop budget", 403)
        event = {
            "version": PROTOCOL, "id": str(uuid.uuid4()), "sequence": 0,
            "workspace_id": self.workspace_id, "root_id": root_id, "actor": actor,
            "native_session_id": native_id, "kind": kind, "body": body,
            "recipient": recipient, "created_at": time.time(), "deadline": deadline,
            "hop_budget": budget, "correlation_id": metadata.get("correlation_id", root_id),
            "idempotency_key": key, "visibility": "selected_public_output",
            "origin": {"type": origin_type, "actor": actor, "native_session_id": native_id,
                       "transport": "local", "verified_by": "runtime" if native_id else "human_console"},
            "metadata": metadata,
        }
        # BEGIN IMMEDIATE serializes allocation with the immutable payload insert.
        event["sequence"] = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        db.execute(
            "INSERT INTO events(seq,id,actor,kind,root_id,recipient,envelope,request_hash,idempotency_key,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event["sequence"], event["id"], actor, kind, root_id, recipient, _json(event),
             request_hash, key, event["created_at"]),
        )
        return event

    def import_selected_result(self, actor: str, body: str, *, root_id: str,
                               native_session_id: str, source_event_id: str,
                               source_job_id: str, source_created_at: float) -> dict[str, Any]:
        """Trusted runtime observation of an exact already-completed bus job.

        The bridge verifies the owned root/input/job/native callback before this
        internal entry point. It grants no peer authority and never debits,
        extends, resumes or recreates a goal. No HTTP peer operation exposes it.
        """
        self._selected_text(body)
        for value, name in ((root_id, "human root ID"), (source_event_id, "source event ID"),
                            (source_job_id, "source job ID"), (native_session_id, "native session ID")):
            _text(value, name, 128)
        _number(source_created_at, "source timestamp", 0, time.time() + 60)
        metadata = {"idempotency_key": "sharebus:" + source_event_id, "source": "sharebus_selected_output",
                    "source_created_at": source_created_at, "source_job_id": source_job_id,
                    "observed_completed_result": True, "hop_budget": 0}
        with self._connection(write=True) as db:
            pane = self._actor(db, actor)
            if pane["native_session_id"] != native_session_id:
                raise HarmonyError("Observed result is not from the registered native identity", 403)
            if db.execute("SELECT 1 FROM goals WHERE id=?", (root_id,)).fetchone() is None:
                raise HarmonyError("An observation cannot create a human root", 403)
            # Preserve an event imported by an older runtime before this narrow
            # observation path existed, rather than changing its immutable body.
            prior = db.execute("SELECT * FROM events WHERE actor=? AND idempotency_key=?", (actor, metadata["idempotency_key"])).fetchone()
            if prior:
                event = self._event(db, prior)
                if event["kind"] != "result" or event["body"] != body or event["root_id"] != root_id or event["native_session_id"] != native_session_id:
                    raise HarmonyError("Observed result identity already has different content", 409)
                return event
            return self._append(db, actor, native_session_id, "result", body, root_id, None, metadata,
                                origin_type="native", observed_result=True)

    def publish(self, actor: str, kind: str, body: str, *, root_id: str | None = None,
                recipient: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(kind, str) or kind not in KINDS:
            raise HarmonyError("Only selected public coordination event kinds are allowed")
        self._selected_text(body)
        metadata = self._metadata(metadata)
        if root_id is not None:
            _text(root_id, "human root ID", 128)
        with self._connection(write=True) as db:
            pane = self._actor(db, actor)
            if recipient is not None:
                _text(recipient, "recipient", 128)
                if recipient != "human" and db.execute("SELECT 1 FROM panes WHERE id=?", (recipient,)).fetchone() is None:
                    raise HarmonyError("Recipient is not a registered pane", 404)
            key = metadata.get("idempotency_key")
            if key is not None:
                existing = db.execute("SELECT * FROM events WHERE actor=? AND idempotency_key=?", (actor, key)).fetchone()
                if existing:
                    expected = _digest(_json([kind, body, root_id, recipient, metadata]).encode("utf-8"))
                    if existing["request_hash"] != expected:
                        raise HarmonyError("Idempotency key already has a different event", 409)
                    return self._event(db, existing)
            self._not_paused(db)
            root = self._active_root(db, root_id, require_budget=kind in {"message", "proposal", "question"}) if root_id else None
            if kind != "status" and root is None:
                raise HarmonyError("Peer work requires an existing human authority root", 403)
            if kind in {"message", "proposal", "question"}:
                if root["used"] >= root["budget"]:
                    raise HarmonyError("The human root's peer message budget is exhausted", 409)
                db.execute("UPDATE goals SET used=used+1 WHERE id=?", (root_id,))
            return self._append(db, actor, pane["native_session_id"], kind, body, root_id, recipient, metadata)

    @staticmethod
    def _event(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["envelope"])

    def capture(self, pane_id: str, after: int = 0, limit: int = 100,
                *, root_id: str | None = None) -> dict[str, Any]:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise HarmonyError("after must be a nonnegative sequence cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise HarmonyError("limit must be an integer between 1 and 1000")
        if root_id is not None:
            _text(root_id, "human root ID", 128)
        with self._connection() as db:
            if pane_id != "human" and db.execute("SELECT 1 FROM panes WHERE id=?", (pane_id,)).fetchone() is None:
                raise HarmonyError("Pane not found", 404)
            if root_id is None:
                rows = db.execute("SELECT * FROM events WHERE actor=? AND seq>? ORDER BY seq LIMIT ?",
                                  (pane_id, after, limit + 1)).fetchall()
            else:
                rows = db.execute("SELECT * FROM events WHERE actor=? AND seq>? AND root_id=? ORDER BY seq LIMIT ?",
                                  (pane_id, after, root_id, limit + 1)).fetchall()
            messages = [self._event(db, row) for row in rows[:limit]]
            return {"version": PROTOCOL, "workspace_id": self.workspace_id, "pane_id": pane_id,
                    "root_id": root_id,
                    "messages": messages, "after": after,
                    "next_cursor": messages[-1]["sequence"] if messages else after,
                    "has_more": len(rows) > limit, "truncated": len(rows) > limit,
                    "cursor_truncated": False, "retention": "durable_no_automatic_pruning",
                    "visibility": "selected_public_output"}

    def _path(self, value: str, *, resource: bool = False) -> tuple[Path, str, str]:
        self._check_workspace_identity()
        _text(value, "workspace path", 4096)
        raw = value
        if resource and "::" in raw:
            raw, symbol = raw.split("::", 1)
            _text(symbol, "symbol selector", 256)
        raw = raw.replace("\\", "/")
        if raw.startswith(("//", "//?/", "//./")) or "\x00" in raw:
            raise HarmonyError("Device and network paths cannot be shared", 403)
        windows = PureWindowsPath(raw)
        if os.name != "nt" and windows.drive:
            raise HarmonyError("A Windows absolute path cannot be resolved on this platform", 403)
        parts = Path(raw).parts
        if ".." in parts:
            raise HarmonyError("Parent traversal is not allowed", 403)
        # Windows abspath may trim terminal spaces/dots. Reject the original
        # spelling before normalization can collapse it onto another name.
        for part in parts:
            if part != Path(raw).anchor and (":" in part or part.rstrip(" .") != part
                                             or PureWindowsPath(part).is_reserved()):
                raise HarmonyError("Ambiguous Windows paths cannot be shared", 403)
        path = Path(raw)
        if not path.is_absolute():
            path = self.workspace / path
        path = Path(os.path.abspath(path))
        def guarded_relative(candidate: Path) -> Path:
            try:
                relative = candidate.relative_to(self.workspace)
            except ValueError as exc:
                raise HarmonyError("Path is outside this workspace", 403) from exc
            for part in relative.parts:
                lowered = part.casefold()
                if (":" in part or part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
                        or lowered in _PRIVATE_PARTS or lowered in _SECRET_NAMES
                        or lowered == ".env" or lowered.startswith(".env.")
                        or Path(lowered).suffix in _SECRET_SUFFIXES):
                    raise HarmonyError("Private, credential or ambiguous paths cannot be shared", 403)
            cursor = self.workspace
            for part in relative.parts:
                cursor = cursor / part
                self._check_workspace_identity()
                if _linked(cursor):
                    raise HarmonyError("Symbolic links and reparse points cannot be shared", 403)
                self._check_workspace_identity()
                exists = cursor.exists()
                self._check_workspace_identity()
                if exists:
                    regular = cursor.is_file()
                    self._check_workspace_identity()
                    if regular and cursor.stat().st_nlink != 1:
                        raise HarmonyError("Hard-linked files cannot be shared", 403)
                self._check_workspace_identity()
            return relative

        # Check the lexical path before resolution, so resolution never grants
        # permission to traverse a junction. On Windows resolve expands 8.3
        # names in existing files/ancestors, including a nonexistent child tail.
        guarded_relative(path)
        self._check_workspace_identity()
        try:
            path = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise HarmonyError("Workspace path could not be safely resolved", 403) from exc
        relative = guarded_relative(path)
        display = relative.as_posix() if relative.parts else "."
        return path, display.casefold().rstrip("/") if display != "." else "", display

    @staticmethod
    def _overlap(left: str, right: str) -> bool:
        return not left or not right or left == right or left.startswith(right + "/") or right.startswith(left + "/")

    @staticmethod
    def _lease(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["lease_id"] = item["id"]
        item["resources"] = json.loads(item["resources"])
        item["canonical_resources"] = json.loads(item.pop("resource_keys"))
        item["expired"] = item["expires_at"] <= time.time()
        item["native_writes_are_advisory"] = True
        return item

    def get_lease(self, lease_id: str) -> dict[str, Any]:
        """Exact durable lease lookup, including records outside the UI page."""
        _text(lease_id, "lease ID", 128)
        with self._connection() as db:
            row = db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if row is None:
                raise HarmonyError("Lease not found", 404)
            return self._lease(row)

    @staticmethod
    def _next(db: sqlite3.Connection, name: str) -> int:
        db.execute("UPDATE config SET value=CAST(value AS INTEGER)+1 WHERE key=?", (name,))
        return int(db.execute("SELECT value FROM config WHERE key=?", (name,)).fetchone()[0])

    def claim(self, actor: str, resources: list[str], ttl: float = DEFAULT_LEASE_TTL,
              *, root_id: str | None = None) -> dict[str, Any]:
        ttl = _number(ttl, "lease TTL", 0.01, MAX_LEASE_TTL)
        if not isinstance(resources, list) or not 1 <= len(resources) <= 64:
            raise HarmonyError("A lease needs between 1 and 64 resource paths")
        checked = [self._path(value, resource=True) for value in resources]
        keys = sorted({key for _, key, _ in checked})
        displays = [next(display for _, candidate, display in checked if candidate == key) for key in keys]
        now = time.time()
        with self._connection(write=True) as db:
            self._actor(db, actor)
            root = self._active_root(db, root_id)
            now = time.time()
            db.execute("UPDATE leases SET state='expired',updated_at=? WHERE state='active' AND expires_at<=?", (now, now))
            for row in db.execute("SELECT * FROM leases WHERE state='active'").fetchall():
                if any(self._overlap(key, other) for key in keys for other in json.loads(row["resource_keys"])):
                    raise HarmonyError("Resource is held by an active lease", 409)
            lease_id = str(uuid.uuid4())
            fence = self._next(db, "fence")
            db.execute("INSERT INTO leases VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (lease_id, actor, _json(displays), _json(keys), fence, min(now + ttl, root["deadline"]), "active", now, now, root_id))
            return self._lease(db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())

    def _current_lease(self, db: sqlite3.Connection, actor: str, lease_id: str, fence: int,
                       *, root_id: str | None = None) -> sqlite3.Row:
        self._actor(db, actor)
        if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
            raise HarmonyError("fence must be a positive integer")
        row = db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
        if row is None:
            raise HarmonyError("Lease not found", 404)
        if row["actor"] != actor:
            raise HarmonyError("A pane can change only its own lease", 403)
        if root_id is not None and row["root_id"] != root_id:
            raise HarmonyError("Lease belongs to another human root", 403)
        self._active_root(db, row["root_id"])
        if row["fence"] != fence or row["state"] != "active" or row["expires_at"] <= time.time():
            raise HarmonyError("Lease fencing token is stale or expired", 409)
        return row

    def renew(self, actor: str, lease_id: str, fence: int, ttl: float = DEFAULT_LEASE_TTL,
              *, root_id: str | None = None) -> dict[str, Any]:
        ttl = _number(ttl, "lease TTL", 0.01, MAX_LEASE_TTL)
        with self._connection(write=True) as db:
            lease = self._current_lease(db, actor, lease_id, fence, root_id=root_id)
            root = self._active_root(db, lease["root_id"])
            now = time.time()
            db.execute("UPDATE leases SET expires_at=?,updated_at=? WHERE id=?", (min(now + ttl, root["deadline"]), now, lease_id))
            return self._lease(db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())

    def handoff(self, actor: str, lease_id: str, fence: int, to: str,
                *, root_id: str | None = None) -> dict[str, Any]:
        with self._connection(write=True) as db:
            self._current_lease(db, actor, lease_id, fence, root_id=root_id)
            self._actor(db, to)
            if actor == to:
                raise HarmonyError("Handoff requires another pane")
            next_fence = self._next(db, "fence")
            db.execute("UPDATE leases SET actor=?,fence=?,updated_at=? WHERE id=?",
                       (to, next_fence, time.time(), lease_id))
            return self._lease(db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())

    def release(self, actor: str, lease_id: str, fence: int,
                *, root_id: str | None = None) -> dict[str, Any]:
        with self._connection(write=True) as db:
            self._current_lease(db, actor, lease_id, fence, root_id=root_id)
            db.execute("UPDATE leases SET state='released',updated_at=? WHERE id=?", (time.time(), lease_id))
            return self._lease(db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone())

    def commit_if_current(self, actor: str, lease_id: str, fence: int, path: str,
                          expected_sha256: str | None, content: str | bytes,
                          *, root_id: str | None = None) -> dict[str, Any]:
        """Atomic replace + CAS among cooperating gates; direct native writes remain advisory."""
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                                           or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
            raise HarmonyError("expected_sha256 must be a lowercase SHA-256 or null for a new file")
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, bytes):
            data = content
        else:
            raise HarmonyError("content must be text or bytes")
        if len(data) > MAX_CONTEXT_BYTES:
            raise HarmonyError("A gated commit exceeds the 2 MiB byte budget")
        target, key, display = self._path(path)
        self._check_workspace_identity()
        if target == self.workspace or not target.parent.is_dir():
            raise HarmonyError("Commit parent must be an existing workspace directory")
        # Failed operations retain their bounded temporary file. Retrying lexical
        # cleanup after a nested guard refusal could touch a replacement root.
        with self._connection(write=True) as db:
            lease = self._current_lease(db, actor, lease_id, fence, root_id=root_id)
            if not any(not parent or key == parent or key.startswith(parent + "/")
                       for parent in json.loads(lease["resource_keys"])):
                raise HarmonyError("The lease does not cover this file", 403)
            self._path(path)
            self._check_workspace_identity()
            exists = target.exists()
            if exists:
                self._check_workspace_identity()
                if not target.is_file():
                    raise HarmonyError("Commit target must be a regular file")
            self._check_workspace_identity()
            old = _digest(target.read_bytes()) if exists else None
            if old != expected_sha256:
                raise HarmonyError("File content changed since the expected snapshot", 409)
            temporary = target.parent / (".avalokita-write-" + str(uuid.uuid4()) + ".tmp")
            self._check_workspace_identity()
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                self._check_workspace_identity()
                stream.write(data)
                self._check_workspace_identity()
                stream.flush()
                self._check_workspace_identity()
                os.fsync(stream.fileno())
            self._path(path)
            self._current_lease(db, actor, lease_id, fence, root_id=root_id)
            self._check_workspace_identity()
            exists = target.exists()
            self._check_workspace_identity()
            current = _digest(target.read_bytes()) if exists else None
            if current != expected_sha256:
                raise HarmonyError("File changed while preparing the atomic replacement", 409)
            self._check_workspace_identity()
            os.replace(temporary, target)
            return {"path": display, "sha256": _digest(data), "bytes": len(data),
                    "lease_id": lease_id, "fence": fence, "actor": actor,
                    "atomic_replace": True, "cooperative_cas": True,
                    "native_writes_are_advisory": True}

    def publish_context(self, actor: str, paths: list[str], label: str = "",
                        *, root_id: str | None = None) -> dict[str, Any]:
        if not isinstance(label, str) or len(label) > 256:
            raise HarmonyError("Context label must contain at most 256 characters")
        self._selected_text(label)
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_CONTEXT_FILES:
            raise HarmonyError("A context must select between 1 and 32 files")
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        with self._connection(write=True) as db:
            self._actor(db, actor)
            self._active_root(db, root_id)
            for value in paths:
                path, key, display = self._path(value)
                if key in seen:
                    raise HarmonyError("Context paths must be unique after canonicalization")
                seen.add(key)
                self._check_workspace_identity()
                if not path.is_file():
                    raise HarmonyError("Context paths must select regular files", 404)
                self._check_workspace_identity()
                before = path.stat()
                self._check_workspace_identity()
                if before.st_size > MAX_CONTEXT_FILE_BYTES:
                    raise HarmonyError("A context file exceeds 512 KiB")
                self._check_workspace_identity()
                with path.open("rb") as stream:
                    self._check_workspace_identity()
                    data = stream.read(MAX_CONTEXT_FILE_BYTES + 1)
                    self._check_workspace_identity()
                self._check_workspace_identity()
                after = path.stat()
                self._check_workspace_identity()
                self._path(value)
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise HarmonyError("Context changed while it was being captured", 409)
                total += len(data)
                if len(data) > MAX_CONTEXT_FILE_BYTES or total > MAX_CONTEXT_BYTES:
                    raise HarmonyError("Selected context exceeds its byte budget")
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise HarmonyError("Only selected UTF-8 text context is supported") from exc
                if "\x00" in content or _SENSITIVE_TEXT.search(content):
                    raise HarmonyError("Credential and binary context cannot be published", 403)
                files.append({"path": display, "canonical_path": key, "sha256": _digest(data),
                              "bytes": len(data), "mtime_ns": after.st_mtime_ns, "content": content})
            generation = self._next(db, "context_generation")
            context_id = str(uuid.uuid4())
            manifest_hash = _digest(_json([{key: value for key, value in item.items() if key != "content"}
                                           for item in files]).encode("utf-8"))
            self._active_root(db, root_id)
            db.execute("INSERT INTO contexts VALUES(?,?,?,?,?,?,?,?,?)",
                       (context_id, actor, label, generation, _json(files), total, manifest_hash, time.time(), root_id))
        return self.read_context(context_id)

    def read_context(self, snapshot_id: str, *, root_id: str | None = None) -> dict[str, Any]:
        if root_id is not None:
            _text(root_id, "human root ID", 128)
        with self._connection() as db:
            row = db.execute("SELECT * FROM contexts WHERE id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise HarmonyError("Context snapshot not found", 404)
            if root_id is not None and row["root_id"] != root_id:
                raise HarmonyError("Context belongs to another human root", 403)
            context = dict(row)
            context["snapshot_id"] = context["id"]
            context["files"] = json.loads(context["files"])
            latest = int(db.execute("SELECT value FROM config WHERE key='context_generation'").fetchone()[0])
            stale: list[str] = []
            for item in context["files"]:
                try:
                    path, _, _ = self._path(item["path"])
                    fresh = False
                    self._check_workspace_identity()
                    regular = path.is_file()
                    self._check_workspace_identity()
                    if regular:
                        info = path.stat()
                        self._check_workspace_identity()
                        if info.st_size <= MAX_CONTEXT_FILE_BYTES:
                            self._check_workspace_identity()
                            with path.open("rb") as stream:
                                self._check_workspace_identity()
                                current = stream.read(MAX_CONTEXT_FILE_BYTES + 1)
                                self._check_workspace_identity()
                            fresh = len(current) <= MAX_CONTEXT_FILE_BYTES and _digest(current) == item["sha256"]
                except HarmonyError as error:
                    if self._workspace_retired or error.status == 503:
                        raise
                    fresh = False
                except OSError:
                    fresh = False
                item["fresh"] = fresh
                if not fresh:
                    stale.append(item["path"])
            context.update({"version": PROTOCOL, "workspace_id": self.workspace_id,
                            "fresh": not stale, "stale_paths": stale, "requires_refresh": bool(stale),
                            "latest_generation": latest, "is_latest": context["generation"] == latest,
                            "byte_budget": MAX_CONTEXT_BYTES, "scope": "selected_text_files",
                            "shared_ast": False})
            return context

    def snapshot(self) -> dict[str, Any]:
        with self._connection() as db:
            panes = [self._pane(row) for row in db.execute("SELECT * FROM panes ORDER BY created_at")]
            goals = [self._goal(row) for row in db.execute("SELECT * FROM goals ORDER BY created_at DESC LIMIT 50")]
            rows = db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 100").fetchall()
            messages = [self._event(db, row) for row in reversed(rows)]
            leases = [self._lease(row) for row in db.execute("SELECT * FROM leases ORDER BY created_at DESC LIMIT 200")]
            contexts = [dict(row) for row in db.execute(
                "SELECT id,actor,label,generation,total_bytes,manifest_sha256,created_at,root_id FROM contexts ORDER BY generation DESC LIMIT 50"
            )]
            cursor = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
            return {"version": PROTOCOL, "workspace_id": self.workspace_id,
                    "paused": db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "1",
                    "workspace": str(self.workspace), "panes": panes, "goals": goals,
                    "messages": messages, "leases": leases, "contexts": contexts, "cursor": cursor,
                    "truncated": cursor > len(messages), "remote_transport_implemented": False,
                    "native_writes_are_advisory": True}
