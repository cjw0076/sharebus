"""Private, durable state for the app-owned help conversation, outside bus queues."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
import uuid


TEXT_LIMIT = 12000
REPLY_LIMIT = 32000
TURN_SECONDS = 180
SPEC_VERSION = "support-v1"
PURPOSE = "sharebus-native-support-v1"
PROFILE_REASONS = frozenset({"native_missing", "unsupported_version", "profile_unverified"})
ERRORS = {
    "workspace": "선택한 작업실이 바뀌었습니다. 폴더를 다시 확인해 주세요.",
    "storage": "도움 대화의 저장 공간을 확인하지 못했습니다.",
    "busy": "앞서 보낸 도움 요청을 확인하고 있습니다.",
    "unknown": "앞선 요청의 처리 결과를 확인하지 못했습니다. 다시 보내지 않았습니다.",
    "stale": "이 도움 대화의 연결을 확인해야 합니다. 새 대화로 바꾸지 않았습니다.",
    "provider": "이 버전의 도움 대화는 지원되는 Claude 앱으로만 열 수 있습니다.",
    "input": "도움받을 내용을 입력해 주세요. 너무 긴 내용은 나누어 보내 주세요.",
    "duplicate": "이 요청은 이미 다른 내용으로 접수되었습니다. 다시 보내지 않았습니다.",
    "auth": "도움 대화의 연결을 확인하지 못했습니다. 앱에서 도움을 다시 열어 주세요.",
    "request": "이 요청을 처리할 수 없습니다.",
    "connection": "도움 대화를 열지 못했습니다. 잠시 후 앱에서 다시 열어 주세요.",
    "native": "도움 요청을 마치지 못했습니다. 앱 연결 상태를 확인해 주세요.",
    "approval": "이 요청은 앱에서 사용자의 확인이 필요합니다. 자동으로 승인하지 않았습니다.",
}


class SupportError(RuntimeError):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(ERRORS[code])


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def check_directory(path: Path) -> None:
    for current in (path, *path.parents):
        info = current.lstat()
        if (not stat.S_ISDIR(info.st_mode) or current.is_symlink()
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise SupportError("storage", 403)


def make_directory(path: Path) -> None:
    if not path.is_absolute():
        raise SupportError("storage", 403)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        check_directory(current)


def read_private(path: Path, *, maximum: int = 65536) -> str:
    check_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > maximum):
        raise SupportError("storage", 403)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
            raise SupportError("storage", 403)
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise SupportError("storage", 403)
    return raw.decode("utf-8")


def write_private(path: Path, value: str, *, exclusive: bool = False) -> None:
    check_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_private(path, maximum=2 * 1024 * 1024)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    temporary = path if exclusive else path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(value)
            target.flush()
            os.fsync(target.fileno())
        if not exclusive:
            os.replace(temporary, path)
    finally:
        if not exclusive and temporary.exists():
            temporary.unlink()


class SupportPaths:
    """Capture the selected directory before making any support-owned files."""
    def __init__(self, workspace: Path, *, home: Path | None = None, root: Path | None = None):
        # Imported lazily: the click launcher imports SupportClient in its action.
        from launcher import WorkspaceSelection
        try:
            self.selection = WorkspaceSelection.capture(str(Path(workspace).absolute()))
            base = Path(root) if root is not None else Path(home or Path.home()) / ".sharebus" / "support"
            self.root = base.absolute()
            if self.root.resolve() != self.root or self.root.is_relative_to(self.selection.path):
                raise SupportError("storage", 403)
            self.key = digest(os.path.normcase(str(self.selection.path)))
            self.directory = self.root / self.key
            self.private = self.directory / ".sharebus" / "private"
            self._identities: dict[Path, tuple[int, int]] = {}
        except (OSError, ValueError):
            raise SupportError("workspace", 403) from None

    def target_check(self) -> None:
        try:
            self.selection.check()
        except (OSError, RuntimeError, ValueError):
            raise SupportError("workspace", 403) from None

    def ensure(self) -> None:
        self.target_check()
        make_directory(self.private)
        self.check()

    def check(self) -> None:
        try:
            check_directory(self.private)
            for path in (self.root, self.directory, self.directory / ".sharebus", self.private):
                info = path.stat()
                identity = info.st_dev, info.st_ino
                if path in self._identities and self._identities[path] != identity:
                    raise SupportError("storage", 403)
                self._identities[path] = identity
        except OSError:
            raise SupportError("storage", 403) from None

    def file(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise SupportError("storage", 403)
        self.check()
        path = self.private / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            # SQLite may remove an optional journal between guarded accesses.
            # Absence grants no permission to adopt a changed private parent.
            self.check()
            return path
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0) & 0x400):
            raise SupportError("storage", 403)
        return path

    @contextmanager
    def lease(self):
        """An OS-held lock fences owners; neither stale PID nor JSON grants ownership."""
        self.ensure()
        path = self.file("support.guard")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        acquired = False
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
                raise SupportError("storage", 403)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                raise SupportError("busy", 409) from None
            if not info.st_size:
                os.write(descriptor, b"0")
            self.check()
            yield
        finally:
            try:
                if acquired:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class SupportStore:
    def __init__(self, paths: SupportPaths):
        self.paths = paths
        self.path = paths.file("support.sqlite3")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (
                  one INTEGER PRIMARY KEY CHECK(one=1), target TEXT NOT NULL,
                  device INTEGER NOT NULL, inode INTEGER NOT NULL, purpose TEXT NOT NULL,
                  provider TEXT NOT NULL, native_id TEXT, reserved_id TEXT,
                  state TEXT NOT NULL, epoch INTEGER NOT NULL, spec_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, text_hash TEXT NOT NULL,
                  text TEXT NOT NULL, state TEXT NOT NULL, epoch INTEGER NOT NULL,
                  deadline REAL NOT NULL, attempted INTEGER NOT NULL DEFAULT 0,
                  cap_hash TEXT, inventory_ok INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  request_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL,
                  status TEXT NOT NULL);
            """)
            if "reason_code" not in {row["name"] for row in db.execute("PRAGMA table_info(binding)")}:
                db.execute("ALTER TABLE binding ADD COLUMN reason_code TEXT")

    @contextmanager
    def connection(self):
        self.paths.check()
        self.paths.file("support.sqlite3")
        for suffix in ("-journal", "-wal", "-shm"):
            self.paths.file("support.sqlite3" + suffix)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            self.paths.check()
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _binding(db):
        row = db.execute("SELECT * FROM binding WHERE one=1").fetchone()
        if row is None:
            raise SupportError("stale", 409)
        return dict(row)

    def open(self, provider: str, spec_hash: str) -> dict:
        if provider != "claude":
            raise SupportError("provider", 409)
        self.paths.target_check()
        selection = self.paths.selection
        expected = (str(selection.path), selection.device, selection.inode, PURPOSE, provider)
        with self.connection() as db:
            current = db.execute("SELECT * FROM binding WHERE one=1").fetchone()
            if current is None:
                db.execute("INSERT INTO binding(one,target,device,inode,purpose,provider,native_id,reserved_id,"
                           "state,epoch,spec_hash) VALUES (1,?,?,?,?,?,NULL,NULL,'ready',0,?)", (*expected, spec_hash))
            elif tuple(current[name] for name in ("target", "device", "inode", "purpose", "provider")) != expected:
                raise SupportError("stale", 409)
            else:
                # Updating the public spec conveys no previous execution grant.
                if current["spec_hash"] != spec_hash:
                    db.execute("UPDATE jobs SET state=CASE WHEN attempted=1 THEN 'unknown' ELSE 'cancelled' END, "
                               "cap_hash=NULL WHERE state IN ('working','needs_human')")
                    pending = db.execute("SELECT 1 FROM jobs WHERE state='unknown' LIMIT 1").fetchone()
                    db.execute("UPDATE binding SET state=?,epoch=epoch+1 WHERE one=1",
                               ("unknown" if pending else "ready",))
                    db.execute("UPDATE messages SET status=(SELECT state FROM jobs WHERE "
                               "jobs.request_id=messages.request_id) WHERE request_id IN "
                               "(SELECT request_id FROM jobs)")
                db.execute("UPDATE binding SET spec_hash=? WHERE one=1", (spec_hash,))
            return self._binding(db)

    def binding(self) -> dict:
        with self.connection() as db:
            return self._binding(db)

    def refresh_profile(self, expected_epoch: int, reason_code: str | None) -> bool:
        """A model-free positive reprobe may clear only a pre-input profile block."""
        if reason_code is not None and reason_code not in PROFILE_REASONS:
            raise ValueError("Invalid guide profile failure")
        self.paths.target_check()
        with self.connection() as db:
            binding = self._binding(db)
            if (binding["epoch"] != expected_epoch or binding["state"] not in {"ready", "error"}
                    or binding["reason_code"] not in PROFILE_REASONS
                    or db.execute("SELECT 1 FROM jobs WHERE state IN ('working','needs_human','unknown') LIMIT 1").fetchone()):
                return False
            db.execute("UPDATE binding SET state=?,reason_code=?,epoch=epoch+1 WHERE one=1",
                       ("error" if reason_code else "ready", reason_code))
            return True

    def recover(self) -> None:
        """Called only by the newly leased owner; never replay an interrupted job."""
        with self.connection() as db:
            rows = db.execute("SELECT id,request_id,attempted FROM jobs WHERE state IN ('working','needs_human')").fetchall()
            binding = db.execute("SELECT * FROM binding WHERE one=1").fetchone()
            uncertain_identity = bool(binding and binding["reserved_id"] and not binding["native_id"])
            for row in rows:
                state = "unknown" if row["attempted"] or uncertain_identity else "cancelled"
                db.execute("UPDATE jobs SET state=?,cap_hash=NULL WHERE id=?", (state, row["id"]))
                db.execute("UPDATE messages SET status=? WHERE request_id=?", (state, row["request_id"]))
            if rows:
                state = "unknown" if uncertain_identity or any(row["attempted"] for row in rows) else "ready"
                db.execute("UPDATE binding SET state=?,epoch=epoch+1 WHERE one=1", (state,))

    def create_job(self, text: str, request_id: str, *, now: float | None = None) -> tuple[dict, str | None]:
        if (not isinstance(text, str) or not text.strip() or len(text) > TEXT_LIMIT
                or not isinstance(request_id, str) or not 8 <= len(request_id) <= 96
                or any(not (ch.isascii() and (ch.isalnum() or ch in "_-")) for ch in request_id)):
            raise SupportError("input")
        try:
            text.encode("utf-8")
        except UnicodeError:
            raise SupportError("input") from None
        self.paths.target_check()
        now = time.time() if now is None else now
        with self.connection() as db:
            old = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["text_hash"] != digest(text):
                    raise SupportError("duplicate", 409)
                return dict(old), None
            binding = self._binding(db)
            if binding["reason_code"] is not None:
                raise SupportError("native", 409)
            if binding["state"] not in {"ready", "error"}:
                raise SupportError("unknown" if binding["state"] == "unknown" else "busy", 409)
            if binding["reserved_id"] and not binding["native_id"]:
                raise SupportError("stale", 409)
            if db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] >= 1000:
                raise SupportError("storage", 409)
            epoch = binding["epoch"] + 1
            identifier, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            db.execute("INSERT INTO jobs(id,request_id,text_hash,text,state,epoch,deadline,cap_hash,created) "
                       "VALUES (?,?,?,?,'working',?,?,?,?)", (identifier, request_id, digest(text), text, epoch,
                                                            now + TURN_SECONDS, digest(token), now))
            db.execute("INSERT INTO messages(id,request_id,role,text,status) VALUES (?,?,'user',?,'working')",
                       (uuid.uuid4().hex, request_id, text))
            db.execute("UPDATE binding SET state='working',epoch=? WHERE one=1", (epoch,))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()), token

    def _active(self, db, identifier: str, *, deadline: bool = True):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        binding = self._binding(db)
        if (row is None or row["state"] != "working" or binding["state"] != "working"
                or row["epoch"] != binding["epoch"] or (deadline and row["deadline"] <= time.time())):
            raise SupportError("unknown", 409)
        return row

    def reserve_native(self, identifier: str, native_id: str) -> None:
        self.paths.target_check()
        with self.connection() as db:
            self._active(db, identifier)
            current = self._binding(db)
            expected = current["native_id"] or current["reserved_id"]
            if expected and expected != native_id:
                raise SupportError("stale", 409)
            db.execute("UPDATE binding SET reserved_id=? WHERE one=1", (native_id,))

    def receipt(self, identifier: str, event: dict) -> None:
        self.paths.target_check()
        with self.connection() as db:
            self._active(db, identifier)
            binding = self._binding(db)
            native_id = event.get("session_id")
            expected = binding["native_id"] or binding["reserved_id"]
            if native_id and (not expected or native_id != expected):
                raise SupportError("stale", 409)
            if event.get("type") == "session":
                if not native_id:
                    raise SupportError("stale", 409)
                db.execute("UPDATE binding SET native_id=? WHERE one=1", (native_id,))
            elif event.get("type") == "input_attempt":
                if not expected:
                    raise SupportError("stale", 409)
                db.execute("UPDATE jobs SET attempted=1 WHERE id=?", (identifier,))

    def profile_ready(self, identifier: str) -> None:
        with self.connection() as db:
            self._active(db, identifier)
            if not self._binding(db)["native_id"]:
                raise SupportError("stale", 409)
            db.execute("UPDATE jobs SET inventory_ok=1 WHERE id=?", (identifier,))

    def authorize_diagnostic(self, token: str) -> dict:
        self.paths.target_check()
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE cap_hash=?", (digest(token),)).fetchone()
            if row is None:
                raise SupportError("auth", 403)
            current = self._active(db, row["id"])
            if not current["inventory_ok"] or not current["attempted"]:
                raise SupportError("auth", 403)
            return dict(current)

    def diagnostic_job(self, identifier: str) -> dict:
        self.paths.target_check()
        with self.connection() as db:
            current = self._active(db, identifier)
            if not current["cap_hash"] or not current["inventory_ok"] or not current["attempted"]:
                raise SupportError("auth", 403)
            return dict(current)

    def finish(self, identifier: str, *, text: str = "", state: str = "completed", native_id: str | None = None,
               terminal_native_failure: bool = False, reason_code: str | None = None) -> bool:
        if state not in {"completed", "unknown", "needs_human", "failed", "cancelled"}:
            raise ValueError("Invalid support result state")
        if not isinstance(text, str) or len(text) > REPLY_LIMIT:
            raise SupportError("native", 409)
        if reason_code is not None and (reason_code not in PROFILE_REASONS or state != "failed"):
            raise ValueError("Invalid guide profile failure")
        try:
            self.paths.target_check()
            target_current = True
        except SupportError:
            target_current = False
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            binding = self._binding(db)
            if not row or row["state"] != "working" or row["epoch"] != binding["epoch"]:
                return False
            if row["deadline"] <= time.time() and row["attempted"]:
                state, text = "unknown", ""
            if not target_current:
                state, text = "unknown", ""
            if native_id and native_id != (binding["native_id"] or binding["reserved_id"]):
                state, text = "unknown", ""
            if state == "completed" and (not binding["native_id"] or not row["attempted"]
                                         or not row["inventory_ok"] or native_id != binding["native_id"]):
                state, text = "unknown", ""
            if state == "failed" and row["attempted"] and not terminal_native_failure:
                state, text = "unknown", ""
            if state in {"failed", "cancelled"} and binding["reserved_id"] and not binding["native_id"]:
                state, text = "unknown", ""
            db.execute("UPDATE jobs SET state=?,cap_hash=NULL WHERE id=?", (state, identifier))
            db.execute("UPDATE messages SET status=? WHERE request_id=?", (state, row["request_id"]))
            if text:
                db.execute("INSERT INTO messages(id,request_id,role,text,status) VALUES (?,?,'assistant',?,?)",
                           (uuid.uuid4().hex, row["request_id"], text, state))
            guide_state = {"completed": "ready", "failed": "error", "cancelled": "ready"}.get(state, state)
            reason = reason_code if state == "failed" and not row["attempted"] else None
            db.execute("UPDATE binding SET state=?,reason_code=?,epoch=epoch+1 WHERE one=1", (guide_state, reason))
            return True

    def cancel(self, request_id: str) -> str:
        with self.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise SupportError("request", 404)
            if row["state"] not in {"working", "needs_human"}:
                return row["state"]
            binding = self._binding(db)
            uncertain = row["attempted"] or binding["reserved_id"] and not binding["native_id"]
            state = "unknown" if uncertain else "cancelled"
            db.execute("UPDATE jobs SET state=?,cap_hash=NULL WHERE id=?", (state, row["id"]))
            db.execute("UPDATE messages SET status=? WHERE request_id=?", (state, request_id))
            db.execute("UPDATE binding SET state=?,epoch=epoch+1 WHERE one=1", ("unknown" if uncertain else "ready",))
            return state

    def public(self, workspace_status: str) -> dict:
        with self.connection() as db:
            binding = self._binding(db)
            state = binding["state"]
            try:
                self.paths.target_check()
            except SupportError:
                state, workspace_status = "stale", "unknown"
            if binding["reserved_id"] and not binding["native_id"] and state in {"ready", "error"}:
                state = "stale"
            messages, message_bytes = [], 0
            for row in db.execute("SELECT id,request_id,role,text,status FROM messages ORDER BY seq DESC LIMIT 100"):
                selected = dict(row)
                size = len(json.dumps(selected, ensure_ascii=False).encode("utf-8"))
                if message_bytes + size > 1024 * 1024:
                    break
                messages.append(selected)
                message_bytes += size
            messages.reverse()
            active = db.execute("SELECT request_id FROM jobs WHERE state IN ('working','needs_human','unknown') "
                                "ORDER BY created DESC LIMIT 1").fetchone()
            return {"guide": {"provider": binding["provider"], "label": "ShareBus 도움", "state": state,
                              "can_send": state in {"ready", "error"} and binding["reason_code"] is None,
                              "reason_code": binding["reason_code"],
                              "active_request_id": active["request_id"] if active else None},
                    "workspace": {"name": self.paths.selection.path.name, "status": workspace_status},
                    "messages": messages, "capabilities": {"providers": ["claude"], "can_configure": False},
                    "spec": {"version": SPEC_VERSION}}
