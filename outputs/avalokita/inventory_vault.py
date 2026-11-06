"""Private, durable observations and learning evidence, never bus authority.

Provider histories are not copied here. This store accepts only the bounded
inventory projection and numeric preference evidence described by its schema.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import sys
import threading
import time


SCHEMA = "avalokita.inventory-vault/1"
MAX_INVENTORY = 4096
MAX_EVIDENCE = 100000
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
FEATURE_DIMENSION = 64
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_FORBIDDEN = {"text", "prompt", "messages", "transcript", "reasoning", "thinking",
              "analysis", "system", "developer", "token", "tokens", "api_key",
              "authorization", "cookie", "cookies", "headers", "env", "command",
              "args", "url", "download_url", "password", "credentials", "context_file",
              "native_session_id", "session_token", "root_id", "job_id"}
_SECRET = re.compile(r"https?://|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
                     r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|"
                     r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b|"
                     r"-----BEGIN [A-Z ]*PRIVATE KEY-----|authorization\s*:\s*bearer", re.I)
_OBSERVATION_FIELDS = {"id", "kind", "provider", "label", "device_key", "workspace_key",
                       "status", "observed_at", "expires_at", "verified", "provenance"}
_SAMPLE_FIELDS = {"id", "source_kind", "source_ref", "event_ref", "observed_at", "scope",
                  "provider", "purpose", "capability", "features", "target", "weight"}
SOURCE_STATUSES = {"ready", "empty", "missing", "unsupported", "unavailable", "partial",
                   "error", "stale", "storage_full", "closed"}
OBSERVATION_STATUSES = {"observed", "configured", "installed", "available", "unverified",
                        "unavailable", "unsupported", "stale", "missing"}


class InventoryError(ValueError):
    """A fixed, non-secret error code suitable for a controller boundary."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def opaque(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _ordinary_directory(path: Path) -> None:
    for current in (path, *path.parents):
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or current.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise InventoryError("linked_directory")


