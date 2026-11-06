"""Loopback-only Avalokita service with durable human-authorized work.

Construction performs no model calls. Operator state refresh may continue a
saved, active human request which has provably never created native work.
"""
from __future__ import annotations

import argparse
import hashlib
from contextlib import contextmanager, nullcontext
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import stat
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from adapters.canonical_reason import CanonicalReasonTransport
from bridge import BridgeError, BusError, BusStore, SharebusBridge, WorkspaceRetired, agent_binding, participant_readiness, is_hosted, private_read
from hosted import HostedRuntime
from device_discovery import DeviceDiscovery
from inventory_runtime import InventoryRuntime
from harmony import HarmonyError, HarmonyStore
from native_dispatch import NativeDispatcher
from hosted_runtime import HostedAuthorityDenied

HERE = Path(__file__).resolve().parent
MAX_REQUEST = 1024 * 1024
MAX_PROMPT_POLICY_BYTES = 32 * 1024
MAX_NATIVE_PROMPT_BYTES = 64 * 1024


class Runtime:
    def __init__(self, workspace: str | Path, *, bridge=None, discovery=True, native_ipc=None, native_factory=None,
                 hosted_factory=None, hosted_discovery=None, reason_transport: CanonicalReasonTransport | None = None,
                 inventory=None, inventory_home=None, inventory_data_root=None, inventory_factory=None):
        self.store = HarmonyStore(Path(workspace))
        self.bridge = bridge or SharebusBridge(workspace)
        self._work_condition = threading.Condition()
        self._closing = False
        self._active_work = 0
        self._drain_lock = threading.Lock()
        self._workspace_retired = False
        self.store._workspace_guard = self._guard_workspace_identity
        self.bridge._workspace_guard = self._guard_workspace_identity
        self.hosted = HostedRuntime(self.bridge, adapter_factory=hosted_factory, discovery=hosted_discovery,
                                    peer_callback=self.peer, workspace_guard=self._guard_workspace_identity,
                                    acquire_work=self.try_acquire_work, admission=self._hosted_admission,
                                    native_completion_handler=self.complete_native_result,
                                    initialize_on_prompt=discovery or hosted_discovery is not None)
        self.lock = threading.RLock()
        self.device_discovery = DeviceDiscovery(self.store.workspace, enabled=discovery)
        self.inventory = (inventory_factory or InventoryRuntime)(self.store.workspace,
            enabled=discovery if inventory is None else inventory, home=inventory_home,
            data_root=inventory_data_root,
            device_probe=self.device_discovery.snapshot, close_timeout=0.25)
        self.callback = ""
        self.waiting_cursor = 0
        self.store.set_paused(self.bridge.paused())
        self.reason_transport = reason_transport
        enabled = discovery if native_ipc is None else native_ipc
        self.native_dispatcher = NativeDispatcher(self.bridge, harmony_store=self.store, adapter_factory=native_factory,
                                                  reason_transport=reason_transport) if enabled else None
        self.peer_wakeup = threading.Event()
        self.peer_stop = threading.Event()
        self.peer_thread = None

    # Reuse the existing complete native/hosted ancestry policy without creating
    # another dispatcher or acquiring an additional authority connection.
    _lineage = NativeDispatcher._lineage

    def complete_native_result(self, *, actor, native_session_id, text, job_id, event_id):
        """Trusted local callback installed on this runtime's owned broker."""
        try:
            with self._work():
                self._guard_workspace_identity()
                if (not isinstance(actor, str) or not 1 <= len(actor) <= 128
                        or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in actor)):
                    raise BridgeError("Exact native actor required", 403)
                token = private_read(self.bridge.private / "credentials" / (actor + ".token"),
                                     workspace_guard=self._guard_workspace_identity)
                return NativeDispatcher.complete_native(self.bridge, token, actor=actor, native_session_id=native_session_id,
                    job_id=job_id, event_id=event_id, text=text)
        except BridgeError as error:
            raise BusError(str(error), error.status) from None

    def _hosted_authority(self, harmony, local, db, actor, job, generation, phase):
        reserved = phase != "claim"
        resuming = phase == "resume"
        root = db.execute("SELECT * FROM roots WHERE id=?", (job["root_id"],)).fetchone()
        human = db.execute("SELECT * FROM events WHERE id=?", (root["human_event_id"],)).fetchone() if root else None
        metadata = json.loads(human["metadata"]) if human else {}
        if not isinstance(metadata, dict) or not isinstance(metadata.get("avalokita_goal_id"), str):
            raise HostedAuthorityDenied("Original human goal metadata is unavailable")
        saved = local.execute("SELECT * FROM dispatch WHERE goal_id=?", (metadata.get("avalokita_goal_id"),)).fetchone()
        if saved and saved["state"] == "dispatching":
            raise HostedAuthorityDenied("Original dispatch is still reconciling", defer=True)
        receipt = {**dict(saved), **json.loads(saved["receipt"])} if saved else {}
        if (not isinstance(receipt.get("participants"), list) or not isinstance(receipt.get("bindings"), dict)
                or any(not isinstance(value, str) for value in receipt["participants"])):
            raise HostedAuthorityDenied("Original human participant receipt is unavailable")
        agent = db.execute("SELECT * FROM agents WHERE id=?", (actor,)).fetchone()
        owner = db.execute("SELECT * FROM avalokita_hosted_owners WHERE actor=?", (actor,)).fetchone()
        flags = dict(db.execute("SELECT key,value FROM config"))
        if (not saved or saved["state"] != "queued" or not root or not human or human["sender"] != "user"
                or saved["root_id"] != root["id"] or saved["event_id"] != human["id"]
                or human["root_id"] != root["id"] or metadata.get("human_authorized") is not True
                or metadata.get("bindings") != receipt.get("bindings")
                or sorted(metadata.get("bindings", {})) != sorted(receipt.get("participants", []))
                or actor not in receipt.get("participants", []) or not agent or not is_hosted(dict(agent))
                or receipt["bindings"].get(actor) != agent_binding(agent)
                or not owner or not owner["enabled"] or owner["provider"] != agent["provider"]
                or owner["owner_id"] != agent["native_address"] or owner["native_session_id"] != agent["native_session_id"]
                or owner["generation"] != agent["generation"]
                or generation is not None and generation not in ({agent["generation"], agent["generation"] + 1}
                                                                 if phase == "begin" else {agent["generation"]})
                or job["agent_id"] != actor or job["native_input_reserved_at"] is not None
                or reserved and job["execution_owner"] != "managed_runtime"
                or not reserved and job["execution_owner"] not in {None, "managed_runtime"}
                or root["deadline"] <= time.time() or root["used"] > root["budget"]
                or not reserved and root["used"] >= root["budget"]):
            raise HostedAuthorityDenied("Exact original human receipt, binding or budget no longer authorizes this job")
        if (local.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"
                or flags.get("paused") == "true" or flags.get("auto_suspended") == "true"):
            raise HostedAuthorityDenied("Original workspace is paused", state="avalokita_paused")
        goal = harmony.execute("SELECT state FROM goals WHERE id=?", (saved["goal_id"],)).fetchone()
        try:
            NativeDispatcher.harmony_guard(harmony, saved["goal_id"], reserved=reserved)
        except BridgeError as error:
            paused = harmony.execute("SELECT value FROM config WHERE key='paused'").fetchone()
            state = "avalokita_paused" if paused and paused[0] == "1" or goal and goal["state"] == "paused" else "avalokita_stopped" if goal and goal["state"] == "stopped" else "needs_human"
            raise HostedAuthorityDenied(str(error), state=state) from None
        for peer in receipt["participants"]:
            current = db.execute("SELECT * FROM agents WHERE id=?", (peer,)).fetchone()
            if not current or agent_binding(current) != receipt["bindings"][peer]:
                raise HostedAuthorityDenied("An immutable human participant binding changed")
        attempt = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()
        if resuming and (job["state"] not in {"avalokita_paused", "queued"} or job["started_at"] is None or root["used"] < 1
                         or attempt and (attempt["state"] != "paused" or attempt["input_attempted"]
                                         or attempt["native_turn_id"] is not None or attempt["actor"] != actor
                                         or attempt["event_id"] != job["event_id"] or attempt["root_id"] != root["id"]
                                         or attempt["owner_id"] != owner["owner_id"] or attempt["generation"] > owner["generation"]
                                         or attempt["native_session_id"] is not None and attempt["native_session_id"] != owner["native_session_id"])):
            raise HostedAuthorityDenied("The held reservation no longer proves an exact unattempted input")
        if reserved and not resuming and (job["state"] not in {"running", "awaiting_native_reply"}
                         or agent["active_root"] != root["id"]):
            raise HostedAuthorityDenied("The exact reserved input is no longer active")
        if reserved and agent["state"] in {"paused", "stopped", "needs_human"}:
            state = "avalokita_paused" if agent["state"] == "paused" else "avalokita_stopped" if agent["state"] == "stopped" else "needs_human"
            raise HostedAuthorityDenied("Human control holds this exact reserved input", state=state)
        if phase == "claim" and attempt:
            raise HostedAuthorityDenied("A saved input already has a native attempt; replay refused")
        # NativeDispatcher's recursive proof handles both forms of peer parent.
        event = db.execute("SELECT * FROM events WHERE id=?", (job["event_id"],)).fetchone()
        if event and event["id"] != human["id"] and not self.bridge._hosted_descendant(db, job, receipt):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_peers'").fetchone():
                raise HostedAuthorityDenied("The owned input lacks an authenticated parent")
        try:
            self._lineage(db, job, receipt)
        except BridgeError as error:
            raise HostedAuthorityDenied(str(error)) from None
        current, seen = job, set()
        while current["event_id"] != human["id"]:
            if current["id"] in seen or len(seen) >= 200:
                raise HostedAuthorityDenied("Owned input parent cycle or depth limit")
            seen.add(current["id"])
            selected = db.execute("SELECT * FROM events WHERE id=?", (current["event_id"],)).fetchone()
            meta = json.loads(selected["metadata"])
            if meta.get("avalokita_hosted_peer") is True:
                proof = local.execute("SELECT * FROM peer_dispatch WHERE id=?", (meta.get("avalokita_peer_dispatch"),)).fetchone()
                if proof and proof["state"] == "unknown":
                    raise HostedAuthorityDenied("Peer dispatch is still reconciling", defer=True)
                values = [selected["recipient"], selected["body"], selected["kind"], selected["root_id"], meta.get("parent_job_id"), meta.get("parent_event_id")]
                delivered = json.loads(proof["receipt"]) if proof else {}
                if (not proof or proof["state"] != "accepted" or proof["actor"] != selected["sender"]
                        or proof["root_id"] != root["id"] or delivered.get("event", {}).get("id") != selected["id"]
                        or proof["request_hash"] != hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()
                        or not any(item.get("id") == current["id"] and item.get("agent_id") == current["agent_id"] for item in delivered.get("jobs", []))):
                    raise HostedAuthorityDenied("The exact hosted peer receipt changed")
                parent_id = meta["parent_job_id"]
            else:
                proof = db.execute("SELECT parent_job_id FROM avalokita_native_peers WHERE job_id=?", (current["id"],)).fetchone()
                parent_id = proof[0] if proof else None
            current = db.execute("SELECT * FROM jobs WHERE id=?", (parent_id,)).fetchone()
            if not current:
                raise HostedAuthorityDenied("Owned input parent disappeared")
            sender = db.execute("SELECT * FROM agents WHERE id=?", (current["agent_id"],)).fetchone()
            if sender and is_hosted(dict(sender)):
                attempt = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=?", (current["id"],)).fetchone()
                if (not attempt or not attempt["input_attempted"] or attempt["actor"] != sender["id"]
                        or attempt["root_id"] != root["id"] or attempt["event_id"] != current["event_id"]
                        or attempt["owner_id"] != sender["native_address"] or attempt["native_session_id"] != sender["native_session_id"]):
                    raise HostedAuthorityDenied("Hosted parent lacks its exact attempted input proof")
        return receipt

    @staticmethod
    def _hosted_readset(db, actor, job, receipt):
        roster = sorted(receipt["participants"])
        placeholders = ",".join("?" for _ in roster)
        queries = [("SELECT * FROM config ORDER BY key", ()),
                   ("SELECT * FROM roots WHERE id=?", (job["root_id"],)),
                   ("SELECT * FROM jobs WHERE root_id=? ORDER BY id", (job["root_id"],)),
                   ("SELECT * FROM events WHERE root_id=? ORDER BY seq", (job["root_id"],)),
                   (f"SELECT * FROM agents WHERE id IN ({placeholders}) ORDER BY id", tuple(roster)),
                   (f"SELECT * FROM avalokita_hosted_owners WHERE actor IN ({placeholders}) ORDER BY actor", tuple(roster)),
                   ("SELECT * FROM avalokita_hosted_attempts WHERE root_id=? ORDER BY job_id", (job["root_id"],))]
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_native_peers'").fetchone():
            queries.append(("SELECT * FROM avalokita_native_peers WHERE root_id=? ORDER BY id", (job["root_id"],)))
        return [(query, arguments, [tuple(row) for row in db.execute(query, arguments)]) for query, arguments in queries]

    @contextmanager
    def _hosted_admission(self, store, actor, job=None, generation=None, *, phase="claim"):
        """Hold Harmony/local writers; recheck the complete Bus proof at its writer."""
        self._guard_workspace_identity()
        active = [True]
        try:
            with self.store._connection(write=True) as harmony:
                with self.bridge._db() as local:
                    local.execute("BEGIN IMMEDIATE")
                    expected = {"job_id": None}
                    with store.connection() as db:
                        db.execute("BEGIN IMMEDIATE")
                        if phase in {"claim", "resume"}:
                            selection = "state IN ('queued','avalokita_paused') AND started_at IS " + ("NOT NULL" if phase == "resume" else "NULL")
                            candidates = db.execute(f"SELECT * FROM jobs WHERE agent_id=? AND ({selection}) ORDER BY priority DESC,created_at,id", (actor,)).fetchall()
                        else:
                            candidates = [db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()]
                        for candidate in candidates:
                            if phase in {"claim", "resume"} and candidate and (
                                    candidate["native_input_reserved_at"] is not None
                                    or candidate["execution_owner"] not in ({None, "managed_runtime"} if phase == "claim" else {"managed_runtime"})):
                                # A saved foreign or legacy reservation is not a
                                # new hosted candidate and must not be reclassified.
                                continue
                            try:
                                if not candidate or job is not None and any(candidate[key] != job[key] for key in ("id", "agent_id", "event_id", "root_id")):
                                    raise HostedAuthorityDenied("The exact owned job changed")
                                receipt = self._hosted_authority(harmony, local, db, actor, candidate, generation, phase)
                            except (json.JSONDecodeError, TypeError, KeyError):
                                if phase not in {"claim", "resume"}:
                                    raise HostedAuthorityDenied("Original input proof is malformed") from None
                                db.execute("UPDATE jobs SET state='needs_human',error='Original input proof is malformed' WHERE id=? AND state IN ('queued','avalokita_paused')", (candidate["id"],))
                                continue
                            except HostedAuthorityDenied as error:
                                if phase not in {"claim", "resume"}:
                                    raise
                                if not error.defer:
                                    db.execute("UPDATE jobs SET state=?,error=? WHERE id=? AND state IN ('queued','avalokita_paused')", (error.state, str(error), candidate["id"]))
                                    if phase == "resume" and error.state != "avalokita_paused":
                                        db.execute("UPDATE avalokita_hosted_attempts SET state='refused',updated_at=? WHERE job_id=? AND state='paused' AND input_attempted=0", (time.time(), candidate["id"]))
                                continue
                            snapshot = self._hosted_readset(db, actor, candidate, receipt)
                            deadline = min(harmony.execute("SELECT deadline FROM goals WHERE id=?", (receipt["goal_id"],)).fetchone()[0],
                                           db.execute("SELECT deadline FROM roots WHERE id=?", (candidate["root_id"],)).fetchone()[0])
                            def validate(writer, rows=snapshot, expires=deadline):
                                if not active[0] or not all([tuple(row) for row in writer.execute(query, arguments)] == values for query, arguments, values in rows):
                                    return False
                                if time.time() >= expires:
                                    raise HostedAuthorityDenied("Original human authority expired before its durable grant")
                                return True
                            expected = {"job_id": candidate["id"], "state": candidate["state"], "validate": validate, "deadline": deadline}
                            break
                    yield expected
        finally:
            active[0] = False

    def try_acquire_work(self):
        """Return one transferable, idempotent release permit, or refuse new work."""
        with self._work_condition:
            if self._closing:
                return None
            self._active_work += 1
        released = False

        def release():
            nonlocal released
            with self._work_condition:
                if not released:
                    released = True
                    self._active_work -= 1
                    self._work_condition.notify_all()
        return release

    @contextmanager
    def _work(self):
        release = self.try_acquire_work()
        if release is None:
            raise BridgeError("Owned workspace service is closing", 409)
        try:
            yield
        finally:
            release()

    def begin_closing(self):
        with self._work_condition:
            self._closing = True
            self._work_condition.notify_all()
        self.hosted.begin_closing()
        self.inventory.close()

    def wait_idle(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        with self._work_condition:
            while self._active_work:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._work_condition.wait(remaining)
            return True

    def configure_hosted(self, payload):
        with self._work():
            return self.hosted.configure(payload)

    def repair_hosted_startup(self, payload):
        # Also admit this permit inside the gate: two simultaneous repairs must
        # not both acquire permits and then reject each other as other work.
        with self._work_condition:
            with self._work():
                # Keep admission and close outside this short repair. Counting
                # alone would permit a new worker before the readiness commit.
                if self._closing or self._active_work != 1:
                    raise BridgeError("Owned workspace has other active work", 409)
                return self.hosted.repair_startup(payload)

    def reply_hosted(self, payload):
        with self._work():
            return self.hosted.reply(payload)

    def _retire_workspace(self):
        self._workspace_retired = True
        self.store._workspace_retired = True
        self.bridge._workspace_retired = True
        raise WorkspaceRetired()

    def _guard_workspace_identity(self):
        if (self._workspace_retired or self.store._workspace_retired
                or getattr(self.bridge, "_workspace_retired", False)):
            self._retire_workspace()
        if os.name == "nt" and (self.store._workspace_identity[1] == 0 or self.bridge._workspace_identity[1] == 0):
            raise BusError("Workspace identity metadata is unavailable", 503)
        try:
            root = self.store.workspace
            for path in (*reversed(root.parents), root):
                info = path.lstat()
                if os.name == "nt" and info.st_ino == 0:
                    raise BusError("Workspace identity metadata is unavailable", 503)
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not stat.S_ISDIR(info.st_mode)):
                    self._retire_workspace()
            if ((info.st_dev, info.st_ino) != self.store._workspace_identity
                    or (info.st_dev, info.st_ino) != self.bridge._workspace_identity
                    or self.bridge.workspace != root):
                self._retire_workspace()
        except FileNotFoundError:
            self._retire_workspace()
        except OSError:
            raise BusError("Workspace identity could not be checked", 503) from None

    def _guard_workspace(self):
        try:
            self._guard_workspace_identity()
        except WorkspaceRetired as exc:
            raise BridgeError(str(exc), exc.status) from None
        self.store._check_database_files()
        self.bridge._guard_workspace()

    def start_peer_reconciler(self):
        """The server owns an idle local mailbox helper, never a model heartbeat."""
        if self.peer_stop.is_set() or self.peer_thread and self.peer_thread.is_alive():
            return
        def reconcile():
            while not self.peer_stop.is_set():
                self.peer_wakeup.wait(0.75)
                self.peer_wakeup.clear()
                if not self.peer_stop.is_set():
                    try:
                        self.drain_native_peers()
                    except (BridgeError, BusError, HarmonyError, sqlite3.Error, OSError) as error:
                        if (isinstance(error, WorkspaceRetired) or self._workspace_retired
                                or self.store._workspace_retired or self.bridge._workspace_retired):
                            self.peer_stop.set()
        self.peer_thread = threading.Thread(target=reconcile, name="avalokita-peer-mailbox", daemon=True)
        self.peer_thread.start()

    def stop_peer_reconciler(self):
        self.peer_stop.set()
        self.peer_wakeup.set()
        if self.peer_thread and self.peer_thread is not threading.current_thread():
            self.peer_thread.join(timeout=3)

    def drain_native_peers(self):
        if not self._drain_lock.acquire(blocking=False):
            return []
        release = None
        try:
            release = self.try_acquire_work()
            if release is None:
                return []
            with self.lock:
                self._guard_workspace()
                dispatcher = self.native_dispatcher
                if not dispatcher or not callable(getattr(dispatcher, "drain", None)):
                    return []
                dispatcher.callback_url = self.callback or None
            try:
                return dispatcher.drain(limit=1)
            except (BridgeError, OSError, ValueError, sqlite3.Error):
                return []
        finally:
            if release is not None:
                release()
            self._drain_lock.release()

    def prompt(self, payload):
        with self._work():
            return self._prompt(payload)

    def _prompt(self, payload):
        self._guard_workspace()
        if set(payload) - {"text", "idempotency_key", "reply_to", "recipient_ids", "collaborator_ids", "require_native_ipc", "budget", "ttl"}:
            raise BridgeError("Unknown prompt field")
        if (not isinstance(payload.get("idempotency_key"), str) or not payload["idempotency_key"].strip()
                or len(payload["idempotency_key"]) > 128):
            raise BridgeError("A stable idempotency_key is required")
        recipients, required = payload.get("recipient_ids"), payload.get("require_native_ipc", False)
        collaborators = payload.get("collaborator_ids")
        if type(required) is not bool:
            raise BridgeError("require_native_ipc must be boolean")
        if recipients is not None and (not isinstance(recipients, list) or not 1 <= len(recipients) <= 32
                or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in recipients)
                or len(set(recipients)) != len(recipients)):
            raise BridgeError("Exact recipient_ids are required")
        if collaborators is not None:
            if (not isinstance(collaborators, list) or not 1 <= len(collaborators) <= 32
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in collaborators)
                    or len(set(collaborators)) != len(collaborators) or not recipients
                    or not set(recipients).issubset(collaborators)):
                raise BridgeError("collaborator_ids must explicitly authorize a roster containing the initial recipient_ids")
        if required and (not recipients or len(recipients) != 1):
            raise BridgeError("Native-only dispatch requires one explicit existing Codex recipient")
        budget, ttl = payload.get("budget", 24), payload.get("ttl", 3600)
        if type(budget) is not int or not 1 <= budget <= 200 or type(ttl) not in {int, float} or not 1 <= ttl <= 86400:
            raise BridgeError("Human budget must be 1–200 turns and TTL 1–86400 seconds")
        if not isinstance(payload.get("text"), str) or not payload["text"].strip():
            raise HarmonyError("prompt must be nonempty selected text")
        self.store._selected_text(payload["text"])
        pending_delivery: list[tuple[int, str]] = []
        goal_placeholder = "0" * 36  # HarmonyStore generates UUID goal IDs.
        callback, helper = self.callback, str(HERE / "avalokita.py")

        def prepare_native_text():
            try:
                with (HERE / "HARMONY.md").open("rb") as policy_file:
                    encoded_policy = policy_file.read(MAX_PROMPT_POLICY_BYTES + 1)
                if len(encoded_policy) > MAX_PROMPT_POLICY_BYTES:
                    raise BridgeError("Local collaboration policy exceeds its size limit", 503)
                policy = encoded_policy.decode("utf-8", errors="strict")
            except (OSError, UnicodeError):
                raise BridgeError("Local collaboration policy is unavailable", 503) from None
            if not policy.strip() or "\x00" in policy:
                raise BridgeError("Local collaboration policy is unavailable", 503)
            text = ("Avalokita human goal. Keep your existing role and native context. "
                    "Coordinate as peers; no central model planner. Publish selected public results only. "
                    "Reply to the user in readable natural language, using the user's language. "
                    "Prior diagnostic/proof output formats do not apply to this new request unless the user asks for them. "
                    "Use the verified Sharebus job's own agent ID, native session ID and human root. "
                    f"Harmony goal: {goal_placeholder}. Local callback: {callback}. "
                    f"Automatic helper: {helper} peer --help. This helper reads only your exact workspace capability; "
                    "never print or share tokens. Peer work inherits this same root and pause/budget limits.\n"
                    "<avalokita_policy>\n" + policy + "\n</avalokita_policy>\n"
                    "<human_prompt>\n" + payload["text"] + "\n</human_prompt>")
            if len(text.encode("utf-8")) > MAX_NATIVE_PROMPT_BYTES:
                raise BridgeError("Prompt with collaboration policy must contain 1–65536 bytes")
            return text

        with self.store._connection() as db:
            existing = db.execute("SELECT id FROM goals WHERE idempotency_key=?", (payload["idempotency_key"],)).fetchone()
        prior = self.bridge.dispatches().get(existing["id"]) if existing else None
        recover_only = bool(prior and prior["state"] not in {"waiting", "paused"})
        native_text = None if recover_only else prepare_native_text()
        if existing is None and not any(key in payload for key in (
                "reply_to", "recipient_ids", "collaborator_ids", "require_native_ipc")):
            prepared = self.hosted.before_prompt()
            if prepared is not None and prepared.get("state") != "ready":
                raise BridgeError("Hosted provider CLI is unavailable", 409)
        with self.lock:
            dispatcher = self.native_dispatcher
            native_ids = []
            with self.store._connection() as db:
                existing = db.execute("SELECT id FROM goals WHERE idempotency_key=?", (payload["idempotency_key"],)).fetchone()
            prior = self.bridge.dispatches().get(existing["id"]) if existing else None
            recover_only = bool(prior and prior["state"] not in {"waiting", "paused"})
            if not recover_only and native_text is None:
                native_text = prepare_native_text()
            readiness = {}
            observed = self.bridge.observe()
            if dispatcher is not None and not recover_only:
                candidates = [agent for agent in observed["state"].get("agents", [])
                              if agent.get("provider") == "codex" and agent.get("mode") in {"native_attached", "external_attached"}
                              and agent.get("native_session_id") and (agent.get("state") == "idle"
                                  or (payload.get("reply_to") and agent.get("state") == "waiting_for_user"))
                              and (recipients is None or agent["id"] in recipients or agent["id"] in (collaborators or []))]
                native_ids = [agent["id"] for agent in candidates if dispatcher.available_for(
                    agent["id"], agent["native_session_id"]).get("state") == "metadata_verified"]
            if not recover_only:
                readiness = {agent["id"]: participant_readiness(agent, observed, native_ipc=agent["id"] in native_ids,
                    paused=self.bridge.paused(), reply=bool(payload.get("reply_to"))) for agent in observed["state"].get("agents", [])}
                if recipients and any(not readiness.get(actor, {}).get("can_dispatch") for actor in recipients):
                    raise BridgeError("An explicitly selected native session has no currently verified available route; no work was queued", 409)
                if collaborators and any(not readiness.get(actor, {}).get("can_dispatch") for actor in collaborators):
                    raise BridgeError("An explicitly authorized collaborator has no currently verified available route; no work was queued", 409)
            initial_native_ids = [actor for actor in native_ids if recipients is None or actor in recipients]
            if required and not recover_only and (not initial_native_ids or initial_native_ids != recipients or self.bridge.paused()):
                raise BridgeError("Pinned native IPC is pending capability or the exact recipient is unavailable; no human root was created", 409)
            goal = self.store.new_goal(payload.get("text"), budget=budget, ttl=ttl, idempotency_key=payload["idempotency_key"])
            if recover_only:
                # Bridge reconciliation reuses the original event; no new policy or body is sent.
                text = payload["text"]
            else:
                assert native_text is not None
                text = native_text.replace(f"Harmony goal: {goal_placeholder}.", f"Harmony goal: {goal['id']}.", 1)
            receipt = self.bridge.dispatch(goal, text, reply_to=payload.get("reply_to"), recipient_ids=recipients,
                                           collaborator_ids=collaborators,
                                           native_ipc_ids=native_ids, require_native_ipc=required, budget=budget, ttl=ttl)
            delivery: list[dict[str, Any]] = []
            if dispatcher is not None and receipt["state"] == "queued":
                dispatcher.callback_url = self.callback or None
                for actor in receipt.get("native_ipc_ids", []):
                    if receipt.get("recipient_ids") is not None and actor not in receipt["recipient_ids"]:
                        continue
                    try:
                        claim = dispatcher.reserve(goal["id"], actor)
                        if claim["state"] == "claimed":
                            pending_delivery.append((len(delivery), claim["dispatch_id"]))
                            delivery.append(claim)
                        else:
                            delivery.append(claim)
                    except (BridgeError, HarmonyError, OSError):
                        delivery.append({"state": "pending_review", "automatic_retry": False, "fallback_attempted": False})
            if receipt["state"] == "queued" and not pending_delivery:
                receipt = self.bridge.reconcile_dispatches().get(goal["id"], receipt)
        if dispatcher is not None:
            for index, dispatch_id in pending_delivery:
                try:
                    # Both transports retain their canonical revalidation;
                    # slow native I/O must not hold the Runtime control lock.
                    delivery[index] = dispatcher.deliver(dispatch_id)
                except (BridgeError, HarmonyError, OSError):
                    delivery[index] = {"state": "pending_review", "automatic_retry": False, "fallback_attempted": False}
        if pending_delivery and receipt["state"] == "queued":
            with self.lock:
                self._guard_workspace()
                receipt = self.bridge.reconcile_dispatches().get(goal["id"], receipt)
        receipt["durable_accepted"] = True
        receipt["skipped"] = [{"agent_id": actor, **value} for actor, value in readiness.items()
                              if not value["can_dispatch"] and (recipients is None or actor in recipients)]
        return {"goal": goal, "dispatch": receipt, "native_delivery": delivery}

    def control(self, action):
        with self.lock:
            self._guard_workspace()
            result = self.bridge.control(action)
            self.store.set_paused(result["paused"])
            return result

    def _recover_waiting(self):
        """Continue only saved human inputs which have never created bus work.

        Deterministic bridge IDs and its transaction recheck are the cross-
        process guard. No uncertain/native-attempted dispatch enters this path.
        """
        with self.lock:
            self._guard_workspace()
            if self.bridge.paused() or self.bridge.observe()["connection"]["state"] != "connected":
                return
            waiting = [(root, receipt) for root, receipt in self.bridge.dispatches().items() if receipt["state"] == "waiting"]
            if not waiting:
                return
            cursor = self.waiting_cursor % len(waiting)
            batch = (waiting[cursor:] + waiting[:cursor])[:8]
            self.waiting_cursor = (cursor + len(batch)) % len(waiting)
            requests = []
            for root, receipt in batch:
                if receipt["state"] != "waiting":
                    continue
                try:
                    goal = self.store.get_goal(root)
                    if goal["state"] != "active" or goal["expired"] or not goal["remaining"]:
                        continue
                    requests.append({"text": goal["prompt"], "idempotency_key": goal["idempotency_key"],
                                     "reply_to": receipt.get("reply_to"), "recipient_ids": receipt.get("recipient_ids"),
                                     "collaborator_ids": receipt.get("collaborator_ids"),
                                     "require_native_ipc": receipt.get("require_native_ipc", False),
                                     "budget": receipt.get("request_budget", 24), "ttl": receipt.get("request_ttl", 3600)})
                except (BridgeError, HarmonyError, OSError):
                    continue
        for request in requests:
            try:
                self.prompt(request)
            except (BridgeError, HarmonyError, OSError):
                # Exact explicit choices remain pending rather than being
                # silently broadened to a different peer or delivery path.
                continue

    def draft(self, value=None):
        """Operator-only private unsent text; never an executable human goal."""
        path = self.bridge.receipts.parent / "draft.json"
        guard_failure = None

        def guard():
            nonlocal guard_failure
            if guard_failure is not None:
                raise guard_failure
            try:
                self._guard_workspace_identity()
            except Exception as error:
                guard_failure = error
                raise

        with self.lock:
            self._guard_workspace()
            guard()
            BusStore.reject_linked_path(path)
            if value is None:
                guard()
                if not path.exists():
                    return {"draft": None}
                guard()
                BusStore.reject_linked_path(path)
                guard()
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 96 * 1024:
                    raise BridgeError("Private capability file is invalid", 403)
                guard()
                saved = json.loads(path.read_text(encoding="utf-8").strip())
                guard()
                return {"draft": saved, "updated_at": path.stat().st_mtime}
            if not isinstance(value, dict) or set(value) != {"text", "reply", "pending"}:
                raise BridgeError("Invalid private draft fields")
            text, reply, pending = value["text"], value["reply"], value["pending"]
            if not isinstance(text, str) or len(text.encode("utf-8")) > 65536 or "\x00" in text:
                raise BridgeError("Draft must be bounded text")
            if reply is not None and (not isinstance(reply, dict) or set(reply) != {"id", "label"}
                    or any(not isinstance(reply[key], str) or not 1 <= len(reply[key]) <= 512 for key in reply)):
                raise BridgeError("Invalid draft reply")
            if pending is not None and (not isinstance(pending, dict) or set(pending) != {"text", "key", "replyTo"}
                    or not isinstance(pending["text"], str) or len(pending["text"].encode("utf-8")) > 65536
                    or not isinstance(pending["key"], str) or not 1 <= len(pending["key"]) <= 128
                    or (pending["replyTo"] is not None and (not isinstance(pending["replyTo"], str) or not 1 <= len(pending["replyTo"]) <= 128))):
                raise BridgeError("Invalid pending draft identity")
            raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if len(raw.encode("utf-8")) > 96 * 1024:
                raise BridgeError("Private draft exceeds the local size limit")
            guard()
            exists = path.exists()
            if exists:
                guard()
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise BridgeError("Private draft cannot be linked", 403)
            temporary = path.with_name(".draft-" + secrets.token_hex(16) + ".tmp")
            body_error = None
            try:
                guard()
                with temporary.open("x", encoding="utf-8") as stream:
                    guard()
                    os.chmod(temporary, 0o600)
                    guard()
                    stream.write(raw)
                    guard()
                    stream.flush()
                    guard()
                    os.fsync(stream.fileno())
                guard()
                os.replace(temporary, path)
            except BaseException as error:
                body_error = error
                raise
            finally:
                if guard_failure is None:
                    try:
                        guard()
                        temporary.unlink(missing_ok=True)
                    except Exception:
                        if body_error is None:
                            raise
            guard()
            return {"saved": True, "updated_at": path.stat().st_mtime}

    def peer(self, token: str, payload: dict[str, Any]):
        with self._work() if payload.get("operation") == "send-keys" else nullcontext():
            with self.lock:
                self._guard_workspace()
                value = self._peer(token, payload)
            if (payload.get("operation") == "send-keys" and value.get("state") == "claimed"
                    and not value.get("duplicate_suppressed")):
                dispatcher = self.native_dispatcher
                if dispatcher is None:
                    raise BridgeError("Exact Codex peer IPC is pending capability", 409)
                value["native_delivery"] = dispatcher.deliver(value["dispatch_id"])
                value["state"] = "accepted"
            return value

    def _peer(self, token: str, payload: dict[str, Any]):
        """No client supplied actor can confer authority."""
        fields = {
            "publish": {"kind", "body", "recipient", "metadata"},
            "send-keys": {"recipient", "body", "kind", "idempotency_key", "parent_job_id", "parent_event_id"},
            "capture-pane": {"pane_id", "after", "limit"}, "claim": {"resources", "ttl"},
            "context": {"paths", "label"}, "read-context": {"snapshot_id"},
            "renew": {"lease_id", "fence", "ttl"}, "release": {"lease_id", "fence"},
            "handoff": {"lease_id", "fence", "to"},
        }
        op = payload.get("operation")
        if not isinstance(op, str) or op not in fields:
            raise BridgeError("Unknown peer operation")
        if set(payload) - ({"operation", "root_id", "actor"} | fields[op]):
            raise BridgeError("Unknown peer field; workspace and authority are fixed by this server", 403)
        read_only = op in {"capture-pane", "read-context"}
        if self.bridge.paused() and not read_only:
            raise BridgeError("Human paused Avalokita", 409)
        own = self.bridge.authenticate_peer(token, allow_paused=read_only)
        root = payload.get("root_id")
        receipt = self.bridge.peer_scope(own, root, read_only=read_only)
        goal = self.store.get_goal(root)
        if (goal["state"] != "active" or goal["expired"]) and not read_only:
            raise BridgeError("The original human goal is not active", 409)
        if "actor" in payload and payload["actor"] != own["id"]:
            raise BridgeError("A peer cannot impersonate another actor", 403)
        if op in {"context", "claim"}:
            paths = payload.get("paths" if op == "context" else "resources")
            if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
                raise BridgeError("Select a bounded workspace resource list")
            for path in paths:
                self.store._path(path, resource=op == "claim")
        if op in {"renew", "release", "handoff"}:
            lease = self.store.get_lease(payload.get("lease_id"))
            if lease.get("root_id") != root:
                raise BridgeError("Lease belongs to a different human root", 403)
        if op == "read-context":
            with self.store._connection() as db:
                selected = db.execute("SELECT root_id FROM contexts WHERE id=?", (payload.get("snapshot_id"),)).fetchone()
            if not selected:
                raise BridgeError("Context snapshot not found", 404)
            if selected["root_id"] != root:
                raise BridgeError("Context belongs to a different human root", 403)
        if op == "capture-pane" and payload.get("pane_id", own["id"]) not in {*receipt.get("participants", []), "human"}:
            raise BridgeError("Pane is outside the original human roster", 403)
        if op == "publish" and payload.get("recipient") not in {None, "human", *receipt.get("participants", [])}:
            raise BridgeError("Recipient is outside the original human roster", 403)
        if op == "send-keys":
            NativeDispatcher.peer_transport_preflight(self.bridge, token, root, payload.get("recipient"),
                parent_job_id=payload.get("parent_job_id"), parent_event_id=payload.get("parent_event_id"),
                reason_transport=self.reason_transport)
        self.store.register_pane(own["id"], own["provider"], own.get("role") or own.get("label") or "peer",
                                 own["native_session_id"], own.get("capabilities") or [], verified=True)
        actor = own["id"]
        if op == "publish":
            return self.store.publish(actor, payload.get("kind", "message"), payload.get("body", ""), root_id=root,
                                      recipient=payload.get("recipient"), metadata=payload.get("metadata"))
        if op == "send-keys":
            recipient = payload.get("recipient")
            binding = receipt.get("bindings", {}).get(recipient)
            if recipient != "user" and not binding:
                raise BridgeError("Recipient is outside the immutable original human roster", 403)
            if binding and binding[0] == "codex" and not str(binding[1]).startswith("avalokita-hosted/1:"):
                if not self.native_dispatcher:
                    raise BridgeError("Exact Codex peer IPC is pending capability", 409)
                self.native_dispatcher.callback_url = self.callback or None
                value = self.native_dispatcher.peer_send(token, root, recipient, payload.get("body"), payload.get("kind", "message"),
                    payload.get("idempotency_key"), parent_job_id=payload.get("parent_job_id"), parent_event_id=payload.get("parent_event_id"))
                self.peer_wakeup.set()
                return value
            return self.bridge.peer_send(token, payload.get("recipient"), payload.get("body"), payload.get("kind", "message"), receipt["root_id"], payload.get("idempotency_key"),
                                         parent_job_id=payload.get("parent_job_id"), parent_event_id=payload.get("parent_event_id"))
        if op == "capture-pane":
            return self.store.capture(payload.get("pane_id", actor), payload.get("after", 0), payload.get("limit", 100), root_id=root)
        if op == "claim":
            return self.store.claim(actor, payload.get("resources"), payload.get("ttl", 120), root_id=root)
        if op == "context":
            return self.store.publish_context(actor, payload.get("paths"), payload.get("label", ""), root_id=root)
        if op == "read-context":
            return self.store.read_context(payload.get("snapshot_id"), root_id=root)
        if op in {"renew", "release", "handoff"}:
            lease = self.store.get_lease(payload.get("lease_id"))
            if not lease or lease.get("root_id") != root:
                raise BridgeError("Lease belongs to a different human root", 403)
            args = (actor, payload.get("lease_id"), payload.get("fence"))
            if op == "renew":
                return self.store.renew(*args, payload.get("ttl", 120), root_id=root)
            if op == "handoff":
                return self.store.handoff(*args, payload.get("to"), root_id=root)
            return self.store.release(*args, root_id=root)
        raise BridgeError("Unknown peer operation")

    def _context_manifests(self, goal_ids):
        """Selected snapshot metadata only; never re-open current source files."""
        if not goal_ids:
            return [], 0
        placeholders = ",".join("?" for _ in goal_ids)
        with self.store._connection() as db:
            condition = f"c.root_id IN ({placeholders}) AND EXISTS(SELECT 1 FROM panes p WHERE p.id=c.actor AND p.verified=1)"
            total = db.execute("SELECT COUNT(*) FROM contexts c WHERE " + condition, goal_ids).fetchone()[0]
            contexts = [dict(row) for row in db.execute("SELECT c.id,c.actor,c.label,c.generation,c.total_bytes,c.manifest_sha256,c.created_at,c.root_id "
                "FROM contexts c WHERE " + condition + " ORDER BY c.generation DESC LIMIT 50", goal_ids)]
            for context in contexts:
                files = db.execute("SELECT json_extract(value,'$.path') AS path,json_extract(value,'$.sha256') AS sha256,"
                    "json_extract(value,'$.bytes') AS bytes FROM json_each((SELECT files FROM contexts WHERE id=?)) LIMIT 32", (context["id"],)).fetchall()
                context["files"] = [dict(row) for row in files]
                context.update(content_included=False, freshness="not_rechecked")
        return contexts, total

    def summary(self):
        """Operator observation only: no recovery, drain, native probe or bus HTTP."""
        self._guard_workspace()
        now = time.time()
        with self.store._connection() as db:
            counts = {name: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                      for name, table in (("goals", "goals"), ("messages", "events"))}
            counts["published_panes"] = db.execute("SELECT COUNT(*) FROM panes WHERE verified=1").fetchone()[0]
            counts["active_goals"] = db.execute("SELECT COUNT(*) FROM goals WHERE state='active' AND deadline>? AND used<budget", (now,)).fetchone()[0]
            counts["leases"] = db.execute("SELECT COUNT(*) FROM leases l WHERE EXISTS(SELECT 1 FROM goals g WHERE g.id=l.root_id)").fetchone()[0]
            counts["active_leases"] = db.execute("SELECT COUNT(*) FROM leases l WHERE state='active' AND expires_at>? "
                "AND EXISTS(SELECT 1 FROM goals g WHERE g.id=l.root_id)", (now,)).fetchone()[0]
            counts["contexts"] = db.execute("SELECT COUNT(*) FROM contexts c WHERE EXISTS(SELECT 1 FROM goals g WHERE g.id=c.root_id) "
                "AND EXISTS(SELECT 1 FROM panes p WHERE p.id=c.actor AND p.verified=1)").fetchone()[0]
            goal_ids = [row[0] for row in db.execute("SELECT id FROM goals ORDER BY created_at DESC,id DESC LIMIT 50")]
            cursor = db.execute("SELECT COALESCE(MAX(seq),0) FROM events").fetchone()[0]
        observation = self.bridge.public_inspector(goal_ids, limit=1)
        counts.update(observation["totals"])
        counts["participants"] = observation["participants"] if observation["participants"] is not None else 0
        _, discovery_status = self.device_discovery.snapshot()
        return {"workspace": {"path": str(self.store.workspace), "id": self.store.workspace_id, "name": self.store.workspace.name},
                "counts": counts, "cursor": cursor, "observed_at": now, "discovery_status": discovery_status,
                "service": self.hosted.service_snapshot(),
                "inventory": self.inventory.snapshot(),
                "inspection": {"status": observation["status"], "error_code": observation["error_code"],
                               "scope": "latest_50_goals", "bounded": True, "goals_truncated": counts["goals"] > len(goal_ids)}}

    def state(self):
        self._guard_workspace()
        self._recover_waiting()
        self.drain_native_peers()
        observed = self.bridge.observe()
        agents = observed["state"].get("agents", [])
        dispatches = self.bridge.reconcile_dispatches()
        with self.store._connection() as db:
            import_goals = {row["id"]: dict(row) for row in db.execute("SELECT id,state,deadline,budget,used FROM goals ORDER BY created_at DESC,id DESC LIMIT 50")}
            goal_window = list(import_goals)
            import_paused = db.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "1"
        import_remaining = {key: max(0, value["budget"] - value["used"]) for key, value in import_goals.items()}
        try:
            selected = self.bridge.selected_outputs(goal_ids=goal_window)
        except (BridgeError, OSError):
            selected = []
        imported_keys = set()
        if selected:
            keys = ["sharebus:" + event["id"] for event in selected]
            with self.store._connection() as db:
                imported_keys = {(row["actor"], row["idempotency_key"]) for row in db.execute(
                    "SELECT actor,idempotency_key FROM events WHERE idempotency_key IN (" + ",".join("?" for _ in keys) + ")", keys)}
        synced = set()
        for event in selected:
            try:
                if (event["sender"], "sharebus:" + event["id"]) in imported_keys:
                    continue
                kind = event["kind"] if event["kind"] in {"message", "proposal", "result", "question"} else "status"
                completed_observation = kind == "result" and event.get("completion_verified")
                hint = import_goals.get(event["goal_id"])
                # Skip only observations the existing kernel would reject.
                # This is a read-only hint: publish still checks current control
                # and budget, and skipped source IDs are never marked imported.
                # Correlated completed history always reaches its import path.
                if not completed_observation and hint and (import_paused or hint["state"] != "active" or hint["deadline"] <= time.time()
                        or kind in {"message", "proposal", "question"} and not import_remaining[event["goal_id"]]):
                    continue
                if event["sender"] not in synced:
                    self.store.register_pane(event["sender"], event["provider"], event["role"], event["native_session_id"],
                                             event["capabilities"], verified=True)
                    synced.add(event["sender"])
                if completed_observation:
                    self.store.import_selected_result(event["sender"], event["body"], root_id=event["goal_id"],
                        native_session_id=event["native_session_id"], source_event_id=event["id"],
                        source_job_id=event["source_job_id"], source_created_at=event["created_at"])
                else:
                    recipient = event.get("recipient")
                    if recipient not in {None, "all", "user", event["sender"]}:
                        target = next((a for a in agents if a["id"] == recipient), None)
                        if target and target["id"] not in synced:
                            self.store.register_pane(target["id"], target["provider"], target.get("role") or target.get("label") or "peer",
                                target["native_session_id"], target.get("capabilities") or [], verified=True)
                            synced.add(target["id"])
                    imported = self.store.publish(event["sender"], kind, event["body"], root_id=event["goal_id"],
                                       recipient="human" if recipient == "user" else None if recipient == "all" else recipient,
                                       metadata={"idempotency_key": "sharebus:" + event["id"], "source": "sharebus_selected_output",
                                                 "source_created_at": event["created_at"]})
                    if kind in {"message", "proposal", "question"}:
                        import_remaining[event["goal_id"]] = min(import_remaining[event["goal_id"]], imported["hop_budget"])
            except (HarmonyError, ValueError):
                # A revoked/expired root cannot be revived by importing late output.
                pass
        if self.native_dispatcher:
            self._guard_workspace()
            try:
                self.native_dispatcher.reconcile_completed(limit=8)
            except (BridgeError, OSError, ValueError, sqlite3.Error):
                pass
        snapshot = self.store.snapshot()
        goal_ids = [goal["id"] for goal in snapshot["goals"]]
        inspector_data = self.bridge.public_inspector(goal_ids)
        contexts, context_total = self._context_manifests(goal_ids)
        discovery, discovery_status = self.device_discovery.snapshot()
        providers = {a["id"]: a.get("provider", "") for a in agents}
        messages = [{"id": m["id"], "seq": m["sequence"], "actor": m["actor"],
                     "provider": providers.get(m["actor"], "human" if m["actor"] == "human" else ""),
                     "body": m["body"], "kind": m["kind"], "created_at": m["created_at"],
                     "recipient": m.get("recipient"), "root_id": m.get("root_id")} for m in snapshot["messages"]]
        latest = {m["actor"]: m["body"] for m in messages}
        connected = observed["connection"]["state"] == "connected"
        native_ready = {a["id"] for a in agents if self.native_dispatcher and a.get("provider") == "codex"
                        and a.get("state") == "idle" and a.get("mode") in {"native_attached", "external_attached"}
                        and a.get("native_session_id") and self.native_dispatcher.available_for(
                            a["id"], a["native_session_id"]).get("state") == "metadata_verified"}
        participants = [{"id": a["id"], "provider": a.get("provider", "unknown"),
                         "name": a.get("label") or a["id"], "role": a.get("role") or "peer",
                         "status": a.get("state", "unknown") if connected else "offline",
                         "attachment": a.get("mode", "registered"), "verified": bool(a.get("channel_verified")) and connected,
                         "native_session_bound": bool(a.get("native_session_id") and (a.get("mode") in {"native_attached", "external_attached"} or is_hosted(a))),
                         "transport": a.get("transport", "native_attached"), "identity_state": a.get("identity_state", "observed" if a.get("native_session_id") else "pending"),
                         "readiness": participant_readiness(a, observed, native_ipc=a["id"] in native_ready, paused=self.bridge.paused()),
                         "capabilities": a.get("capabilities") or [], "latest_message": latest.get(a["id"], "")}
                        for a in agents]
        by_source = {"sharebus:" + event["id"]: event for event in selected}
        for message in messages:
            source = next((event for event in snapshot["messages"] if event["id"] == message["id"]), None)
            public = by_source.get(source.get("idempotency_key")) if source else None
            if public:
                message.update(source_event_id=public["id"], root_id=public["goal_id"], recipient=public.get("recipient"), kind=public["kind"])
                if public["kind"] == "question" and public.get("question_pending"):
                    message.update(question_id=public["id"], question_pending=True)
        return {"workspace": {"path": str(self.store.workspace), "id": self.store.workspace_id,
                              "name": self.store.workspace.name},
                "connection": {**observed["connection"], "paused": self.bridge.paused()},
                "participants": participants, "messages": messages, "goals": snapshot["goals"],
                "leases": snapshot["leases"], "team": self.bridge.team(workspace_guard=self._guard_workspace_identity), "discovery": discovery, "discovery_status": discovery_status,
                "jobs": inspector_data["jobs"], "contexts": contexts, "artifacts": inspector_data["artifacts"],
                "inspector": {"status": inspector_data["status"], "error_code": inspector_data["error_code"], "observed_at": time.time(),
                    "scope": "latest_50_goals", "limits": {"goals": 50, "messages": 100, "selected_events": 200, "leases": 200, "jobs": 200, "contexts": 50, "artifacts": 100},
                    "totals": {**inspector_data["totals"], "contexts": context_total},
                    "truncated": {**inspector_data["truncated"], "contexts": context_total > len(contexts), "messages": snapshot["truncated"],
                                  "selected_events": len(selected) == 200}},
                "cursor": snapshot["cursor"], "dispatches": {key: {k: v for k, v in value.items() if k != "bindings"} for key, value in dispatches.items()}}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, workspace, host="127.0.0.1", port=0, *, instance_id=None, assets=None, runtime=None):
        if host != "127.0.0.1":
            raise ValueError("Only exact IPv4 loopback is supported")
        self.runtime = runtime or Runtime(workspace)
        self.token = secrets.token_urlsafe(32)
        self.owner_token = secrets.token_urlsafe(32)
        self.instance_id = instance_id or secrets.token_hex(24)
        self.owner_workspace = str(self.runtime.store.workspace)
        self.owner_pid = os.getpid()
        self._server_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._serving = threading.Event()
        self._close_requested = threading.Event()
        self._closed = threading.Event()
        self._serve_thread = None
        self._shutdown_thread = None
        self.assets = Path(assets or HERE / "app/dist").resolve()
        super().__init__((host, port), Handler)
        self.url = f"http://127.0.0.1:{self.server_port}"
        self.runtime.callback = self.url

    def serve_forever(self, poll_interval=0.5):
        with self._server_lock:
            if self._close_requested.is_set() or self._closed.is_set():
                return
            self._serve_thread = threading.current_thread()
        try:
            self.runtime.device_discovery.start()
            self.runtime.inventory.start()
            self.runtime.start_peer_reconciler()
            with self._server_lock:
                if self._close_requested.is_set():
                    return
                self._serving.set()
            return super().serve_forever(poll_interval=poll_interval)
        finally:
            with self._server_lock:
                self._serving.clear()
                self._serve_thread = None
            self.runtime.stop_peer_reconciler()
            self.runtime.device_discovery.stop()
            self.runtime.inventory.close()

    def begin_closing(self):
        self.runtime.begin_closing()
        self._close_requested.set()

    def service_actions(self):
        # Called by BaseServer only after the actual serving loop has begun.
        if not self._close_requested.is_set():
            self.runtime.hosted.start_service()

    def schedule_shutdown(self):
        """Called after the ACK attempt; no workspace or DB access is needed."""
        with self._server_lock:
            if self._shutdown_thread is not None or self._closed.is_set():
                return
            worker = threading.Thread(target=self._shutdown_owned, name="avalokita-owner-shutdown", daemon=True)
            self._shutdown_thread = worker
        try:
            worker.start()
        except BaseException:
            self.server_close()
            raise

    def _shutdown_owned(self):
        try:
            # This covers admitted synchronous work, not every HTTP handler or
            # a future external native turn. The Desktop has its own deadline.
            self.runtime.wait_idle(3)
        finally:
            self.server_close()

    def server_close(self):
        self.begin_closing()
        with self._close_lock:
            if self._closed.is_set():
                return
            try:
                with self._server_lock:
                    serving = self._serving.is_set() and self._serve_thread is not threading.current_thread()
                if serving:
                    self.shutdown()
                self.runtime.stop_peer_reconciler()
                self.runtime.device_discovery.stop()
                self.runtime.inventory.close()
                self.runtime.hosted.close()
            finally:
                try:
                    super().server_close()
                finally:
                    self._closed.set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass  # Never record human input, bearer capabilities, or cookie headers.

    def _send(self, status, body, content_type="application/json; charset=utf-8", *, cookie=False):
        raw = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, (dict, list)) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", f"avalokita={self.server.token}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(raw)

    def _gate(self, *, authenticated=True):
        if self.headers.get_all("Host") != [f"127.0.0.1:{self.server.server_port}"]:
            raise BridgeError("Exact loopback Host is required", 403)
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.url]:
            raise BridgeError("Foreign Origin is forbidden", 403)
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            raise BridgeError("Same-origin requests only", 403)
        if authenticated:
            if self.headers.get("X-Avalokita-Request") != "1":
                raise BridgeError("CSRF request marker required", 403)
            bearer = self._bearer()
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get("Cookie", ""))
            except Exception:
                raise BridgeError("Invalid session", 401) from None
            cookie = cookies.get("avalokita")
            supplied = bearer or (cookie.value if cookie else "")
            if not supplied or not secrets.compare_digest(supplied, self.server.token):
                raise BridgeError("Local session required", 401)

    def _bearer(self):
        value = self.headers.get("Authorization", "")
        return value[7:] if value.startswith("Bearer ") else ""

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise BridgeError("Chunked requests are unsupported", 400)
        if self.headers.get_content_type() != "application/json":
            raise BridgeError("JSON content type required", 415)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise BridgeError("A single content length is required", 411)
        length = int(lengths[0])
        if length > MAX_REQUEST:
            raise BridgeError("Request too large", 413)
        self.connection.settimeout(5)
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise BridgeError("JSON object required")
        return value

    def _owner_shutdown(self):
        self._gate(authenticated=False)
        if self.headers.get_all("Sec-Fetch-Site", []) not in ([], ["same-origin"], ["none"]):
            raise BridgeError("Same-origin requests only", 403)
        if self.headers.get_all("X-Avalokita-Request", []) != ["1"]:
            raise BridgeError("CSRF request marker required", 403)
        if self.headers.get_all("X-Avalokita-Instance", []) != [self.server.instance_id]:
            raise BridgeError("Exact owned instance required", 403)
        authorization = self.headers.get_all("Authorization", [])
        if (len(authorization) != 1 or not authorization[0].startswith("Bearer ")
                or not secrets.compare_digest(authorization[0][7:], self.server.owner_token)):
            raise BridgeError("Owned backend capability required", 401)
        if self.headers.get_all("Transfer-Encoding", []):
            raise BridgeError("Transfer encoding is unsupported", 400)
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise BridgeError("A single content length is required", 411)
        length = int(lengths[0])
        if not 0 < length <= 4096:
            raise BridgeError("Owner request must be 1–4096 bytes", 413)
        if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
            raise BridgeError("JSON content type required", 415)
        self.connection.settimeout(1)

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise BridgeError("Duplicate owner field", 400)
                value[key] = item
            return value

        raw = self.rfile.read(length)
        if len(raw) != length:
            raise BridgeError("Incomplete owner request", 400)
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        expected = {"contract": "avalokita-owner-shutdown/1", "instance_id": self.server.instance_id,
                    "pid": self.server.owner_pid, "workspace": self.server.owner_workspace}
        if (not isinstance(body, dict) or body != expected or set(body) != set(expected)
                or type(body.get("pid")) is not int):
            raise BridgeError("Exact owned backend identity required", 403)
        self.server.begin_closing()
        self.close_connection = True
        try:
            self._send(202, {"ok": True, "closing": True})
        finally:
            self.server.schedule_shutdown()

    def _handle(self, method):
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            if path.startswith("/api/") and (parsed.query or parsed.fragment):
                raise BridgeError("API query fields are unsupported; workspace is fixed by this server", 400)
            if method == "POST" and path == "/api/owner/shutdown":
                return self._owner_shutdown()
            self._gate(authenticated=False)
            self.server.runtime._guard_workspace()
            if method == "GET" and path == "/health":
                dispatcher = self.server.runtime.native_dispatcher
                native_context = bool(dispatcher and os.environ.get("CODEX_THREAD_ID") and dispatcher.source == os.environ.get("CODEX_THREAD_ID"))
                return self._send(200, {"ok": True, "pid": os.getpid(), "instance_id": self.server.instance_id,
                                        "workspace": str(self.server.runtime.store.workspace),
                                        "native_ipc": {"contract": "avalokita-native-ipc/1", "enabled": dispatcher is not None,
                                                       "source_context_available": native_context,
                                                       "routing_source_available": bool(dispatcher and dispatcher.source and dispatcher._source_allowed(dispatcher.source)),
                                                       "routing_source": "observed_environment" if native_context else "remembered_environment" if dispatcher and dispatcher.routing else "unavailable"}})
            if method == "GET" and path == "/api/session":
                self._gate()
                return self._send(200, {"ok": True}, cookie=True)
            if path == "/api/peer" and method == "POST":
                if self.headers.get("X-Avalokita-Request") != "1" or not self._bearer():
                    raise BridgeError("Native bearer capability required", 401)
                return self._send(200, self.server.runtime.peer(self._bearer(), self._body()))
            if path.startswith("/api/"):
                self._gate()
                runtime = self.server.runtime
                if method == "GET" and path == "/api/state":
                    return self._send(200, runtime.state())
                if method == "GET" and path == "/api/summary":
                    return self._send(200, runtime.summary())
                if method == "GET" and path == "/api/team":
                    return self._send(200, runtime.bridge.team(workspace_guard=runtime._guard_workspace_identity))
                if method == "GET" and path == "/api/draft":
                    return self._send(200, runtime.draft())
                if method == "GET" and path == "/api/hosted":
                    return self._send(200, runtime.hosted.state())
                if method == "POST":
                    payload = self._body()
                    if path == "/api/prompt":
                        return self._send(200, runtime.prompt(payload))
                    if path == "/api/control":
                        if set(payload) != {"action"}:
                            raise BridgeError("Unknown control field")
                        return self._send(200, runtime.control(payload.get("action")))
                    if path == "/api/team":
                        if set(payload) - {"team", "expected_revision"}:
                            raise BridgeError("Unknown team field")
                        return self._send(200, runtime.bridge.save_team(payload.get("team"), payload.get("expected_revision"),
                                                                     workspace_guard=runtime._guard_workspace_identity))
                    if path == "/api/draft":
                        return self._send(200, runtime.draft(payload))
                    if path == "/api/hosted":
                        return self._send(200, runtime.configure_hosted(payload))
                    if path == "/api/hosted/repair-startup":
                        return self._send(200, runtime.repair_hosted_startup(payload))
                    if path == "/api/hosted/approval":
                        return self._send(200, runtime.reply_hosted(payload))
                    if path == "/api/discovery/refresh":
                        if payload:
                            raise BridgeError("Discovery does not accept paths, process names or authority")
                        runtime.device_discovery.refresh()
                        rows, status = runtime.device_discovery.snapshot()
                        return self._send(200, {"discovery": rows, "discovery_status": status})
                raise BridgeError("Endpoint not found", 404)
            if method != "GET":
                raise BridgeError("Endpoint not found", 404)
            relative = path.lstrip("/") or "index.html"
            candidate = (self.server.assets / relative).resolve()
            if not candidate.is_relative_to(self.server.assets):
                raise BridgeError("Invalid asset path", 403)
            if not candidate.is_file():
                if "." in Path(relative).name:
                    raise BridgeError("Asset not found", 404)
                candidate = self.server.assets / "index.html"
            if not candidate.is_file():
                return self._send(503, {"error": {"message": "Build the Avalokita app before opening the desktop window"}})
            return self._send(200, candidate.read_bytes(), mimetypes.guess_type(candidate.name)[0] or "application/octet-stream")
        except (BridgeError, BusError, HarmonyError) as exc:
            self.close_connection = True
            self._send(getattr(exc, "status", 400), {"error": {"message": str(exc)}})
        except (ValueError, TypeError, KeyError):
            self.close_connection = True
            self._send(400, {"error": {"message": "Invalid request"}})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            self._send(500, {"error": {"message": "Local operation failed; original sessions are preserved"}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def _cleanup_ready(server, target):
    try:
        server.runtime._guard_workspace_identity()
        BusStore.reject_linked_path(target)
        server.runtime._guard_workspace_identity()
        value = json.loads(target.read_text(encoding="utf-8"))
        expected = {"instance_id": server.instance_id, "pid": os.getpid(),
                    "workspace": str(server.runtime.store.workspace)}
        if all(value.get(key) == item for key, item in expected.items()):
            server.runtime._guard_workspace_identity()
            target.unlink()
    except (BridgeError, BusError, HarmonyError, OSError, ValueError, TypeError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file")
    parser.add_argument("--instance-id")
    parser.add_argument("--assets")
    args = parser.parse_args()
    if not Path(args.workspace).is_absolute():
        parser.error("workspace must be absolute")
    server = Server(args.workspace, args.host, args.port, instance_id=args.instance_id, assets=args.assets)
    target = Path(args.ready_file) if args.ready_file else server.runtime.store.private / "runtime.json"
    guard_failure = None
    created = False

    def guard():
        nonlocal guard_failure
        if guard_failure is not None:
            raise guard_failure
        try:
            server.runtime._guard_workspace_identity()
        except Exception as error:
            guard_failure = error
            raise

    try:
        if not target.is_absolute():
            parser.error("ready-file must be absolute")
        ready = {"url": server.url, "pid": os.getpid(), "workspace": str(server.runtime.store.workspace),
                 "instance_id": server.instance_id, "token": server.token, "owner_token": server.owner_token}
        guard()
        BusStore.reject_linked_path(target)
        guard()
        if target.exists():
            raise ValueError("Ready file already exists; preserve and review its owner")
        guard()
        target.parent.mkdir(parents=True, exist_ok=True)
        guard()
        with target.open("x", encoding="utf-8") as stream:
            created = True
            guard()
            stream.write(json.dumps(ready))
        guard()
        os.chmod(target, 0o600)

        def stop(*_):
            server.begin_closing()
            server.schedule_shutdown()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(json.dumps({key: ready[key] for key in ("url", "pid", "workspace", "instance_id")}), flush=True)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        if created and guard_failure is None:
            _cleanup_ready(server, target)



if __name__ == "__main__":
    main()
