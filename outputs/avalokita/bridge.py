"""Narrow adapter for an already-running, source-pinned Sharebus workspace.

Discovery never starts an agent, resumes a conversation, or invokes a model.
The one schema extension is an explicit human prompt -> exact native fanout.
Peers always use their own authenticated Sharebus HTTP capability.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid
from contextlib import contextmanager

HERE = Path(__file__).resolve().parent
SHAREBUS = HERE.parent / "sharebus"
if str(SHAREBUS) not in sys.path:
    sys.path.append(str(SHAREBUS))
from store import BusError, BusStore, WorkspaceRetired  # noqa: E402
from hosted_runtime import is_hosted  # noqa: E402

STORE_SHA256 = "c8bace316788d531b362d5591aaff53cbf4955e1bc6ef03ea9579fed2059cc44"
NATIVE_MODES = {"external_attached", "native_attached", "managed_new", "managed_resume"}
CONTROLLED = {"paused", "stopped", "needs_human", "unavailable", "offline", "starting"}
WATCHER_MAX_AGE = 120
SCHEMA = {
    "config": {"key", "value"},
    "agents": {"id", "provider", "label", "capabilities", "mode", "token_hash", "role", "native_session_id", "native_address", "state", "pid", "generation", "channel_verified", "active_root", "error", "created_at", "updated_at"},
    "roots": {"id", "human_event_id", "budget", "used", "deadline", "created_at"},
    "events": {"seq", "id", "kind", "sender", "recipient", "body", "root_id", "created_at", "metadata"},
    "jobs": {"id", "event_id", "agent_id", "root_id", "state", "created_at", "started_at", "finished_at", "generation", "error", "priority", "execution_owner", "native_input_reserved_at", "native_completion_domain"},
}


class BridgeError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeError("A local bridge cannot follow redirects", 403)


def private_read(path: Path, *, maximum: int = 65536, workspace_guard: Callable[[], None] | None = None) -> str:
    def guard():
        if workspace_guard is not None:
            workspace_guard()

    guard()
    BusStore.reject_linked_path(path)
    guard()
    info = path.stat()
    guard()
    if not path.is_file() or info.st_nlink != 1 or info.st_size > maximum:
        raise BridgeError("Private capability file is invalid", 403)
    guard()
    value = path.read_text(encoding="utf-8").strip()
    guard()
    return value


def native_agent(agent: dict[str, Any]) -> bool:
    agent = dict(agent)
    native = agent.get("native_session_id")
    return ((is_hosted(agent) or (agent.get("mode") in {"external_attached", "native_attached"}
            and isinstance(native, str) and 0 < len(native) <= 256 and not any(ord(c) < 32 for c in native)))
            and agent.get("state") not in CONTROLLED)


def agent_binding(agent):
    """A hosted owner is not a requested or observed native session ID."""
    agent = dict(agent)
    return [agent["provider"], agent["native_address"] if is_hosted(agent) else agent["native_session_id"]]


def participant_readiness(agent, observed, *, native_ipc=False, paused=False, now=None, reply=False):
    """Public route admission, distinct from registration and past delivery.

    Legacy wake flags are configuration, so require a fresh healthy watcher too.
    Codex uses only its pinned direct route; queue acceptance is not a fallback.
    """
    result = {"state": "unavailable", "can_dispatch": False, "route": None}
    if not native_agent(agent):
        return {**result, "reason": "native_session_unavailable"}
    state = observed.get("state", {})
    if observed.get("connection", {}).get("state") != "connected":
        return {**result, "reason": "workspace_offline"}
    if paused or state.get("paused") or state.get("auto", {}).get("suspended"):
        return {**result, "reason": "workspace_paused"}
    if is_hosted(agent):
        if not agent.get("hosted_enabled") or not agent.get("hosted_owner_verified") or not agent.get("wake_channel_available"):
            return {**result, "reason": "hosted_transport_unavailable"}
        if agent.get("hosted_blocked"):
            return {**result, "reason": "hosted_delivery_requires_review"}
        if agent.get("state") not in ({"idle", "waiting_for_user"} if reply else {"idle"}):
            return {**result, "state": "busy", "reason": "native_session_busy"}
        return {"state": "ready", "can_dispatch": True, "route": "hosted_cli", "reason": "owned_cli_lazy_start_available"}
    if agent.get("state") == "waiting_for_user" and not reply:
        return {**result, "state": "waiting_user", "reason": "waiting_for_human_reply"}
    if agent.get("state") not in ({"idle", "waiting_for_user"} if reply else {"idle"}):
        return {**result, "state": "busy", "reason": "native_session_busy"}
    if agent.get("provider") == "codex":
        return ({"state": "ready", "can_dispatch": True, "route": "codex_ipc", "reason": "pinned_native_route_available"}
                if native_ipc else {**result, "reason": "codex_native_route_unavailable"})
    watcher = next((item for item in state.get("auto", {}).get("observations", []) if item.get("agent_id") == agent["id"]), {})
    stamp = watcher.get("updated_at", 0)
    age = (time.time() if now is None else now) - stamp if type(stamp) in {int, float} else float("inf")
    if (agent.get("mode") == "native_attached" and agent.get("channel_verified") is True
            and agent.get("wake_supported") is True and agent.get("wake_channel_available") is True
            and state.get("auto", {}).get("active") is True
            and watcher.get("state") in {"watching", "waiting"}
            and watcher.get("note") in {"ready", "idle", "no_pending"} and 0 <= age <= WATCHER_MAX_AGE):
        return {"state": "ready", "can_dispatch": True, "route": "sharebus", "reason": "verified_live_native_watcher"}
    return {**result, "reason": "native_wake_not_verified"}


class SharebusBridge:
    def __init__(self, workspace: str | Path, *, timeout: float = 3):
        self.workspace = BusStore.checked_workspace(workspace)
        identity = self.workspace.stat()
        if os.name == "nt" and identity.st_ino == 0:
            raise BridgeError("Workspace identity metadata is unavailable", 503)
        self._workspace_identity = (identity.st_dev, identity.st_ino)
        self._workspace_retired = False
        self._workspace_guard: Callable[[], None] | None = None
        self._database_initialized = False
        self.private = self.workspace / ".sharebus" / "private"
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.lock = threading.RLock()
        own = self.workspace / ".avalokita" / "private"
        BusStore.reject_linked_path(own)
        own.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(own, 0o700)
        except OSError:
            pass
        self.receipts = own / "bridge.sqlite3"
        BusStore.reject_linked_path(self.receipts)
        if self.receipts.exists() and (not self.receipts.is_file() or self.receipts.stat().st_nlink != 1):
            raise BridgeError("Bridge database cannot be linked", 403)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS dispatch (goal_id TEXT PRIMARY KEY,root_id TEXT NOT NULL,event_id TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL,updated_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS peer_dispatch (id TEXT PRIMARY KEY,actor TEXT NOT NULL,root_id TEXT NOT NULL,request_hash TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('paused','false')")
        self._database_initialized = True

    @contextmanager
    def _db(self):
        self._guard_workspace()
        target = self.receipts.as_uri() + "?mode=rw" if self._database_initialized else self.receipts
        self._guard_workspace_identity()
        db = sqlite3.connect(target, uri=self._database_initialized, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                self._guard_workspace()
                yield db
                self._guard_workspace()
                self._guard_workspace_identity()
        finally:
            db.close()

    def _guard_workspace_identity(self):
        if self._workspace_retired:
            raise BridgeError("Workspace identity changed; this runtime is retired", 403)
        if self._workspace_guard is not None:
            try:
                self._workspace_guard()
            except WorkspaceRetired as exc:
                raise BridgeError(str(exc), exc.status) from None
        else:
            if os.name == "nt" and self._workspace_identity[1] == 0:
                raise BridgeError("Workspace identity metadata is unavailable", 503)
            try:
                for path in (*reversed(self.workspace.parents), self.workspace):
                    info = path.lstat()
                    if os.name == "nt" and info.st_ino == 0:
                        raise BridgeError("Workspace identity metadata is unavailable", 503)
                    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                            or not stat.S_ISDIR(info.st_mode)):
                        self._workspace_retired = True
                        raise BridgeError("Workspace identity changed; this runtime is retired", 403)
                if (info.st_dev, info.st_ino) != self._workspace_identity:
                    self._workspace_retired = True
                    raise BridgeError("Workspace identity changed; this runtime is retired", 403)
            except FileNotFoundError:
                self._workspace_retired = True
                raise BridgeError("Workspace identity is unavailable; this runtime is retired", 403) from None
            except OSError:
                raise BridgeError("Workspace identity could not be checked", 503) from None

    def _guard_workspace(self):
        self._check_database_files()

    def _check_storage_parents(self, *, pinned=False):
        self._guard_workspace_identity()
        for path in (self.receipts.parent.parent, self.receipts.parent):
            self._check_database_path(path, required=True, directory=True)
        for path in (self.private.parent, self.private):
            self._check_database_path(path, required=pinned, directory=True)

    def _check_database_path(self, path, *, required=False, directory=False):
        if not directory:
            optional = not required and path in {Path(str(base) + suffix) for base in (self.receipts, self.private / "bus.sqlite3")
                                                for suffix in ("-wal", "-shm")}
            pinned = path.parent == self.private
            for sample in range(3):
                self._check_storage_parents(pinned=pinned)
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if not required:
                        return
                    raise BridgeError("Bridge storage is unavailable", 409) from None
                except OSError:
                    raise BridgeError("Bridge storage could not be checked", 503) from None
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not stat.S_ISREG(info.st_mode)):
                    raise BridgeError("Bridge database must use regular nonlinked files", 403)
                if info.st_nlink == 1:
                    return
                if os.name == "nt" and info.st_nlink == 0:
                    if not optional or sample == 2:
                        raise BridgeError("Bridge database metadata is unavailable", 503)
                    time.sleep(0.025)
                    continue
                raise BridgeError("Bridge database must use regular nonlinked files", 403)

        try:
            info = path.lstat()
        except FileNotFoundError:
            if not required:
                return
            raise BridgeError("Bridge storage is unavailable", 409) from None
        except OSError:
            raise BridgeError("Bridge storage could not be checked", 503) from None
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BridgeError("Bridge storage cannot use links or reparse points", 403)
        if not stat.S_ISDIR(info.st_mode):
            raise BridgeError("Bridge storage must use real directories", 403)

    def _check_database_files(self):
        self._check_database_path(self.receipts, required=self._database_initialized)
        for suffix in ("-wal", "-shm"):
            self._check_database_path(Path(str(self.receipts) + suffix))

    def _guard_pinned_database(self):
        self._guard_workspace()
        path = self.private / "bus.sqlite3"
        self._check_database_path(path, required=True)
        for suffix in ("-wal", "-shm"):
            self._check_database_path(Path(str(path) + suffix))

    @contextmanager
    def _pinned_transaction(self, db):
        with db:
            self._guard_pinned_database()
            yield db
            self._guard_pinned_database()
            self._guard_workspace_identity()

    def paused(self) -> bool:
        with self._db() as db:
            return db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"

    def control(self, action: str) -> dict[str, Any]:
        if action not in {"pause", "resume"}:
            raise BridgeError("Use pause or resume")
        with self.lock:
            # First close the new-dispatch gate. Do not alter a foreign bus's
            # global controls or terminate work it already accepted.
            with self._db() as db:
                db.execute("UPDATE settings SET value='true' WHERE key='paused'")
            scopes = self.dispatches()
            owned = {value["root_id"]: goal for goal, value in scopes.items()
                     if value["state"] in {"queued", "dispatching", "unknown"}}
            count = 0
            if owned:
                try:
                    db = self._pinned_db()
                    try:
                        with self._pinned_transaction(db):
                            db.execute("BEGIN IMMEDIATE")
                            now = time.time()
                            for root, goal in owned.items():
                                event = db.execute("SELECT id,sender,metadata FROM events WHERE root_id=? AND sender='user' ORDER BY seq LIMIT 1", (root,)).fetchone()
                                if not event or json.loads(event["metadata"]).get("avalokita_goal_id") != goal:
                                    continue
                                if action == "pause":
                                    count += db.execute("UPDATE jobs SET state='avalokita_paused' WHERE root_id=? AND state='queued' AND native_input_reserved_at IS NULL", (root,)).rowcount
                                else:
                                    # Recheck inherited budget/deadline and exact
                                    # current binding before re-exposing queue IDs.
                                    flags = dict(db.execute("SELECT key,value FROM config"))
                                    if flags.get("paused") == "true" or flags.get("auto_suspended") == "true":
                                        continue
                                    budget = db.execute("SELECT * FROM roots WHERE id=?", (root,)).fetchone()
                                    if not budget or budget["deadline"] <= now or budget["used"] >= budget["budget"]:
                                        continue
                                    for job in db.execute("SELECT id,agent_id FROM jobs WHERE root_id=? AND state='avalokita_paused' AND native_input_reserved_at IS NULL", (root,)).fetchall():
                                        agent = db.execute("SELECT * FROM agents WHERE id=?", (job["agent_id"],)).fetchone()
                                        if (agent and native_agent(dict(agent)) and agent["id"] in scopes[goal].get("participants", [])
                                                and scopes[goal].get("bindings", {}).get(agent["id"]) == agent_binding(agent)):
                                            count += db.execute("UPDATE jobs SET state='queued' WHERE id=? AND state='avalokita_paused' AND native_input_reserved_at IS NULL", (job["id"],)).rowcount
                    finally:
                        db.close()
                except (BridgeError, OSError, sqlite3.Error, ValueError):
                    raise BridgeError("Avalokita is locally paused, but its queued bus work could not be reconciled; verify before continuing", 409) from None
            if action == "resume":
                with self._db() as db:
                    db.execute("UPDATE settings SET value='false' WHERE key='paused'")
        return {"paused": action == "pause", "scope": "avalokita_dispatch", "queued_jobs_changed": count,
                "native_cancellation_requested": False}

    def _runtime(self) -> dict[str, Any]:
        self._guard_workspace()
        try:
            runtime = json.loads(private_read(self.private / "runtime.json", workspace_guard=self._guard_workspace_identity))
            parsed = urlsplit(runtime.get("url", ""))
            valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                     and parsed.path in ("", "/") and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment and runtime.get("workspace") == str(self.workspace)
                     and type(runtime.get("pid")) is int and runtime["pid"] > 0
                     and isinstance(runtime.get("workspace_id"), str) and bool(runtime["workspace_id"])
                     and isinstance(runtime.get("nonce"), str) and len(runtime["nonce"]) >= 16)
            if not valid:
                raise ValueError
            health = self._fetch(runtime["url"] + "/health")
            if health.get("ok") is not True or any(health.get(k) != runtime[k] for k in ("pid", "nonce", "workspace_id")):
                raise ValueError
            self._guard_workspace()
            return runtime
        except BusError:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            raise BridgeError("Existing Sharebus is unavailable or its exact process identity changed", 409) from None

    def _fetch(self, url: str, token: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"X-Sharebus-Request": "1"}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(url, data=data, headers=headers), timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            value = json.loads(raw)
            if len(raw) > 2 * 1024 * 1024 or not isinstance(value, dict):
                raise ValueError
            return value
        except HTTPError as exc:
            raise BridgeError("Sharebus rejected the operation (HTTP " + str(exc.code) + ")", exc.code) from None
        except (OSError, URLError, ValueError):
            raise BridgeError("Sharebus is unavailable; uncertain writes are never automatically retried", 409) from None

    def request(self, path: str, payload: dict[str, Any] | None = None, *, token: str | None = None) -> dict[str, Any]:
        self._guard_workspace()
        runtime = self._runtime()  # Validate health before reading or sending a capability.
        token = token if token is not None else private_read(self.private / "operator.token", workspace_guard=self._guard_workspace_identity)
        if not token or len(token) > 4096 or any(ord(c) < 32 for c in token):
            raise BridgeError("Invalid native capability", 403)
        self._guard_workspace()
        return self._fetch(runtime["url"].rstrip("/") + path, token, payload)

    def observe(self) -> dict[str, Any]:
        try:
            state = self.request("/api/state")
            return {"connection": {"state": "connected", "message": "Existing workspace bus attached; installed clients are not presumed connected"}, "state": state}
        except (BridgeError, BusError, OSError):
            return {"connection": {"state": "offline", "message": "No verified running Sharebus in this workspace. Requests remain saved locally."}, "state": {"agents": [], "jobs": []}}

    def authenticate_peer(self, token: str, *, allow_paused: bool = False) -> dict[str, Any]:
        state = self.request("/api/state", token=token)
        own = state.get("own_agent")
        if not isinstance(own, dict) or own.get("mode") not in NATIVE_MODES or not own.get("native_session_id"):
            raise BridgeError("A bound original native session capability is required", 403)
        if is_hosted(own) and not own.get("hosted_owner_verified"):
            raise BridgeError("Observed hosted identity no longer matches its immutable owner", 403)
        if not allow_paused and (own.get("state") in CONTROLLED or state.get("paused") or state.get("auto", {}).get("suspended")):
            raise BridgeError("Native participant or workspace is paused or stopped", 409)
        return own

    def dispatches(self) -> dict[str, dict[str, Any]]:
        with self._db() as db:
            return {row["goal_id"]: {"state": row["state"], "root_id": row["root_id"], "event_id": row["event_id"], **json.loads(row["receipt"])} for row in db.execute("SELECT * FROM dispatch")}

    def _receipt(self, goal: str, root: str, event: str, state: str, value: dict[str, Any]) -> dict[str, Any]:
        with self._db() as db:
            db.execute("INSERT INTO dispatch VALUES (?,?,?,?,?,?) ON CONFLICT(goal_id) DO UPDATE SET state=excluded.state,receipt=excluded.receipt,updated_at=excluded.updated_at", (goal, root, event, state, json.dumps(value), time.time()))
        return {"state": state, "root_id": root, "event_id": event, **value}

    @staticmethod
    def _hosted_descendant(db, job, receipt):
        event = db.execute("SELECT * FROM events WHERE id=? AND root_id=?", (job["event_id"], receipt["root_id"])).fetchone()
        metadata = json.loads(event["metadata"]) if event else {}
        if (metadata.get("avalokita_hosted_peer") is not True or metadata.get("human_event_id") != receipt["event_id"]
                or event["sender"] not in receipt.get("participants", []) or event["recipient"] != job["agent_id"]
                or job["agent_id"] not in receipt.get("participants", [])):
            return False
        return bool(db.execute("SELECT 1 FROM jobs WHERE id=? AND event_id=? AND agent_id=? AND root_id=?",
            (metadata.get("parent_job_id"), metadata.get("parent_event_id"), event["sender"], receipt["root_id"])).fetchone())

    def _pinned_db(self, *, read_only=False):
        self._guard_pinned_database()
        if hashlib.sha256((SHAREBUS / "store.py").read_bytes()).hexdigest() != STORE_SHA256:
            raise BridgeError("Sharebus source version changed; targeted fanout requires review", 409)
        path = self.private / "bus.sqlite3"
        self._guard_workspace_identity()
        db = sqlite3.connect(path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"), uri=True, timeout=3)
        try:
            db.row_factory = sqlite3.Row
            if read_only:
                db.execute("PRAGMA query_only=ON")
            self._guard_pinned_database()
            for table, columns in SCHEMA.items():
                if {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")} != columns:
                    raise BridgeError("Sharebus schema changed; no fanout was written", 409)
            return db
        except BaseException:
            db.close()
            raise

    def public_inspector(self, goal_ids: list[str], *, limit=200) -> dict[str, Any]:
        """Bounded read-only job and explicitly published artifact metadata.

        No HTTP state probe, reconciliation, native calls, paths from prose or
        exported-file reads. The immutable original roster is checked again.
        """
        self._guard_workspace()
        if (not isinstance(goal_ids, list) or len(goal_ids) > 50 or any(not isinstance(value, str) for value in goal_ids)
                or type(limit) is not int or not 1 <= limit <= 200):
            raise BridgeError("Invalid public observation window")
        output = {"jobs": [], "artifacts": [], "totals": {"jobs": 0, "active_jobs": 0, "artifacts": 0},
                  "truncated": {"jobs": False, "artifacts": False}, "status": "unavailable", "error_code": None,
                  "participants": None}
        placeholders = ",".join("?" for _ in goal_ids)
        with self._db() as local:
            saved = local.execute(f"SELECT goal_id,root_id,event_id,state,receipt FROM dispatch WHERE goal_id IN ({placeholders})", goal_ids).fetchall()
        try:
            db = self._pinned_db(read_only=True)
            try:
                # Bound observation cost even if a native store was damaged or
                # a root accumulated an unreasonable number of descendants.
                expires = time.monotonic() + 1.5
                db.set_progress_handler(lambda: int(time.monotonic() > expires), 1000)
                db.execute("BEGIN")
                output["participants"] = db.execute("SELECT COUNT(*) FROM agents WHERE (mode IN ('native_attached','external_attached') "
                    "AND native_session_id IS NOT NULL AND length(native_session_id)>0) OR (mode IN ('managed_new','managed_resume') AND native_address LIKE 'avalokita-hosted/1:%')").fetchone()[0]
                scopes = []
                for row in saved:
                    goal, root, event_id = row["goal_id"], row["root_id"], row["event_id"]
                    expected_root = str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.workspace) + ":avalokita:root:" + goal))
                    expected_event = str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.workspace) + ":avalokita:event:" + goal))
                    if root != expected_root or event_id != expected_event or row["state"] in {"waiting", "paused"}:
                        continue
                    receipt = json.loads(row["receipt"])
                    human = db.execute("SELECT root_id,sender,metadata FROM events WHERE id=?", (event_id,)).fetchone()
                    metadata = json.loads(human["metadata"]) if human else {}
                    if (not human or human["sender"] != "user" or human["root_id"] != root
                            or metadata.get("avalokita_goal_id") != goal or metadata.get("human_authorized") is not True
                            or metadata.get("bindings") != receipt.get("bindings")):
                        continue
                    roster = {}
                    for actor, binding in receipt.get("bindings", {}).items():
                        agent = db.execute("SELECT * FROM agents WHERE id=?", (actor,)).fetchone()
                        if agent and agent["mode"] in NATIVE_MODES and binding == agent_binding(agent):
                            roster[actor] = agent["provider"]
                    if roster:
                        scopes.append((goal, root, event_id, roster))
                peer_table = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_peers'").fetchone())
                artifact_columns = {row[1] for row in db.execute("PRAGMA table_info(artifacts)")}
                artifacts_ok = artifact_columns == {"id", "owner", "filename", "relative_path", "sha256", "size", "created_at", "root_id"}
                for goal, root, human_id, roster in scopes:
                    # Only original inputs and descendants actually published
                    # by a roster member to a roster member belong in this view.
                    actors = ",".join("?" for _ in roster)
                    params = [root, *roster, human_id, *roster, *roster]
                    condition = (f"j.root_id=? AND j.agent_id IN ({actors}) AND "
                        f"(e.id=? OR (e.sender IN ({actors}) AND e.recipient IN ({actors})))")
                    base = " FROM jobs j JOIN events e ON e.id=j.event_id AND e.root_id=j.root_id WHERE " + condition
                    output["totals"]["jobs"] += db.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
                    output["totals"]["active_jobs"] += db.execute("SELECT COUNT(*)" + base +
                        " AND j.state IN ('queued','running','awaiting_native_reply','waiting_user','avalokita_native_pending','avalokita_paused')", params).fetchone()[0]
                    room = max(0, limit - len(output["jobs"]))
                    for job in db.execute("SELECT j.id,j.event_id,j.agent_id,j.state,j.created_at,j.started_at,j.finished_at,e.sender,e.recipient,e.metadata" +
                                          base + " ORDER BY j.created_at DESC,j.id DESC LIMIT ?", [*params, room]):
                        item = {key: job[key] for key in ("id", "event_id", "agent_id", "state", "created_at", "started_at", "finished_at", "sender", "recipient")}
                        cause = None
                        if peer_table and job["event_id"] != human_id:
                            cause = db.execute("SELECT parent_job_id,parent_event_id FROM avalokita_native_peers WHERE job_id=? AND event_id=? "
                                "AND recipient=? AND goal_id=? AND root_id=? AND human_event_id=?", (job["id"], job["event_id"], job["agent_id"], goal, root, human_id)).fetchone()
                        if cause is None and job["event_id"] != human_id:
                            metadata = json.loads(job["metadata"])
                            if metadata.get("avalokita_hosted_peer") is True and metadata.get("human_event_id") == human_id:
                                cause = {"parent_job_id": metadata.get("parent_job_id"), "parent_event_id": metadata.get("parent_event_id")}
                        item.update(root_id=goal, bus_root_id=root, parent_job_id=cause["parent_job_id"] if cause else None,
                                    parent_event_id=cause["parent_event_id"] if cause else None, human_event_id=human_id)
                        output["jobs"].append(item)
                    if not artifacts_ok:
                        continue
                    artifact_filter = (f"a.root_id=? AND a.owner IN ({actors}) AND EXISTS(SELECT 1 FROM events e "
                        "WHERE e.kind='artifact' AND e.root_id=a.root_id AND e.sender=a.owner AND e.recipient='all' "
                        "AND json_extract(e.metadata,'$.artifact.id')=a.id AND json_extract(e.metadata,'$.artifact.sha256')=a.sha256 "
                        "AND json_extract(e.metadata,'$.artifact.filename')=a.filename AND json_extract(e.metadata,'$.artifact.size')=a.size)")
                    artifact_params = [root, *roster]
                    output["totals"]["artifacts"] += db.execute("SELECT COUNT(*) FROM artifacts a WHERE " + artifact_filter, artifact_params).fetchone()[0]
                    room = max(0, 100 - len(output["artifacts"]))
                    for artifact in db.execute("SELECT a.id,a.owner,a.filename,a.sha256,a.size,a.created_at FROM artifacts a WHERE " +
                                                artifact_filter + " ORDER BY a.created_at DESC,a.id DESC LIMIT ?", [*artifact_params, room]):
                        item = dict(artifact)
                        if (not isinstance(item["filename"], str) or len(item["filename"]) > 160
                                or any(char in item["filename"] for char in "/\\:\x00")
                                or not isinstance(item["sha256"], str) or len(item["sha256"]) != 64
                                or type(item["size"]) is not int or not 0 <= item["size"] <= 20 * 1024 * 1024):
                            continue
                        item.update(root_id=goal, bus_root_id=root, provider=roster[item["owner"]], preview_supported=False)
                        output["artifacts"].append(item)
                output["jobs"].sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
                output["artifacts"].sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
                output["truncated"] = {key: output["totals"][key] > len(output[key]) for key in ("jobs", "artifacts")}
                output["status"] = "ready"
            finally:
                db.close()
        except (BridgeError, BusError, OSError, sqlite3.Error, ValueError, TypeError):
            # Never expose a partial success as a complete empty observation.
            output.update(jobs=[], artifacts=[], totals={"jobs": 0, "active_jobs": 0, "artifacts": 0},
                          participants=None, status="unavailable", error_code="public_bus_observation_unavailable")
        self._guard_workspace()
        return output

    def dispatch(self, goal: dict[str, Any], text: str, *, budget: int = 24, ttl: float = 3600, reply_to: str | None = None,
                 recipient_ids: list[str] | None = None, native_ipc_ids: list[str] | None = None,
                 collaborator_ids: list[str] | None = None,
                 require_native_ipc: bool = False) -> dict[str, Any]:
        """One human root, exact existing native recipients, durable uncertainty.

        Deterministic IDs recover a crash between the two independent databases.
        A durable intent with no matching bus event remains unknown, never retried.
        collaborator_ids is a human-authorized complete collaboration roster;
        recipient_ids is its initial input subset. Without collaborator_ids,
        only the initial recipients gain authority. Peers cannot expand it.
        Sharebus's existing supervisor polls queued jobs every 750 ms; no wake or
        native app launch is necessary and the existing daemon is not restarted.
        """
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 65536:
            raise BridgeError("Prompt must contain 1–65536 bytes")
        if type(budget) is not int or not 1 <= budget <= 200 or type(ttl) not in (int, float) or not 1 <= ttl <= 86400:
            raise BridgeError("Invalid human root budget or lifetime")
        if recipient_ids is not None:
            if (not isinstance(recipient_ids, list) or not 1 <= len(recipient_ids) <= 32
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in recipient_ids)
                    or len(set(recipient_ids)) != len(recipient_ids)):
                raise BridgeError("recipient_ids must identify exact registered native participants")
            recipient_ids = sorted(recipient_ids)
        if collaborator_ids is not None:
            if (not isinstance(collaborator_ids, list) or not 1 <= len(collaborator_ids) <= 32
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in collaborator_ids)
                    or len(set(collaborator_ids)) != len(collaborator_ids) or not recipient_ids
                    or not set(recipient_ids).issubset(collaborator_ids)):
                raise BridgeError("collaborator_ids must explicitly authorize a roster containing the initial recipient_ids")
            collaborator_ids = sorted(collaborator_ids)
        native_ipc_ids = native_ipc_ids or []
        goal_id = str(goal["id"])
        root = str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.workspace) + ":avalokita:root:" + goal_id))
        event = str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.workspace) + ":avalokita:event:" + goal_id))
        with self.lock:
            prior = self.dispatches().get(goal_id)
            if prior and prior.get("reply_to") != reply_to:
                raise BridgeError("Idempotency key belongs to a different question response", 409)
            if prior and prior.get("recipient_ids") != recipient_ids:
                raise BridgeError("Idempotency key belongs to a different exact recipient selection", 409)
            if prior and prior.get("collaborator_ids") != collaborator_ids:
                raise BridgeError("Idempotency key belongs to a different human-authorized collaboration roster", 409)
            if prior and prior.get("require_native_ipc", False) != require_native_ipc:
                raise BridgeError("Idempotency key belongs to a different native delivery requirement", 409)
            if prior and prior["state"] not in {"waiting", "paused"}:
                return self._reconcile(goal_id, root, event, prior)
            if self.paused():
                return self._receipt(goal_id, root, event, "paused", {"reply_to": reply_to, "recipient_ids": recipient_ids, "collaborator_ids": collaborator_ids, "require_native_ipc": require_native_ipc, "pending": True, "reason": "Human paused Avalokita dispatch", "delivery_verified": False})
            observed = self.observe()
            candidates = [a for a in observed["state"].get("agents", [])
                          if participant_readiness(a, observed, native_ipc=a["id"] in native_ipc_ids,
                                                   reply=bool(reply_to))["can_dispatch"]]
            if recipient_ids is not None:
                candidates = [agent for agent in candidates if agent["id"] in recipient_ids]
                if {agent["id"] for agent in candidates} != set(recipient_ids):
                    raise BridgeError("An explicitly selected native participant is unavailable", 409)
            roster = candidates
            if collaborator_ids is not None:
                roster = [agent for agent in observed["state"].get("agents", []) if agent["id"] in collaborator_ids
                          and participant_readiness(agent, observed, native_ipc=agent["id"] in native_ipc_ids,
                                                   reply=bool(reply_to))["can_dispatch"]]
                if {agent["id"] for agent in roster} != set(collaborator_ids):
                    raise BridgeError("An explicitly authorized native collaborator is unavailable", 409)
            question = None
            if reply_to:
                if not isinstance(reply_to, str) or not 1 <= len(reply_to) <= 128:
                    raise BridgeError("Exact public question ID required")
                db = self._pinned_db()
                try:
                    row = db.execute("SELECT * FROM events WHERE id=?", (reply_to,)).fetchone()
                    owned_roots = {value["root_id"] for value in self.dispatches().values() if value["state"] == "queued"}
                    if not row or row["kind"] != "question" or row["recipient"] != "user" or row["root_id"] not in owned_roots:
                        raise BridgeError("Reply must name an exact public question in this Avalokita workspace", 403)
                    question = dict(row)
                    candidates = [a for a in candidates if a["id"] == row["sender"]]
                    if collaborator_ids is not None and {agent["id"] for agent in candidates} != set(recipient_ids):
                        raise BridgeError("A question reply must initially target only its exact waiting native participant", 409)
                finally:
                    db.close()
            if collaborator_ids is None:
                roster = candidates
            if observed["connection"]["state"] != "connected" or not candidates:
                return self._receipt(goal_id, root, event, "waiting", {"status": "pending_capability", "durable_accepted": True,
                    "request_budget": budget, "request_ttl": ttl,
                    "reply_to": reply_to, "recipient_ids": recipient_ids, "collaborator_ids": collaborator_ids, "require_native_ipc": require_native_ipc,
                    "pending": True, "reason": "No currently verified native route; the human request is saved without enqueueing agent work",
                    "delivery_verified": False, "outcomes": []})
            runtime = self._runtime()
            # Operator capability is checked against the same database while locked.
            token = private_read(self.private / "operator.token", workspace_guard=self._guard_workspace_identity)
            intended = {a["id"]: tuple(agent_binding(a)) for a in roster}
            initial_ids = {agent["id"] for agent in candidates}
            if reply_to:
                native_ipc_ids = [actor for actor in native_ipc_ids if actor in intended]
            if (not isinstance(native_ipc_ids, list) or any(actor not in intended or intended[actor][0] != "codex" for actor in native_ipc_ids)):
                raise BridgeError("Native IPC selection must match an exact selected Codex binding", 403)
            created = time.time()
            deadline = min(created + ttl, float(goal.get("deadline", created + ttl)))
            if deadline <= created:
                raise BridgeError("Human goal expired; create a fresh instruction", 409)
            intent = {"reply_to": reply_to, "recipient_ids": recipient_ids, "collaborator_ids": collaborator_ids, "require_native_ipc": require_native_ipc, "native_ipc_ids": native_ipc_ids, "participants": sorted(intended), "bindings": {key: list(value) for key, value in intended.items()}, "pending": False, "delivery_verified": False, "bridge_version": "sharebus-targeted-v1", "durable_accepted": True, "status": "queued"}
            # Durable claim before touching the bus; concurrent processes cannot fan out twice.
            raced = False
            with self._db() as local:
                local.execute("BEGIN IMMEDIATE")
                current = local.execute("SELECT state,receipt FROM dispatch WHERE goal_id=?", (goal_id,)).fetchone()
                if current:
                    recorded = json.loads(current["receipt"])
                    if (recorded.get("reply_to") != reply_to or recorded.get("recipient_ids") != recipient_ids
                            or recorded.get("collaborator_ids") != collaborator_ids
                            or recorded.get("require_native_ipc", False) != require_native_ipc):
                        raise BridgeError("Concurrent idempotency key has a different recipient, reply or native requirement", 409)
                if current and current[0] not in {"waiting", "paused"}:
                    raced = True
                else:
                    local.execute("INSERT INTO dispatch VALUES (?,?,?,?,?,?) ON CONFLICT(goal_id) DO UPDATE SET state=excluded.state,receipt=excluded.receipt,updated_at=excluded.updated_at", (goal_id, root, event, "dispatching", json.dumps(intent), created))
            if raced:
                return self._reconcile(goal_id, root, event, self.dispatches()[goal_id])
            try:
                db = self._pinned_db()
                try:
                    with self._pinned_transaction(db):
                        db.execute("BEGIN IMMEDIATE")
                        flags = dict(db.execute("SELECT key,value FROM config"))
                        if flags.get("workspace_id") != runtime["workspace_id"] or flags.get("paused") == "true" or flags.get("auto_suspended") == "true":
                            raise BridgeError("Existing workspace control prevents dispatch", 409)
                        # The verified runtime is operator-owned. Re-read token to detect replacement.
                        if not secrets.compare_digest(token, private_read(self.private / "operator.token", workspace_guard=self._guard_workspace_identity)):
                            raise BridgeError("Workspace operator identity changed", 409)
                        if self.paused():
                            raise BridgeError("Human paused Avalokita dispatch", 409)
                        if db.execute("SELECT 1 FROM events WHERE id=?", (event,)).fetchone():
                            raise BridgeError("Existing deterministic event requires reconciliation", 409)
                        if (db.execute("SELECT 1 FROM roots WHERE id=?", (root,)).fetchone()
                                or db.execute("SELECT 1 FROM jobs WHERE root_id=? OR event_id=?", (root, event)).fetchone()):
                            raise BridgeError("Saved input already has bus work; recovery cannot replay it", 409)
                        if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_dispatch'").fetchone()
                                and db.execute("SELECT 1 FROM avalokita_native_dispatch WHERE goal_id=? OR root_id=? OR event_id=?", (goal_id, root, event)).fetchone()):
                            raise BridgeError("Saved input has an existing native attempt; recovery cannot replay it", 409)
                        rows = [dict(row) for row in db.execute("SELECT * FROM agents")]
                        current_observed = {**observed, "state": {**observed["state"], "auto": {**observed["state"].get("auto", {}),
                            "observations": [dict(row) for row in db.execute("SELECT agent_id,state,note,updated_at FROM auto_observations")]}}}
                        recipients = [a for a in rows if a["id"] in intended and native_agent(a)
                                      and tuple(agent_binding(a)) == intended[a["id"]]
                                      and (not is_hosted(a) or db.execute("SELECT 1 FROM avalokita_hosted_owners WHERE actor=? AND provider=? AND owner_id=? AND enabled=1 AND native_session_id IS ?",
                                          (a["id"], a["provider"], a["native_address"], a["native_session_id"])).fetchone())
                                      and participant_readiness({**next((item for item in observed["state"].get("agents", []) if item["id"] == a["id"]), {}), **a,
                                                               "channel_verified": bool(a["channel_verified"])}, current_observed,
                                                               native_ipc=a["id"] in native_ipc_ids, reply=bool(reply_to))["can_dispatch"]]
                        if len(recipients) != len(intended):
                            raise BridgeError("A native binding or control changed before dispatch", 409)
                        parent = {}
                        if reply_to:
                            replied = db.execute("SELECT * FROM events WHERE id=?", (reply_to,)).fetchone()
                            qmeta = json.loads(replied["metadata"]) if replied else {}
                            waiting = db.execute("SELECT * FROM jobs WHERE id=?", (qmeta.get("waiting_job_id"),)).fetchone()
                            initial = [agent for agent in recipients if agent["id"] in initial_ids]
                            owner = initial[0] if len(initial) == 1 else None
                            if (not replied or not question or replied["root_id"] != question["root_id"] or replied["sender"] != question["sender"]
                                    or not owner or owner["id"] != replied["sender"] or not waiting or waiting["state"] != "waiting_user"
                                    or waiting["agent_id"] != owner["id"] or owner["active_root"] != waiting["root_id"]
                                     or type(waiting["generation"]) is not int or waiting["generation"] != owner["generation"]
                                    or owner["native_session_id"] != qmeta.get("native_session_id")
                                    or waiting["execution_owner"] not in {"manual_inbox", "native_dispatch", "managed_runtime"}):
                                raise BridgeError("Question is no longer waiting in this exact original native session", 409)
                            db.execute("UPDATE jobs SET state='input_received',finished_at=? WHERE id=? AND state='waiting_user'", (created, waiting["id"]))
                            db.execute("UPDATE agents SET state=CASE WHEN state='waiting_for_user' THEN 'idle' ELSE state END,active_root=NULL,updated_at=? WHERE id=? AND active_root=? AND native_session_id=? AND generation=?",
                                       (created, owner["id"], waiting["root_id"], owner["native_session_id"], owner["generation"]))
                            parent = {"in_reply_to": reply_to, "parent_root_id": replied["root_id"]}
                        metadata = json.dumps({"avalokita_goal_id": goal_id, "bridge_version": "sharebus-targeted-v1", "human_authorized": True, "bindings": intent["bindings"], "recipient_ids": recipient_ids, "collaborator_ids": collaborator_ids, "require_native_ipc": require_native_ipc, "native_ipc_ids": native_ipc_ids, **parent}, separators=(",", ":"))
                        db.execute("INSERT INTO roots (id,human_event_id,budget,deadline,created_at) VALUES (?,?,?,?,?)", (root, event, budget, deadline, created))
                        db.execute("INSERT INTO events (id,kind,sender,recipient,body,root_id,created_at,metadata) VALUES (?,'message','user','all',?,?,?,?)", (event, text, root, created, metadata))
                        for agent in recipients:
                            if agent["id"] not in initial_ids:
                                continue
                            job = str(uuid.uuid5(uuid.NAMESPACE_URL, event + ":" + agent["id"]))
                            state = "avalokita_native_pending" if agent["id"] in native_ipc_ids else "queued"
                            execution_owner = "native_dispatch" if agent["id"] in native_ipc_ids else None
                            db.execute("INSERT INTO jobs (id,event_id,agent_id,root_id,state,created_at,priority,execution_owner,generation,native_completion_domain) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                       (job, event, agent["id"], root, state, created, int(bool(reply_to)), execution_owner,
                                        agent["generation"] if execution_owner == "native_dispatch" else None,
                                        "avalokita_direct" if execution_owner == "native_dispatch" else None))
                finally:
                    db.close()
                return self._reconcile(goal_id, root, event, {"state": "queued", **intent})
            except (BridgeError, OSError, sqlite3.Error):
                return self._reconcile(goal_id, root, event, {"state": "unknown", **intent})

    def _reconcile(self, goal: str, root: str, event: str, prior: dict[str, Any]) -> dict[str, Any]:
        try:
            db = self._pinned_db()
            try:
                row = db.execute("SELECT sender,root_id,metadata FROM events WHERE id=?", (event,)).fetchone()
                peer_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_peers'").fetchone()
                jobs = [dict(r) for r in db.execute("SELECT id,event_id,agent_id,state FROM jobs WHERE event_id=? AND root_id=? ORDER BY agent_id", (event, root))]
                if peer_table:
                    jobs.extend(dict(r) for r in db.execute("SELECT j.id,j.event_id,j.agent_id,j.state,p.parent_event_id,p.parent_job_id "
                        "FROM jobs j JOIN avalokita_native_peers p ON p.job_id=j.id AND p.event_id=j.event_id AND p.recipient=j.agent_id "
                        "WHERE j.root_id=? AND p.root_id=? AND p.goal_id=? AND p.human_event_id=? ORDER BY p.created_at,p.rowid", (root, root, goal, event)))
                candidate_receipt = {**prior, "event_id": event, "root_id": root}
                for child in db.execute("SELECT j.* FROM jobs j JOIN events e ON e.id=j.event_id WHERE j.root_id=? AND json_extract(e.metadata,'$.avalokita_hosted_peer')=1 ORDER BY j.created_at,j.id", (root,)):
                    if self._hosted_descendant(db, child, candidate_receipt):
                        metadata = json.loads(db.execute("SELECT metadata FROM events WHERE id=?", (child["event_id"],)).fetchone()[0])
                        jobs.append({**dict(child), "parent_job_id": metadata.get("parent_job_id"), "parent_event_id": metadata.get("parent_event_id")})
                if row and row["sender"] == "user" and row["root_id"] == root and json.loads(row["metadata"]).get("avalokita_goal_id") == goal:
                    metadata = json.loads(row["metadata"])
                    native_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_dispatch'").fetchone()
                    outcomes = []
                    for job in jobs:
                        actor = db.execute("SELECT * FROM agents WHERE id=?", (job["agent_id"],)).fetchone()
                        binding = metadata.get("bindings", {}).get(job["agent_id"])
                        bound = bool(actor and binding == agent_binding(actor))
                        # Match the exact owned input and authenticated selected result,
                        # never a transport ACK or an unrelated final message.
                        results = db.execute("SELECT id,metadata FROM events WHERE root_id=? AND sender=? AND kind='result' AND recipient='user'", (root, job["agent_id"])).fetchall()
                        completed = bool(bound and job["state"] == "completed" and any(
                            json.loads(item["metadata"]).get("job_id") == job["id"]
                            and json.loads(item["metadata"]).get("state") == "completed" for item in results))
                        transport = db.execute("SELECT state FROM avalokita_native_dispatch WHERE job_id=? AND event_id=? AND actor=? AND root_id=?", (job["id"], job["event_id"], job["agent_id"], root)).fetchone() if native_table else None
                        if actor and is_hosted(dict(actor)) and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_hosted_attempts'").fetchone():
                            transport = db.execute("SELECT state FROM avalokita_hosted_attempts WHERE job_id=? AND event_id=? AND actor=? AND root_id=?", (job["id"], job["event_id"], job["agent_id"], root)).fetchone()
                        transport_state = transport[0] if transport else None
                        progress = bound and db.execute("SELECT 1 FROM events WHERE root_id=? AND sender=? AND kind='progress' "
                            "AND (json_extract(metadata,'$.job_id')=? OR json_extract(metadata,'$.event_id')=? OR json_extract(metadata,'$.in_reply_to')=?) LIMIT 1",
                            (root, job["agent_id"], job["id"], job["event_id"], job["event_id"])).fetchone()
                        status = {"completed": "completed" if completed else "needs_review", "running": "working" if progress else "awaiting_native_reply",
                                  "awaiting_native_reply": "working" if progress else "delivery_unknown" if transport_state == "delivery_unknown" else "awaiting_native_reply",
                                  "waiting_user": "waiting_user", "input_received": "input_received",
                                  "avalokita_paused": "paused", "avalokita_unroutable": "pending_capability",
                                  "avalokita_not_sent": "pending_capability", "avalokita_expired": "expired",
                                  "avalokita_binding_revoked": "needs_review", "avalokita_stopped": "stopped",
                                  "avalokita_native_pending": "deferred" if transport_state == "deferred" else "queued", "queued": "queued"}.get(job["state"], "needs_review")
                        expired = bool(db.execute("SELECT deadline<=? FROM roots WHERE id=?", (time.time(), root)).fetchone()[0])
                        if transport_state == "not_sent" and not completed:
                            status = "expired" if expired else "pending_capability"
                        elif expired and job["state"] in {"queued", "avalokita_native_pending", "avalokita_paused"}:
                            status = "expired"
                        outcomes.append({"agent_id": job["agent_id"], "job_id": job["id"], "state": job["state"],
                                         "event_id": job["event_id"], "parent_event_id": job.get("parent_event_id"), "parent_job_id": job.get("parent_job_id"),
                                         "status": status, "delivery_verified": completed, "transport_state": transport_state,
                                         "authorization_expired": expired, "native_input_not_sent": transport_state == "not_sent"})
                    statuses = {item["status"] for item in outcomes}
                    if statuses == {"completed"}:
                        status = "completed"
                    elif "waiting_user" in statuses:
                        status = "waiting_user"
                    elif "working" in statuses:
                        status = "working"
                    elif "delivery_unknown" in statuses:
                        status = "delivery_unknown"
                    elif "awaiting_native_reply" in statuses:
                        status = "awaiting_native_reply"
                    elif "deferred" in statuses:
                        status = "deferred"
                    elif "queued" in statuses:
                        status = "queued"
                    elif "completed" in statuses:
                        status = "partially_completed"
                    elif len(statuses) == 1:
                        status = next(iter(statuses))
                    else:
                        status = "needs_review"
                    return self._receipt(goal, root, event, "queued", {"reply_to": metadata.get("in_reply_to"), "recipient_ids": metadata.get("recipient_ids"), "collaborator_ids": metadata.get("collaborator_ids"), "require_native_ipc": metadata.get("require_native_ipc", False), "native_ipc_ids": metadata.get("native_ipc_ids", []), "participants": sorted(metadata.get("bindings", {})), "bindings": metadata.get("bindings", {}), "jobs": jobs, "pending": status in {"pending_capability", "needs_review"}, "delivery_verified": bool(outcomes) and all(item["delivery_verified"] for item in outcomes), "recovered": True, "durable_accepted": True, "status": status, "outcomes": outcomes})
            finally:
                db.close()
        except (BridgeError, OSError, sqlite3.Error, ValueError):
            pass
        return self._receipt(goal, root, event, "unknown", {"reply_to": prior.get("reply_to"), "recipient_ids": prior.get("recipient_ids"), "collaborator_ids": prior.get("collaborator_ids"), "require_native_ipc": prior.get("require_native_ipc", False), "native_ipc_ids": prior.get("native_ipc_ids", []), "participants": prior.get("participants", []), "bindings": prior.get("bindings", {}), "pending": True, "delivery_verified": False, "reason": "Dispatch outcome requires review; automatic resend is disabled"})

    def reconcile_dispatches(self):
        """Refresh exact saved jobs without creating or redelivering any work."""
        return {goal: self._reconcile(goal, value["root_id"], value["event_id"], value)
                if value["state"] not in {"waiting", "paused"} else value
                for goal, value in self.dispatches().items()}

    def quarantine_unroutable(self, goal_id, actor_id):
        """Trusted operator repair of an app-owned, provably never-started job.

        Preserve the original record and do not alter the native actor, another
        owner's jobs, global controls, or any attempted/accepted input.
        """
        with self.lock:
            receipt = self.dispatches().get(goal_id)
            if not receipt or receipt["state"] != "queued" or actor_id not in receipt.get("participants", []):
                raise BridgeError("Only an exact Avalokita-owned recorded job can be classified", 403)
            observed = self.observe()
            current = next((agent for agent in observed["state"].get("agents", []) if agent["id"] == actor_id), None)
            if (not current or current["provider"] == "codex" or actor_id in receipt.get("native_ipc_ids", [])
                    or participant_readiness(current, observed)["reason"] not in {"native_session_unavailable", "native_wake_not_verified"}):
                raise BridgeError("The exact route must be unavailable without a possible native attempt", 409)
            db = self._pinned_db()
            try:
                with self._pinned_transaction(db):
                    db.execute("BEGIN IMMEDIATE")
                    event = db.execute("SELECT * FROM events WHERE id=? AND root_id=? AND sender='user'", (receipt["event_id"], receipt["root_id"])).fetchone()
                    metadata = json.loads(event["metadata"]) if event else {}
                    job = db.execute("SELECT * FROM jobs WHERE event_id=? AND root_id=? AND agent_id=?", (receipt["event_id"], receipt["root_id"], actor_id)).fetchone()
                    actor = db.execute("SELECT * FROM agents WHERE id=?", (actor_id,)).fetchone()
                    watchers = [dict(row) for row in db.execute("SELECT agent_id,state,note,updated_at FROM auto_observations")]
                    current_observed = {**observed, "state": {**observed["state"], "auto": {**observed["state"].get("auto", {}), "observations": watchers}}}
                    attempted = (job and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_dispatch'").fetchone()
                                 and db.execute("SELECT 1 FROM avalokita_native_dispatch WHERE job_id=?", (job["id"],)).fetchone())
                    if (metadata.get("avalokita_goal_id") != goal_id or metadata.get("human_authorized") is not True
                            or not actor or receipt.get("bindings", {}).get(actor_id) != agent_binding(actor)
                            or not job or job["state"] != "queued" or job["started_at"] is not None or job["finished_at"] is not None
                            or job["native_input_reserved_at"] is not None or attempted):
                        raise BridgeError("This job may have started or belongs to another input; it was preserved", 409)
                    if participant_readiness({**current, **dict(actor), "channel_verified": bool(actor["channel_verified"])}, current_observed)["reason"] not in {"native_session_unavailable", "native_wake_not_verified"}:
                        raise BridgeError("The route changed before classification; original work was preserved", 409)
                    changed = db.execute("UPDATE jobs SET state='avalokita_unroutable',error=? WHERE id=? AND state='queued' AND started_at IS NULL AND finished_at IS NULL AND native_input_reserved_at IS NULL", ("No verified native route; original unstarted work preserved for human review", job["id"])).rowcount
            finally:
                db.close()
            return {"classified": changed == 1, "job_id": job["id"], "state": "avalokita_unroutable", "native_attempted": False}

    def peer_send(self, token: str, recipient: str, body: str, kind: str, bus_root: str, idempotency_key: str,
                  *, parent_job_id=None, parent_event_id=None) -> dict[str, Any]:
        if self.paused():
            raise BridgeError("Human paused Avalokita dispatch", 409)
        own = self.authenticate_peer(token)
        state = self.request("/api/state", token=token)
        target = next((a for a in state.get("agents", []) if a.get("id") == recipient), None)
        if recipient != "user" and target is None:
            raise BridgeError("Recipient is not an exact attached native participant", 404)
        hosted = is_hosted(own) or (target is not None and is_hosted(target))
        receipt = next((value for value in self.dispatches().values() if value["root_id"] == bus_root), None) if hosted else None
        if (own.get("active_root") != bus_root and not (hosted and own.get("active_root") is None and own.get("state") == "idle")):
            raise BridgeError("Peer must be active or exactly completed in this human Sharebus root", 403)
        if hosted and (not receipt or receipt.get("bindings", {}).get(own["id"]) != agent_binding(own)
                or (recipient != "user" and receipt.get("bindings", {}).get(recipient) != agent_binding(target or {}))):
            raise BridgeError("Hosted peer is outside the original human roster", 403)
        def routable(value, observed):
            ready = participant_readiness(value, {"connection": {"state": "connected"}, "state": observed})["can_dispatch"]
            return ready or bool(is_hosted(value) and value.get("hosted_enabled") and value.get("hosted_owner_verified")
                and not value.get("hosted_blocked") and value.get("wake_channel_available") and value.get("state") in {"running", "awaiting_native_reply"}
                and value.get("active_root") == bus_root)
        if recipient != "user" and target is None:
            raise BridgeError("Recipient is not an exact attached native participant", 404)
        if (not isinstance(body, str) or not body.strip() or len(body.encode()) > 65536
                or kind not in {"message", "proposal", "question", "answer", "critique", "progress", "result"}
                or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128):
            raise BridgeError("Valid selected body, kind and stable idempotency key required")
        values = [recipient, body, kind, bus_root]
        if hosted:
            if not all(isinstance(value, str) and 1 <= len(value) <= 128 for value in (parent_job_id, parent_event_id)):
                raise BridgeError("Exact hosted parent input IDs are required", 403)
            values.extend([parent_job_id, parent_event_id])
        request_hash = hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
        identity = own["id"] + ":" + bus_root + ":" + idempotency_key
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM peer_dispatch WHERE id=?", (identity,)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise BridgeError("Idempotency key belongs to a different peer message", 409)
                return {"state": prior["state"], **json.loads(prior["receipt"])}
            if recipient != "user" and not routable(target, state):
                raise BridgeError("pending_capability: exact peer has no verified live legacy route; no work was queued", 409)
            db.execute("INSERT INTO peer_dispatch VALUES (?,?,?,?,?,?)", (identity, own["id"], bus_root, request_hash, "unknown", "{}"))
        route_lost = False
        try:
            db = self._pinned_db()
            try:
                with self._pinned_transaction(db):
                    db.execute("BEGIN IMMEDIATE")
                    sender = db.execute("SELECT * FROM agents WHERE id=?", (own["id"],)).fetchone()
                    flags = dict(db.execute("SELECT key,value FROM config"))
                    if self.paused() or flags.get("paused") == "true" or flags.get("auto_suspended") == "true":
                        raise BridgeError("Human control prevents peer dispatch", 409)
                    if (not sender or agent_binding(sender) != agent_binding(own)
                            or sender["active_root"] not in ({None, bus_root} if hosted else {bus_root}) or not native_agent(dict(sender))
                            or not secrets.compare_digest(sender["token_hash"], hashlib.sha256(token.encode()).hexdigest())):
                        raise BridgeError("Original active native identity changed", 403)
                    if hosted:
                        parent = db.execute("SELECT * FROM jobs WHERE id=? AND event_id=? AND agent_id=? AND root_id=?", (parent_job_id, parent_event_id, own["id"], bus_root)).fetchone()
                        if not parent or parent["state"] not in {"running", "awaiting_native_reply", "completed"}:
                            raise BridgeError("Hosted peer input requires its exact active/completed parent job and event", 403)
                        if parent["state"] == "completed" and not db.execute("SELECT 1 FROM events WHERE root_id=? AND sender=? AND kind='result' AND recipient='user' AND json_extract(metadata,'$.job_id')=? AND json_extract(metadata,'$.state')='completed'", (bus_root, own["id"], parent_job_id)).fetchone():
                            raise BridgeError("Completed hosted parent lacks an exact public result", 403)
                    root = db.execute("SELECT * FROM roots WHERE id=?", (bus_root,)).fetchone()
                    if not root or root["deadline"] <= time.time() or (recipient != "user" and root["used"] >= root["budget"]):
                        raise BridgeError("Inherited human root budget or lifetime exhausted", 409)
                    if recipient != "user":
                        current = db.execute("SELECT * FROM agents WHERE id=?", (recipient,)).fetchone()
                        if (not current or not native_agent(dict(current)) or agent_binding(current) != agent_binding(target)):
                            raise BridgeError("Exact native recipient changed", 409)
                        watchers = [dict(row) for row in db.execute("SELECT agent_id,state,note,updated_at FROM auto_observations")]
                        current_state = {**state, "auto": {**state.get("auto", {}), "observations": watchers}}
                        if not routable({**target, **dict(current), "channel_verified": bool(current["channel_verified"])}, current_state):
                            route_lost = True
                            raise BridgeError("Exact peer route became unavailable before dispatch", 409)
                    now = time.time()
                    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.workspace) + ":avalokita-peer:" + identity))
                    metadata = {"avalokita_peer_dispatch" if hosted else "avalokita_broker_peer_dispatch": identity}
                    if hosted:
                        metadata.update(avalokita_hosted_peer=True, parent_job_id=parent_job_id, parent_event_id=parent_event_id,
                                        human_event_id=receipt["event_id"])
                    if recipient == "user" and kind == "question":
                        waiting = db.execute("SELECT id,execution_owner,generation FROM jobs WHERE agent_id=? AND root_id=? AND state IN ('running','awaiting_native_reply','waiting_user')", (own["id"], bus_root)).fetchone()
                        if waiting:
                            if (waiting["execution_owner"] not in {"manual_inbox", "native_dispatch", "managed_runtime"}
                                    or type(waiting["generation"]) is not int or waiting["generation"] != sender["generation"]):
                                raise BridgeError("The waiting input has no known execution owner; original work was preserved", 409)
                            db.execute("UPDATE jobs SET state='waiting_user',finished_at=? WHERE id=?", (now, waiting["id"]))
                            db.execute("UPDATE agents SET state='waiting_for_user',updated_at=? WHERE id=?", (now, own["id"]))
                            metadata.update(waiting_job_id=waiting["id"], native_session_id=own["native_session_id"])
                    db.execute("INSERT INTO events (id,kind,sender,recipient,body,root_id,created_at,metadata) VALUES (?,?,?,?,?,?,?,?)", (event_id, kind, own["id"], recipient, body, bus_root, now, json.dumps(metadata)))
                    jobs = []
                    if recipient != "user" and recipient != own["id"] and kind in {"message", "proposal", "question", "answer", "critique"}:
                        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, event_id + ":" + recipient))
                        db.execute("INSERT INTO jobs (id,event_id,agent_id,root_id,state,created_at,priority) VALUES (?,?,?,?,'queued',?,0)", (job_id, event_id, recipient, bus_root, now))
                        jobs.append({"id": job_id, "agent_id": recipient, "state": "queued"})
                    receipt = {"event": {"id": event_id, "sender": own["id"], "recipient": recipient, "kind": kind, "body": body, "root_id": bus_root, "created_at": now, "metadata": metadata}, "jobs": jobs, "root_id": bus_root}
            finally:
                db.close()
        except (BridgeError, OSError, sqlite3.Error):
            if route_lost:
                receipt = {"state": "pending_capability", "delivery_verified": False, "native_attempted": False,
                           "reason": "Exact peer route became unavailable; no native input or queued work was created"}
                with self._db() as db:
                    db.execute("UPDATE peer_dispatch SET state='pending_capability',receipt=? WHERE id=?", (json.dumps(receipt), identity))
                return receipt
            return {"state": "unknown", "delivery_verified": False, "reason": "Review this same dispatch; resend is disabled"}
        with self._db() as db:
            db.execute("UPDATE peer_dispatch SET state='accepted',receipt=? WHERE id=?", (json.dumps(receipt), identity))
        return {"state": "accepted", **receipt}

    def peer_scope(self, own, goal_id, *, read_only=False):
        """A completed native input keeps roster membership, not new authority."""
        receipt = self.dispatches().get(goal_id)
        if receipt and receipt.get("state") not in {"queued", "waiting", "paused"}:
            receipt = self._reconcile(goal_id, receipt["root_id"], receipt["event_id"], receipt)
        if (not receipt or receipt.get("state") != "queued" or own["id"] not in receipt.get("participants", [])
                or receipt.get("bindings", {}).get(own["id"]) != agent_binding(own)
                or own.get("active_root") not in {None, receipt["root_id"]}):
            raise BridgeError("This exact native actor is outside the original Avalokita human roster", 403)
        if own.get("active_root") == receipt["root_id"]:
            return receipt
        if own.get("state") != "idle" and not read_only:
            raise BridgeError("The native participant is controlled or waiting for human input", 409)
        db = self._pinned_db()
        try:
            completed = db.execute("SELECT j.id,j.event_id FROM jobs j WHERE j.agent_id=? AND j.root_id=? AND j.state='completed' "
                "AND EXISTS(SELECT 1 FROM events e WHERE e.sender=j.agent_id AND e.root_id=j.root_id AND e.kind='result' AND e.recipient='user' "
                "AND json_extract(e.metadata,'$.job_id')=j.id AND json_extract(e.metadata,'$.state')='completed') ORDER BY j.created_at DESC,j.id DESC",
                (own["id"], receipt["root_id"])).fetchall()
            peer_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_peers'").fetchone()
            for job in completed:
                if job["event_id"] == receipt["event_id"] or (peer_table and db.execute(
                        "SELECT 1 FROM avalokita_native_peers WHERE job_id=? AND event_id=? AND recipient=? AND goal_id=? AND root_id=? AND human_event_id=?",
                        (job["id"], job["event_id"], own["id"], goal_id, receipt["root_id"], receipt["event_id"])).fetchone()) or self._hosted_descendant(db, {**dict(job), "agent_id": own["id"]}, receipt):
                    return receipt
        finally:
            db.close()
        raise BridgeError("The original actor has no active or exact completed input in this human root", 403)

    def team(self, *, workspace_guard=None) -> dict[str, Any]:
        self._guard_workspace()
        from policy import read_policy
        value = read_policy(self.workspace, workspace_guard=workspace_guard or self._workspace_guard or self._guard_workspace)
        self._guard_workspace()
        return value

    def save_team(self, team: dict[str, Any], revision: str | None, *, workspace_guard=None) -> dict[str, Any]:
        self._guard_workspace()
        from policy import PolicyConflict, save_team
        try:
            return save_team(self.workspace, team, expected_revision=revision,
                             workspace_guard=workspace_guard or self._workspace_guard or self._guard_workspace)
        except PolicyConflict as exc:
            raise BridgeError(str(exc), 409) from None

    def selected_outputs(self, *, goal_ids=None, limit=200) -> list[dict[str, Any]]:
        """Read only explicit public bus events in roots minted by this adapter.

        Never inspect native transcript stores or unrelated Sharebus histories.
        The exact root/recipient binding is retained in the private dispatch log.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise BridgeError("Invalid selected output window")
        if goal_ids is not None and (not isinstance(goal_ids, list) or len(goal_ids) > 50):
            raise BridgeError("Invalid selected goal window")
        with self._db() as local:
            if goal_ids is None:
                saved = local.execute("SELECT goal_id,root_id,event_id,receipt FROM dispatch WHERE state='queued' ORDER BY updated_at DESC,goal_id DESC LIMIT 50").fetchall()
            elif not goal_ids:
                return []
            else:
                saved = local.execute("SELECT goal_id,root_id,event_id,receipt FROM dispatch WHERE state='queued' AND goal_id IN (" + ",".join("?" for _ in goal_ids) + ")", goal_ids).fetchall()
        bindings = {row["root_id"]: (row["goal_id"], {**json.loads(row["receipt"]), "event_id": row["event_id"]}) for row in saved}
        if not bindings:
            return []
        db = self._pinned_db()
        try:
            result = []
            values = ",".join("(?,?,?)" for _ in bindings)
            params = [value for root, (goal, receipt) in bindings.items() for value in (root, goal, json.dumps(receipt.get("bindings", {})))]
            rows = db.execute("WITH admitted(root,goal,roster) AS (VALUES " + values + ") SELECT e.*,a.goal AS selected_goal "
                "FROM events e JOIN admitted a ON a.root=e.root_id WHERE e.sender NOT IN ('user','system') "
                "AND e.kind IN ('message','proposal','progress','result','question','critique','answer') "
                "AND EXISTS(SELECT 1 FROM json_each(a.roster) r WHERE r.key=e.sender) ORDER BY e.seq DESC LIMIT ?", [*params, limit]).fetchall()
            for row in reversed(rows):
                root, goal = row["root_id"], row["selected_goal"]
                receipt = bindings[root][1]
                agent = db.execute("SELECT * FROM agents WHERE id=?", (row["sender"],)).fetchone()
                if (not agent or agent["mode"] not in NATIVE_MODES or not agent["native_session_id"]
                        or receipt.get("bindings", {}).get(agent["id"]) != agent_binding(agent)):
                    continue
                event = {key: row[key] for key in row.keys() if key != "selected_goal"}
                metadata = json.loads(event["metadata"])
                waiting_id = metadata.get("waiting_job_id")
                waiting = db.execute("SELECT state FROM jobs WHERE id=?", (waiting_id,)).fetchone() if waiting_id else None
                source_job_id = metadata.get("job_id")
                completed_job = db.execute("SELECT * FROM jobs WHERE id=? AND root_id=? AND agent_id=?", (source_job_id, root, agent["id"])).fetchone() if source_job_id else None
                owned_input = bool(completed_job and completed_job["event_id"] == receipt["event_id"])
                if completed_job and not owned_input and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_peers'").fetchone():
                    owned_input = bool(db.execute("SELECT 1 FROM avalokita_native_peers WHERE job_id=? AND event_id=? AND recipient=? AND goal_id=? AND root_id=? AND human_event_id=?",
                        (source_job_id, completed_job["event_id"], agent["id"], goal, root, receipt["event_id"])).fetchone())
                if completed_job and not owned_input:
                    owned_input = self._hosted_descendant(db, completed_job, {**receipt, "root_id": root})
                event.update(goal_id=goal, provider=agent["provider"], native_session_id=agent["native_session_id"],
                             role=agent["role"] or agent["label"] or "peer", capabilities=json.loads(agent["capabilities"]),
                             question_pending=bool(waiting and waiting["state"] == "waiting_user"),
                             completion_verified=bool(row["kind"] == "result" and row["recipient"] == "user"
                                 and owned_input and completed_job["state"] == "completed" and metadata.get("state") == "completed"),
                             source_job_id=source_job_id, source_input_event_id=completed_job["event_id"] if owned_input else None)
                result.append(event)
            return result
        finally:
            db.close()


def discover_installed() -> list[dict[str, Any]]:
    """Only executable lookup/process names. No CLI session or LLM command."""
    from discovery import PROVIDERS, _PROCESS_NAMES, _running_processes, resolve_executable
    processes = _running_processes()
    found = [{"provider": provider, "installed": bool(resolve_executable(provider)), "running": any(p["name"].lower() in _PROCESS_NAMES[provider] for p in processes), "attachment": "detected", "verified": False, "capabilities": [], "notes": "Installation/process detection only; original session access is not inferred"} for provider in PROVIDERS]
    import shutil
    found.append({"provider": "grok", "installed": bool(shutil.which("grok")), "running": False,
                  "attachment": "pending_capability", "verified": False, "capabilities": [],
                  "notes": "Executable detection only; supported native session transport is unknown"})
    return found