def _make_private_directory(path: Path) -> None:
    if not path.is_absolute():
        raise InventoryError("storage_path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _ordinary_directory(current)


def _regular_file(path: Path) -> os.stat_result:
    _ordinary_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise InventoryError("linked_file")
    return info


def _finite(value, *, minimum=0, maximum=10**15) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise InventoryError("invalid_number")
    return float(value)


def _key(value: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value) or _SECRET.search(value):
        raise InventoryError("invalid_key")
    return value


def _slug(value: str) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise InventoryError("invalid_label")
    return value


def _hash(value: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise InventoryError("invalid_reference")
    return value


def _document(value: dict, *, maximum=MAX_DOCUMENT_BYTES) -> tuple[dict, str]:
    """Validate before encoding; arbitrary source payloads are not documents."""
    def visit(item, depth=0):
        if depth > 16:
            raise InventoryError("document_depth")
        if isinstance(item, dict):
            if len(item) > 4096:
                raise InventoryError("document_size")
            for name, child in item.items():
                if (not isinstance(name, str) or len(name) > 160 or name.lower() in _FORBIDDEN
                        or any(term in name.lower() for term in ("secret", "password", "credential"))):
                    raise InventoryError("forbidden_field")
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 20000:
                raise InventoryError("document_size")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str):
            if len(item) > 4096 or _SECRET.search(item) or any(ord(c) < 32 for c in item):
                raise InventoryError("unsafe_text")
        elif item is None or type(item) is bool:
            pass
        elif type(item) in (int, float):
            if not math.isfinite(item):
                raise InventoryError("invalid_number")
        else:
            raise InventoryError("invalid_document")
    visit(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > maximum:
            raise InventoryError("document_size")
        return json.loads(encoded), encoded
    except (UnicodeError, TypeError, OverflowError):
        raise InventoryError("invalid_document") from None


class PrivateVault:
    """One captured workspace's private inventory/evidence/checkpoint partition."""
    def __init__(self, workspace: Path, *, home: Path | None = None, data_root: Path | None = None):
        self._lock = threading.RLock()
        self._closed = False
        candidate = Path(workspace).absolute()
        _ordinary_directory(candidate)
        self.workspace = candidate.resolve(strict=True)
        info = self.workspace.stat()
        if os.name == "nt" and not info.st_ino:
            raise InventoryError("workspace_identity")
        self._workspace_identity = info.st_dev, info.st_ino
        self.workspace_key = opaque(json.dumps([os.path.normcase(str(self.workspace)), *self._workspace_identity]))
        self.home = Path(home).absolute() if home is not None else Path.home().absolute()
        self.home_explicit = home is not None
        _ordinary_directory(self.home)
        self.device_key = opaque("avalokita-device-v1\0" + os.name + "\0" + socket.gethostname()
                                 + "\0" + os.path.normcase(str(self.home)))
        if data_root is None:
            if home is not None:
                root = self.home / ".avalokita" / "private" / "vault"
            elif os.name == "nt":
                root = Path(os.environ.get("LOCALAPPDATA", str(self.home / "AppData/Local"))) / "Avalokita/private/vault"
            elif sys.platform == "darwin":
                root = self.home / "Library/Application Support/Avalokita/private/vault"
            else:
                root = Path(os.environ.get("XDG_DATA_HOME", str(self.home / ".local/share"))) / "avalokita/private/vault"
        else:
            root = Path(data_root)
        self.data_root = root.absolute()
        self.directory = self.data_root / self.workspace_key
        _make_private_directory(self.directory)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass
        self._directory_identities = {
            path: (path.stat().st_dev, path.stat().st_ino)
            for path in (self.data_root, self.directory)
        }
        self.db_path = self.directory / "inventory.sqlite3"
        try:
            descriptor = os.open(self.db_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        info = _regular_file(self.db_path)
        self._database_identity = info.st_dev, info.st_ino
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (
                    source_key TEXT PRIMARY KEY,status TEXT NOT NULL,observed_at REAL NOT NULL,
                    records INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS inventory (
                    source_key TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,
                    retired INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(source_key,id));
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,payload TEXT NOT NULL,payload_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors (source_key TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    name TEXT PRIMARY KEY,payload TEXT NOT NULL,payload_hash TEXT NOT NULL,revision INTEGER NOT NULL);
            """)
            old = db.execute("SELECT value FROM meta WHERE key='workspace_key'").fetchone()
            if old is not None and old[0] != self.workspace_key:
                raise InventoryError("workspace_mismatch")
            db.execute("INSERT OR IGNORE INTO meta VALUES('workspace_key',?)", (self.workspace_key,))
            db.execute("INSERT OR IGNORE INTO meta VALUES('revision','0')")
            db.execute("INSERT OR IGNORE INTO meta VALUES('evidence_revision','0')")

    def guard_workspace(self) -> None:
        try:
            _ordinary_directory(self.workspace)
            info = self.workspace.stat()
            if (info.st_dev, info.st_ino) != self._workspace_identity:
                raise InventoryError("workspace_changed")
        except OSError:
            raise InventoryError("workspace_changed") from None

    def _guard(self) -> None:
        if self._closed:
            raise InventoryError("closed")
        self.guard_workspace()
        try:
            for path, identity in self._directory_identities.items():
                _ordinary_directory(path)
                info = path.stat()
                if (info.st_dev, info.st_ino) != identity:
                    raise InventoryError("storage_changed")
            info = _regular_file(self.db_path)
            if (info.st_dev, info.st_ino) != self._database_identity:
                raise InventoryError("storage_changed")
            for suffix in ("-journal", "-wal", "-shm"):
                path = self.db_path.with_name(self.db_path.name + suffix)
                if path.exists() or path.is_symlink():
                    _regular_file(path)
        except OSError:
            raise InventoryError("storage_changed") from None

    @contextmanager
    def _connection(self):
        with self._lock:
            self._guard()
            db = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
                yield db
                self._guard()
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _bump(db, *, evidence=False):
        db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        if evidence:
            db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='evidence_revision'")

    def _observation(self, item: dict) -> tuple[dict, str]:
        if not isinstance(item, dict) or set(item) != _OBSERVATION_FIELDS:
            raise InventoryError("inventory_schema")
        if item["device_key"] != self.device_key or item["workspace_key"] != self.workspace_key:
            raise InventoryError("inventory_scope")
        _key(item["id"])
        _slug(item["kind"])
        _slug(item["provider"])
        if (not isinstance(item["label"], str) or not 1 <= len(item["label"]) <= 80
                or any(c in item["label"] for c in "\\/")):
            raise InventoryError("invalid_label")
        if item["status"] not in OBSERVATION_STATUSES or type(item["verified"]) is not bool:
            raise InventoryError("inventory_state")
        observed = _finite(item["observed_at"])
        expiry = _finite(item["expires_at"])
        if not observed <= expiry <= observed + 31 * 24 * 3600:
            raise InventoryError("inventory_expiry")
        provenance = item["provenance"]
        if not isinstance(provenance, dict) or set(provenance) != {"source_kind", "source_ref", "source_hash"}:
            raise InventoryError("provenance_schema")
        _slug(provenance["source_kind"])
        _hash(provenance["source_ref"])
        _hash(provenance["source_hash"])
        return _document(item)

    def put_inventory(self, source_key: str, observations: list[dict], *, status: str, observed_at: float) -> int:
        _key(source_key)
        _finite(observed_at)
        if status not in SOURCE_STATUSES or not isinstance(observations, list) or len(observations) > MAX_INVENTORY:
            raise InventoryError("inventory_schema")
        prepared = [self._observation(item) for item in observations]
        if len({item[0]["id"] for item in prepared}) != len(prepared):
            raise InventoryError("duplicate_observation")
        with self._connection() as db:
            # Failed/incremental attempts retain the last observed snapshot.
            # Only a completed observation can establish that a former item left.
            if status in {"ready", "empty", "missing"}:
                db.execute("DELETE FROM inventory WHERE source_key=?", (source_key,))
            for item, encoded in prepared:
                db.execute("INSERT INTO inventory(source_key,id,payload,retired) VALUES (?,?,?,0) "
                           "ON CONFLICT(source_key,id) DO UPDATE SET payload=excluded.payload,retired=0",
                           (source_key, item["id"], encoded))
            count = db.execute("SELECT COUNT(*) FROM inventory WHERE source_key=? AND retired=0", (source_key,)).fetchone()[0]
            db.execute("INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET "
                       "status=excluded.status,observed_at=excluded.observed_at,records=excluded.records",
                       (source_key, status, observed_at, count))
            self._bump(db)
        return len(prepared)

    def put_evidence(self, sample: dict) -> bool:
        if not isinstance(sample, dict) or set(sample) != _SAMPLE_FIELDS:
            raise InventoryError("evidence_schema")
        _key(sample["id"])
        if sample["source_kind"] not in {"human_explicit", "observed_usage", "verified_outcome"}:
            raise InventoryError("evidence_source")
        for name in ("source_ref", "event_ref"):
            _hash(sample[name])
        observed = _finite(sample["observed_at"])
        if observed > time.time() + 300:
            raise InventoryError("future_evidence")
        if sample["scope"] not in {self.workspace_key, "global_style"}:
            raise InventoryError("evidence_scope")
        for name in ("provider", "purpose"):
            _slug(sample[name])
        if sample["capability"] is not None:
            _slug(sample["capability"])
        features = sample["features"]
        if (not isinstance(features, list) or len(features) != FEATURE_DIMENSION
                or any(type(x) not in (int, float) or not math.isfinite(x) or not -1 <= x <= 1 for x in features)
                or features[0] != 1 or sum(x*x for x in features[1:]) > 1.000001):
            raise InventoryError("evidence_features")
        target = sample["target"]
        if target is not None and (type(target) not in (int, float) or target not in (0, 1)):
            raise InventoryError("evidence_target")
        weight = _finite(sample["weight"], maximum=4)
        if sample["source_kind"] == "observed_usage" and (target not in (1, None) or weight > 0.25):
            raise InventoryError("inferred_evidence_weight")
        normalized, encoded = _document(sample)
        with self._connection() as db:
            old = db.execute("SELECT payload_hash FROM evidence WHERE id=?", (normalized["id"],)).fetchone()
            digest = opaque(encoded)
            if old:
                if old[0] != digest:
                    raise InventoryError("evidence_conflict")
                return False
            if db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] >= MAX_EVIDENCE:
                raise InventoryError("storage_full")
            db.execute("INSERT INTO evidence VALUES (?,?,?)", (normalized["id"], encoded, digest))
            self._bump(db, evidence=True)
        return True

    def preference_samples(self) -> list[dict]:
        with self._connection() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM evidence ORDER BY rowid")]

    def get_cursor(self, source_key: str) -> dict | None:
        _key(source_key)
        with self._connection() as db:
            row = db.execute("SELECT payload FROM cursors WHERE source_key=?", (source_key,)).fetchone()
            return json.loads(row[0]) if row else None

    def set_cursor(self, source_key: str, cursor: dict) -> None:
        _key(source_key)
        if not isinstance(cursor, dict):
            raise InventoryError("cursor_schema")
        _, encoded = _document(cursor, maximum=128 * 1024)
        with self._connection() as db:
            db.execute("INSERT INTO cursors VALUES (?,?) ON CONFLICT(source_key) DO UPDATE SET payload=excluded.payload",
                       (source_key, encoded))

    def get_checkpoint(self, name: str) -> dict | None:
        _key(name)
        with self._connection() as db:
            row = db.execute("SELECT payload FROM checkpoints WHERE name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else None

    def put_checkpoint(self, name: str, checkpoint: dict) -> None:
        _key(name)
        if not isinstance(checkpoint, dict):
            raise InventoryError("checkpoint_schema")
        _, encoded = _document(checkpoint)
        digest = opaque(encoded)
        with self._connection() as db:
            old = db.execute("SELECT payload_hash FROM checkpoints WHERE name=?", (name,)).fetchone()
            if old and old[0] == digest:
                return
            db.execute("INSERT INTO checkpoints VALUES (?,?,?,1) ON CONFLICT(name) DO UPDATE SET "
                       "payload=excluded.payload,payload_hash=excluded.payload_hash,revision=checkpoints.revision+1",
                       (name, encoded, digest))
            self._bump(db)

    def summary(self) -> dict:
        with self._connection() as db:
            meta = dict(db.execute("SELECT key,value FROM meta"))
            sources = [dict(row) for row in db.execute(
                "SELECT source_key AS source,status,observed_at,records FROM sources ORDER BY source_key LIMIT 128")]
            rows = db.execute("SELECT payload FROM inventory WHERE retired=0").fetchall()
            stale = sum(json.loads(row[0])["expires_at"] < time.time() for row in rows)
            return {"schema": SCHEMA, "revision": int(meta["revision"]),
                    "evidence_revision": int(meta["evidence_revision"]), "observations": len(rows),
                    "stale_observations": stale, "evidence_samples": db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0],
                    "checkpoints": db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0],
                    "sources": sources,
                    "model_revisions": dict(db.execute("SELECT name,revision FROM checkpoints ORDER BY name"))}

    def close(self) -> None:
        with self._lock:
            self._closed = True
