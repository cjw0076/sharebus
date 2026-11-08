"""Bounded exact Codex admission, with canonical Harmony/Sharebus authority.

Human inputs and authenticated peer continuations retain the same durable human
authority. Source thread metadata is routing, not an identity proof. Native
acceptance and authenticated completion are separate.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import uuid
from urllib.parse import urlsplit

from bridge import BridgeError, BusError, BusStore, NATIVE_MODES, SharebusBridge, private_read, agent_binding
from harmony import HarmonyStore
from adapters.codex_app_tools import CodexAppToolsAdapter, CodexPipeError, PROVENANCE, SUPPORTED_PROVENANCES
from adapters.canonical_reason import CanonicalReasonTransport
from adapters.reason_engine import NativeBinding, ReasonEngineAdapter, ReasonEngineError

HERE = Path(__file__).resolve().parent
TABLE = "avalokita_native_dispatch"
PEERS = "avalokita_native_peers"
ENVELOPES = "avalokita_native_envelopes"
TRANSPORTS = "avalokita_native_transports"
GRANTS = "avalokita_native_reason_grants"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class NativeDispatcher:
    def __init__(self, bridge: SharebusBridge, *, source_thread_id: str | None = None, adapter_factory=None, callback_url: str | None = None, harmony_store=None,
                 reason_transport: CanonicalReasonTransport | None = None):
        self.bridge = bridge
        if reason_transport is not None and not isinstance(reason_transport, CanonicalReasonTransport):
            raise BridgeError("Trusted exact Reason transport configuration required", 403)
        self.reason_transport = reason_transport
        self.source = source_thread_id or os.environ.get("CODEX_THREAD_ID")
        self.adapter_factory = adapter_factory or CodexAppToolsAdapter
        if callback_url:
            parsed = urlsplit(callback_url)
            if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path not in {"", "/"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise BridgeError("Exact own loopback callback required")
        self.callback_url = callback_url
        # Resolve once, before any canonical writer is acquired. Routing and
        # peer helpers must never initialize another store inside admission.
        self.harmony = harmony_store if harmony_store is not None else HarmonyStore(self.bridge.workspace)
        self.routing_path = self.bridge.receipts.parent / "codex-routing.json"
        self.routing = None
        self.routing_error = None
        self.routing_checked = False
        self.drain_after = 0
        self.reason_reconcile_after = 0
        try:
            self.routing = self._load_routing()
            if not self.source and self.routing:
                self.source = self.routing["source_thread_id"]
        except (BridgeError, OSError, ValueError, KeyError, TypeError):
            self.routing_error = "Saved native routing metadata is invalid for this workspace"

    def _routing_key(self):
        return private_read(self.bridge.private / "operator.token", workspace_guard=self.bridge._guard_workspace_identity).encode("utf-8")

    @staticmethod
    def _routing_bytes(record):
        return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

    def _load_routing(self):
        self.bridge._guard_workspace_identity()
        if not self.routing_path.exists():
            return None
        value = json.loads(private_read(self.routing_path, workspace_guard=self.bridge._guard_workspace_identity))
        record, seal = value["record"], value["seal"]
        store = self.harmony
        if (not isinstance(seal, str) or not hmac.compare_digest(seal, hmac.new(self._routing_key(), self._routing_bytes(record), hashlib.sha256).hexdigest())
                or record.get("version") != 1 or record.get("workspace") != str(self.bridge.workspace)
                or record.get("workspace_id") != store.workspace_id or PROVENANCE not in SUPPORTED_PROVENANCES
                or record.get("provenance") not in SUPPORTED_PROVENANCES
                or record.get("origin") != "observed_CODEX_THREAD_ID"
                or record.get("metadata_is_native_attestation") is not False
                or record.get("source_thread_existence_verified") is not False
                or not isinstance(record.get("owner_sid_sha256"), str) or len(record["owner_sid_sha256"]) != 64
                or type(record.get("observed_at")) not in {int, float} or not 0 < record["observed_at"] <= time.time() + 60):
            raise BridgeError("Saved routing bootstrap does not match the trusted workspace", 403)
        CodexAppToolsAdapter._identifier(record.get("source_thread_id"))
        return record

    def _source_allowed(self, source, *, for_probe=False):
        # This is source routing, not native attestation or human authority.
        if source != self.source:
            return False
        actual = os.environ.get("CODEX_THREAD_ID")
        try:
            if self.routing_error or (self.routing is not None and self._load_routing() != self.routing):
                return False
            return source == actual if actual else bool(self.routing is not None and (for_probe or self.routing_checked))
        except (BridgeError, OSError, ValueError, KeyError, TypeError):
            return False

    def _remember_source(self, metadata, *, migrate=False):
        if migrate:
            if (not self.routing or self._load_routing() != self.routing or self.routing["provenance"] not in SUPPORTED_PROVENANCES
                    or self.routing["source_thread_id"] != self.source or metadata["owner_sid_sha256"] != self.routing["owner_sid_sha256"]):
                raise BridgeError("Known sealed routing metadata changed before migration", 403)
        elif self.source != os.environ.get("CODEX_THREAD_ID"):
            raise BridgeError("Only an observed native environment can seed routing metadata", 403)
        store = self.harmony
        record = {"version": 1, "workspace": str(self.bridge.workspace), "workspace_id": store.workspace_id,
                  "source_thread_id": self.source, "observed_at": time.time(), "origin": "observed_CODEX_THREAD_ID",
                  "owner_sid_sha256": metadata["owner_sid_sha256"], "provenance": PROVENANCE,
                  "metadata_is_native_attestation": False, "source_thread_existence_verified": False}
        if migrate:
            record.update(observed_at=self.routing["observed_at"], migrated_at=time.time())
        value = {"record": record, "seal": hmac.new(self._routing_key(), self._routing_bytes(record), hashlib.sha256).hexdigest()}
        BusStore.reject_linked_path(self.routing_path)
        if self.routing_path.exists() and self.routing_path.stat().st_nlink != 1:
            raise BridgeError("Private routing metadata cannot be linked", 403)
        temporary = self.routing_path.with_name(".routing-" + secrets.token_hex(16) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                stream.write(json.dumps(value, ensure_ascii=False))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.routing_path)
        finally:
            temporary.unlink(missing_ok=True)
        self.routing = record
        self.routing_error = None

    def available(self):
        """A verified same-user pipe is capability metadata, never a sent turn."""
        if not self.source or not self._source_allowed(self.source, for_probe=True):
            return {"state": "pending_capability", "reason": self.routing_error or "No observed native routing source is saved in this workspace"}
        try:
            adapter = self.adapter_factory(validate_source=lambda source: self._source_allowed(source, for_probe=True))
            metadata = (adapter.probe_metadata(self.source) if not self.routing_checked
                        or self.routing is None or self.routing.get("source_thread_id") != self.source
                        or self.routing.get("provenance") != PROVENANCE else adapter.discover())
            if self.routing is None or self.routing.get("source_thread_id") != self.source:
                # Compatibility of this source field is not proof that its
                # conversation exists, is active, or authorizes any operation.
                self._remember_source(metadata)
            elif metadata.get("owner_sid_sha256") != self.routing["owner_sid_sha256"]:
                raise CodexPipeError("Saved routing owner changed")
            elif self.routing.get("provenance") != PROVENANCE:
                # An exact known old HMAC record supplies routing only. A fresh
                # current pinned probe and unchanged same-user SID precede reseal.
                self._remember_source(metadata, migrate=True)
            self.routing_checked = True
            return {**metadata, "state": "metadata_verified", "routing_source": "observed_environment" if os.environ.get("CODEX_THREAD_ID") else "remembered_environment",
                    "source_thread_existence_verified": False, "metadata_is_native_attestation": False}
        except (CodexPipeError, BridgeError, OSError, KeyError, ValueError):
            return {"state": "pending_capability", "reason": "Pinned original Codex IPC is unavailable"}

    def available_for(self, actor_id: str, native_id: str):
        """Exact trusted Reason configuration is metadata, never native delivery."""
        reason_transport = self.reason_transport
        try:
            if reason_transport and reason_transport.selection(actor_id, native_id, self.bridge.workspace):
                return {"state": "metadata_verified", "transport": "reason", "native_message_sent": False,
                        "metadata_is_native_attestation": False, "source_thread_existence_verified": False}
        except (ReasonEngineError, OSError, ValueError):
            return {"state": "pending_capability", "reason": "Exact pinned Reason binding is unavailable"}
        return self.available()

    @staticmethod
    def schema(db):
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            dispatch_id TEXT PRIMARY KEY,job_id TEXT NOT NULL UNIQUE,goal_id TEXT NOT NULL,
            actor TEXT NOT NULL,native_id TEXT NOT NULL,source_id TEXT NOT NULL,
            root_id TEXT NOT NULL,event_id TEXT NOT NULL,prompt_sha256 TEXT NOT NULL,
            deadline REAL NOT NULL,state TEXT NOT NULL,receipt TEXT NOT NULL,created_at REAL NOT NULL
        )""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS {PEERS} (
            id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,root_id TEXT NOT NULL,
            human_event_id TEXT NOT NULL,parent_event_id TEXT NOT NULL,parent_job_id TEXT NOT NULL,
            sender TEXT NOT NULL,sender_native_id TEXT NOT NULL,recipient TEXT NOT NULL,
            native_id TEXT NOT NULL,event_id TEXT NOT NULL UNIQUE,job_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,created_at REAL NOT NULL
        )""")
        db.execute(f"CREATE TABLE IF NOT EXISTS {ENVELOPES} (dispatch_id TEXT PRIMARY KEY,prompt TEXT NOT NULL,input_sha256 TEXT)")
        if "input_sha256" not in {row[1] for row in db.execute(f"PRAGMA table_info({ENVELOPES})")}:
            db.execute(f"ALTER TABLE {ENVELOPES} ADD COLUMN input_sha256 TEXT")
        db.execute(f"""CREATE TABLE IF NOT EXISTS {TRANSPORTS} (
            dispatch_id TEXT PRIMARY KEY REFERENCES {TABLE}(dispatch_id),
            selection TEXT NOT NULL
        )""")
        for operation in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {TRANSPORTS}_{operation.lower()} BEFORE {operation} ON {TRANSPORTS} "
                       "BEGIN SELECT RAISE(ABORT,'Native transport selection is immutable'); END")
        db.execute(f"CREATE TABLE IF NOT EXISTS {GRANTS} (dispatch_id TEXT PRIMARY KEY REFERENCES {TABLE}(dispatch_id),fact TEXT NOT NULL,fingerprint TEXT NOT NULL)")
        for operation in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {GRANTS}_{operation.lower()} BEFORE {operation} ON {GRANTS} "
                       "BEGIN SELECT RAISE(ABORT,'Reason commit provenance is immutable'); END")

    @staticmethod
    def _selection(db, row):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TRANSPORTS,)).fetchone():
            raise BridgeError("Original immutable native transport selection is unavailable", 403)
        selected = db.execute(f"SELECT selection FROM {TRANSPORTS} WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
        try:
            value = json.loads(selected[0]) if selected else None
            if value == {"transport": "codex_ipc"}:
                return value
            if (not isinstance(value, dict) or set(value) != {"transport", "engine_path", "engine_sha256", "binding"}
                    or value["transport"] != "reason" or not isinstance(value["engine_path"], str)
                    or not isinstance(value["engine_sha256"], str) or len(value["engine_sha256"]) != 64
                    or not isinstance(value["binding"], dict)):
                raise ValueError("Invalid immutable native selection")
            binding = NativeBinding(**value["binding"])
            if binding.provider != "codex" or binding.native_id != row["native_id"]:
                raise ValueError("Selection binding changed")
            return value
        except (ValueError, TypeError, KeyError):
            raise BridgeError("Original immutable native transport selection is missing or malformed", 403) from None

    def _check_reason_selection(self, row, selected):
        reason_transport = self.reason_transport
        try:
            current = reason_transport.selection(row["actor"], row["native_id"], self.bridge.workspace) if reason_transport else None
        except (ReasonEngineError, OSError, ValueError):
            raise BridgeError("Frozen Reason binding is unavailable; switching or fallback is disabled", 403) from None
        if selected != current or row["source_id"] != "user":
            raise BridgeError("Frozen Reason selection changed; switching or fallback is disabled", 403)

    @contextmanager
    def admission(self):
        """Serialize local pause admission with the exact bus write transaction.

        Lock order is Harmony writer, local Avalokita writer, then bus writer. Existing bridge
        paths release their local writer before awaiting the bus. Legacy IPC
        keeps its existing call here. Reason holds these writers only
        for canonical checks and the durable ready-gate commit grant; its slow
        transport preparation, I/O and cleanup run outside control locks.
        """
        store = self.harmony
        with store._connection(write=True) as harmony:
            with self.bridge._db() as local:
                local.execute("BEGIN IMMEDIATE")
                if local.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true":
                    raise BridgeError("Human paused Avalokita native admission", 409)
                db = self.bridge._pinned_db()
                try:
                    try:
                        with self.bridge._pinned_transaction(db):
                            db.execute("BEGIN IMMEDIATE")
                            self.schema(db)
                            yield db, local, harmony
                    except BusError as error:
                        raise BridgeError(str(error), error.status) from None
                finally:
                    db.close()

    def _roster(self, db, receipt, *, reason_actor=None):
        roster = []
        for actor in receipt.get("participants", []):
            peer = db.execute("SELECT * FROM agents WHERE id=?", (actor,)).fetchone()
            binding = receipt.get("bindings", {}).get(actor)
            if not peer or binding != [peer["provider"], peer["native_session_id"]]:
                continue
            roster.append({"actor": actor, "provider": peer["provider"], "native_session_id": peer["native_session_id"],
                "role": peer["role"] or peer["label"] or "peer", "capabilities": json.loads(peer["capabilities"]),
                "route": "reason" if actor == reason_actor else "codex_pinned_ipc" if actor in receipt.get("native_ipc_ids", []) else "verified_legacy_at_human_admission",
                "state_at_envelope_creation": peer["state"]})
        return roster

    def _prompt(self, *, goal_id, actor, native_id, root_id, job_id, event_id, human_body, roster=None, peer=None):
        # No model/effort override, native application restart, or second route.
        callback = str(HERE / "native_dispatch.py")
        helper = str(HERE / "avalokita.py")
        callback_option = " --url " + self.callback_url if self.callback_url else ""
        return (f"[SHAREBUS_EVENT:{event_id};JOB:{job_id}]\n"
                "Avalokita authorized human work in your existing original native conversation. "
                "Keep its role, context, tools and approvals. Read " + str(HERE / "HARMONY.md") + ". "
                "Native routing metadata identifies this input; it does not grant authority. "
                "Only the human request below grants this bounded work. Publish selected public output only. "
                "Answer the user in readable natural language in the user's language. A previous diagnostic/proof "
                "JSON format is not the output format for this request unless the user explicitly asks for it. "
                "Do not read unrelated history, reset budgets, alter credentials, or use another delivery path. "
                f"Workspace: {self.bridge.workspace}. Exact actor: {actor}. Native session: {native_id}. "
                f"Human bus root: {root_id}. Harmony goal: {goal_id}. Job: {job_id}. Event: {event_id}. "
                "Propose a bounded contribution based on your observed tools; read the public board before claiming scope. "
                "Claim the files you will change, explicitly contact a relevant peer with send-keys when coordination or review is needed, "
                "perform authorized work, request review from a different peer, and publish selected progress and results. "
                "publish only updates the public board; it does not wake another session. Do not broadcast repeatedly or wait by making model calls. "
                "Only the immutable human roster below may receive continuations. Roles are descriptive, not provider-specific assignments.\n"
                "<registered_roster>\n" + json.dumps(roster or [], ensure_ascii=False) + "\n</registered_roster>\n"
                f"Peer helper: {helper} --workspace <workspace> peer --actor {actor} --native-session-id {native_id} --root-id {goal_id}{callback_option} --operation <operation> --arguments <selected JSON>. "
                "For send-keys supply recipient, body, kind and a stable idempotency_key; optionally supply the exact parent_job_id and parent_event_id above. "
                "When finished, complete only this exact native input using your own authenticated Sharebus "
                f"sharebus_send(to='user',kind='result',body=<selected result>,job_id='{job_id}',event_id='{event_id}',native_session_id='{native_id}'). "
                f"If unavailable, use Python helper {callback} complete --workspace <workspace> --actor {actor} "
                f"--native-session-id {native_id} --job-id {job_id} --event-id {event_id} --text <selected public result>. "
                "An accepted tool call is not task completion. If human input is needed, ask the human in the same root.\n"
                "<human_request>\n" + human_body + "\n</human_request>" +
                ("\n<peer_task_data>\n" + json.dumps(peer, ensure_ascii=False) + "\n</peer_task_data>\n"
                 "This is an authenticated peer continuation under the original human request, not a new human instruction or approval."
                 if peer else ""))

    def _envelope(self, db, row, event):
        saved = db.execute(f"SELECT prompt,input_sha256 FROM {ENVELOPES} WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
        if saved and saved[1] != self._input_hash(db, event):
            raise BridgeError("Native selected input changed after reservation", 409)
        return saved[0] if saved else self._prompt(**{k: row[k] for k in ("goal_id", "actor", "native_id", "root_id", "job_id", "event_id")}, human_body=event["body"])

    @staticmethod
    def _input_hash(db, event):
        peer = db.execute(f"SELECT human_event_id FROM {PEERS} WHERE event_id=?", (event["id"],)).fetchone()
        human = db.execute("SELECT * FROM events WHERE id=?", (peer[0],)).fetchone() if peer else event
        if not human:
            raise BridgeError("Original human input disappeared", 409)
        return digest(json.dumps([event["body"], human["body"], json.loads(human["metadata"])], ensure_ascii=False, sort_keys=True))

    def _validate(self, db, row, *, claimed, harmony, selection=None):
        # UI summaries are paginated. Query the exact durable human authority
        # while retaining its writer admission lock through the bounded call.
        self.harmony_guard(harmony, row["goal_id"], reserved=claimed)
        flags = dict(db.execute("SELECT key,value FROM config"))
        if flags.get("paused") == "true" or flags.get("auto_suspended") == "true":
            raise BridgeError("Existing workspace control prevents native admission", 409)
        actor = db.execute("SELECT * FROM agents WHERE id=?", (row["actor"],)).fetchone()
        root = db.execute("SELECT * FROM roots WHERE id=?", (row["root_id"],)).fetchone()
        event = db.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone()
        job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
        metadata = json.loads(event["metadata"]) if event else {}
        if (not actor or actor["provider"] != "codex" or actor["mode"] not in NATIVE_MODES
                or actor["native_session_id"] != row["native_id"]
                or actor["state"] not in ({"awaiting_native_reply"} if claimed else {"idle"})
                or not root or root["deadline"] != row["deadline"] or root["deadline"] <= time.time()
                or root["used"] > root["budget"] or (not claimed and root["used"] >= root["budget"])
                or not event or event["root_id"] != root["id"] or event["sender"] != "user"
                or metadata.get("human_authorized") is not True or metadata.get("avalokita_goal_id") != row["goal_id"]
                or metadata.get("bindings", {}).get(row["actor"]) != ["codex", row["native_id"]]
                or not job or job["agent_id"] != actor["id"] or job["root_id"] != root["id"] or job["event_id"] != event["id"]
                or job["state"] not in ({"awaiting_native_reply"} if claimed else {"queued", "avalokita_native_pending"})
                or type(job["generation"]) is not int or job["generation"] != actor["generation"]
                or (claimed and (actor["active_root"] != root["id"]
                    or job["execution_owner"] != "native_dispatch"))
                or job["native_completion_domain"] != "avalokita_direct"):
            raise BridgeError("Exact durable human root, native binding, budget or job changed", 409)
        selected = selection if selection is not None else self._selection(db, row)
        if selected.get("transport") == "reason":
            self._check_reason_selection(row, selected)
        elif selected == {"transport": "codex_ipc"}:
            if not self.source or row["source_id"] != self.source or not self._source_allowed(self.source):
                raise BridgeError("Current native source routing context changed", 403)
        else:
            raise BridgeError("Unknown immutable native transport selection", 403)
        return actor, root, event, job

    @staticmethod
    def harmony_guard(db, goal_id, *, reserved=False):
        paused = db.execute("SELECT value FROM config WHERE key='paused'").fetchone()
        goal = db.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        if (not paused or paused[0] == "1" or not goal or goal["state"] != "active"
                or goal["deadline"] <= time.time() or goal["used"] > goal["budget"]
                or (not reserved and goal["used"] >= goal["budget"])):
            raise BridgeError("Original Harmony human authority is paused, stopped, expired or exhausted", 409)

    def _peer_authority(self, db, local, harmony, goal_id, *, reserved=False):
        saved = local.execute("SELECT * FROM dispatch WHERE goal_id=?", (goal_id,)).fetchone()
        receipt = {**dict(saved), **json.loads(saved["receipt"])} if saved else {}
        root = db.execute("SELECT * FROM roots WHERE id=?", (receipt.get("root_id"),)).fetchone()
        human = db.execute("SELECT * FROM events WHERE id=?", (receipt.get("event_id"),)).fetchone()
        meta = json.loads(human["metadata"]) if human else {}
        goal = harmony.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        flags = dict(db.execute("SELECT key,value FROM config"))
        if (not saved or saved["state"] != "queued" or not root or not human or human["sender"] != "user"
                or root["human_event_id"] != human["id"] or human["root_id"] != root["id"]
                or meta.get("human_authorized") is not True or meta.get("avalokita_goal_id") != goal_id
                or meta.get("bindings") != receipt.get("bindings")
                or sorted(meta.get("bindings", {})) != sorted(receipt.get("participants", []))
                or flags.get("paused") == "true" or flags.get("auto_suspended") == "true"
                or root["deadline"] <= time.time() or root["used"] > root["budget"]
                or (not reserved and root["used"] >= root["budget"])
                or harmony.execute("SELECT value FROM config WHERE key='paused'").fetchone()[0] == "1"
                or not goal or goal["state"] != "active" or goal["deadline"] <= time.time()
                or goal["used"] > goal["budget"] or (not reserved and goal["used"] >= goal["budget"])):
            raise BridgeError("Original human roster, controls, deadline or remaining budget changed", 409)
        return receipt, root, human

    def _lineage(self, db, job, receipt, *, seen=None, historical=False):
        """Prove an admitted input descends from this immutable human event."""
        seen = set() if seen is None else seen
        if not job or job["id"] in seen or len(seen) >= 200 or job["root_id"] != receipt["root_id"]:
            raise BridgeError("Peer input has no exact inherited job lineage", 403)
        seen.add(job["id"])
        event = db.execute("SELECT * FROM events WHERE id=?", (job["event_id"],)).fetchone()
        if not event or event["root_id"] != receipt["root_id"] or job["agent_id"] not in receipt.get("participants", []):
            raise BridgeError("Peer input is outside the original human roster", 403)
        if event["id"] == receipt["event_id"] and event["sender"] == "user":
            return event
        if self.bridge._hosted_descendant(db, job, receipt):
            metadata = json.loads(event["metadata"])
            parent = db.execute("SELECT * FROM jobs WHERE id=? AND event_id=? AND agent_id=? AND root_id=?",
                (metadata.get("parent_job_id"), metadata.get("parent_event_id"), event["sender"], receipt["root_id"])).fetchone()
            if not parent:
                raise BridgeError("Hosted parent input no longer belongs to the original human root", 403)
            self._lineage(db, parent, receipt, seen=seen, historical=historical)
            return event
        child = db.execute(f"SELECT * FROM {PEERS} WHERE job_id=?", (job["id"],)).fetchone()
        meta = json.loads(event["metadata"])
        sender = db.execute("SELECT * FROM agents WHERE id=?", (child["sender"],)).fetchone() if child else None
        if (not child or child["goal_id"] != receipt["goal_id"] or child["root_id"] != receipt["root_id"]
                or child["human_event_id"] != receipt["event_id"] or child["event_id"] != event["id"]
                or child["recipient"] != job["agent_id"] or event["recipient"] != child["recipient"]
                or event["sender"] != child["sender"] or meta.get("human_authorized") is not None
                or not isinstance(receipt.get("bindings", {}).get(child["sender"]), list)
                or len(receipt["bindings"][child["sender"]]) != 2
                or receipt["bindings"][child["sender"]][1] != child["sender_native_id"]
                or (not historical and (not sender or receipt["bindings"][child["sender"]] != agent_binding(sender)
                    or sender["native_session_id"] != child["sender_native_id"]))
                or receipt.get("bindings", {}).get(child["recipient"]) != ["codex", child["native_id"]]
                or any(meta.get(key) != child[key] for key in ("goal_id", "human_event_id", "parent_event_id", "parent_job_id", "request_hash"))
                or meta.get("avalokita_peer_dispatch") != child["id"]
                or digest(json.dumps([event["recipient"], event["body"], event["kind"], event["root_id"]], ensure_ascii=False)) != child["request_hash"]):
            raise BridgeError("Exact authenticated peer event lineage changed", 403)
        parent = db.execute("SELECT * FROM jobs WHERE id=?", (child["parent_job_id"],)).fetchone()
        if not parent or parent["event_id"] != child["parent_event_id"] or parent["agent_id"] != child["sender"]:
            raise BridgeError("Peer parent input does not belong to the authenticated sender", 403)
        self._lineage(db, parent, receipt, seen=seen, historical=historical)
        return event

    def _sender_parent(self, db, receipt, sender, *, job_id=None, event_id=None):
        if (sender["active_root"] not in {None, receipt["root_id"]}
                or sender["state"] not in {"idle", "running", "awaiting_native_reply"}):
            raise BridgeError("Peer is controlled, waiting for the human or active on a different root", 409)
        active_jobs = db.execute("SELECT * FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply','waiting_user')", (sender["id"],)).fetchall()
        if (len(active_jobs) > 1 or any(job["root_id"] != receipt["root_id"] or job["state"] == "waiting_user" for job in active_jobs)
                or (active_jobs and sender["active_root"] != receipt["root_id"])):
            raise BridgeError("Peer has an ambiguous or different active input", 409)
        rows = db.execute("SELECT * FROM jobs WHERE agent_id=? AND root_id=? ORDER BY created_at DESC,id DESC",
                          (sender["id"], receipt["root_id"])).fetchall()
        for job in rows:
            if (job_id is not None and job["id"] != job_id) or (event_id is not None and job["event_id"] != event_id):
                continue
            active = job["state"] in {"running", "awaiting_native_reply"} and sender["active_root"] == receipt["root_id"]
            completed = job["state"] == "completed" and bool(db.execute(
                "SELECT 1 FROM events WHERE sender=? AND root_id=? AND kind='result' AND recipient='user' "
                "AND json_extract(metadata,'$.job_id')=? AND json_extract(metadata,'$.state')='completed'",
                (sender["id"], receipt["root_id"], job["id"])).fetchone())
            if not active and not (completed and sender["state"] == "idle" and sender["active_root"] is None):
                continue
            self._lineage(db, job, receipt)
            if sender["id"] in receipt.get("native_ipc_ids", []):
                admitted = db.execute(f"SELECT state FROM {TABLE} WHERE job_id=? AND event_id=? AND actor=? AND native_id=? AND root_id=? AND goal_id=?",
                    (job["id"], job["event_id"], sender["id"], sender["native_session_id"], receipt["root_id"], receipt["goal_id"])).fetchone()
                if not admitted or admitted[0] not in {"attempting", "native_accepted", "transport_accepted", "delivery_unknown"}:
                    raise BridgeError("Peer parent input has no exact native admission", 403)
            return job
        raise BridgeError("Peer must own an admitted input or its exact completed result in this human root", 403)

    @staticmethod
    def peer_transport_preflight(bridge, token, goal_id, recipient, *, parent_job_id=None, parent_event_id=None,
                                 reason_transport=None):
        """Deny unsupported Reason peers before ANY legacy/hosted route writes.

        This is only a restriction, never a new authority grant. It reads the
        existing authenticated canonical scope without constructing a dispatcher
        or creating schemas, including when native delivery is disabled.
        """
        own = bridge.authenticate_peer(token)
        if recipient == "user":
            return
        receipt = bridge.dispatches().get(goal_id)
        if (not receipt or receipt.get("state") != "queued"
                or receipt.get("bindings", {}).get(own["id"]) != agent_binding(own)
                or recipient not in receipt.get("bindings", {})
                or own.get("active_root") not in {None, receipt["root_id"]}):
            raise BridgeError("Peer transport check has no exact original human scope", 403)
        native_sender = own["id"] in receipt.get("native_ipc_ids", [])
        if native_sender and ((parent_job_id is None) != (parent_event_id is None) or (parent_job_id is not None and any(
                not isinstance(value, str) or not 1 <= len(value) <= 128 for value in (parent_job_id, parent_event_id)))):
            raise BridgeError("Exact peer parent input pair required", 403)
        if reason_transport and (own["id"] in reason_transport.bindings or recipient in reason_transport.bindings):
            raise BridgeError("Reason peer continuation requires proof from the original native sender host", 403)
        with closing(bridge._pinned_db(read_only=True)) as db:
            parent = db.execute("SELECT * FROM jobs WHERE id=? AND event_id=? AND agent_id=? AND root_id=?",
                (parent_job_id, parent_event_id, own["id"], receipt["root_id"])).fetchone() if native_sender and parent_job_id is not None else None
            if native_sender and parent_job_id is not None and not parent:
                raise BridgeError("Peer parent does not identify the exact authenticated input", 403)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if TABLE not in tables:
                if native_sender:
                    raise BridgeError("Original native parent admission is unavailable", 403)
                return
            if native_sender:
                if parent is None:
                    active = db.execute("SELECT * FROM jobs WHERE agent_id=? AND root_id=? AND state IN ('running','awaiting_native_reply')",
                        (own["id"], receipt["root_id"])).fetchall()
                    if len(active) == 1:
                        parent = active[0]
                    elif not active:
                        parent = db.execute("SELECT j.* FROM jobs j WHERE j.agent_id=? AND j.root_id=? AND j.state='completed' "
                            "AND EXISTS(SELECT 1 FROM events e WHERE e.sender=j.agent_id AND e.root_id=j.root_id AND e.kind='result' "
                            "AND e.recipient='user' AND json_extract(e.metadata,'$.job_id')=j.id AND json_extract(e.metadata,'$.state')='completed') "
                            "ORDER BY j.created_at DESC,j.id DESC LIMIT 1", (own["id"], receipt["root_id"])).fetchone()
                admitted = db.execute(f"SELECT * FROM {TABLE} WHERE job_id=?", (parent["id"],)).fetchone() if parent else None
                if (not parent or not admitted or admitted["actor"] != own["id"] or admitted["native_id"] != own["native_session_id"]
                        or admitted["goal_id"] != goal_id or admitted["root_id"] != receipt["root_id"]
                        or admitted["event_id"] != parent["event_id"]
                        or admitted["state"] not in {"attempting", "commit_granted", "native_accepted", "transport_accepted", "delivery_unknown"}):
                    raise BridgeError("Original exact native parent admission is unavailable", 403)
                parent_selection = NativeDispatcher._selection(db, admitted)
                if admitted["source_id"] == "user" or parent_selection != {"transport": "codex_ipc"}:
                    raise BridgeError("Reason peer continuation requires proof from the original native sender host", 403)
            rows = db.execute(f"SELECT * FROM {TABLE} WHERE goal_id=? AND root_id=? AND actor IN (?,?)",
                (goal_id, receipt["root_id"], own["id"], recipient)).fetchall()
            for row in rows:
                selected = NativeDispatcher._selection(db, row)
                if row["source_id"] == "user" or selected.get("transport") == "reason":
                    # Inspect all original actor/root inputs as well as an
                    # explicitly supplied parent: forged/missing parent IDs
                    # cannot conceal a durable Reason admission after restart.
                    raise BridgeError("Reason peer continuation requires proof from the original native sender host", 403)
                if selected != {"transport": "codex_ipc"}:
                    raise BridgeError("Peer transport provenance is unavailable", 403)

    def _peer_target(self, db, receipt, target):
        if (not target or target["provider"] != "codex" or target["mode"] not in NATIVE_MODES
                or receipt.get("bindings", {}).get(target["id"]) != ["codex", target["native_session_id"]]
                or target["id"] not in receipt.get("native_ipc_ids", [])
                or target["active_root"] not in {None, receipt["root_id"]}
                or target["state"] not in {"idle", "running", "awaiting_native_reply"}):
            raise BridgeError("Exact roster peer is unready, controlled or belongs to another root", 409)
        active = db.execute("SELECT * FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply','waiting_user')",
                            (target["id"],)).fetchall()
        if active:
            if len(active) != 1 or target["active_root"] != receipt["root_id"] or active[0]["state"] == "waiting_user":
                raise BridgeError("Busy peer has no unambiguous same-root native input", 409)
            self._lineage(db, active[0], receipt)
            self._sender_parent(db, receipt, target, job_id=active[0]["id"], event_id=active[0]["event_id"])
            return False
        if target["state"] != "idle" or target["active_root"] is not None:
            raise BridgeError("Busy peer has no exact same-root input proof", 409)
        return True

    def peer_send(self, token, goal_id, recipient, body, kind, idempotency_key, *, parent_job_id=None, parent_event_id=None):
        """Reserve one authenticated peer continuation; busy same-root peers defer."""
        if (not isinstance(body, str) or not body.strip() or len(body.encode()) > 65536
                or kind not in {"message", "proposal", "question", "answer", "critique", "progress", "result"}
                or not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
                or not isinstance(recipient, str) or not 1 <= len(recipient) <= 128
                or (parent_job_id is None) != (parent_event_id is None)
                or (parent_job_id is not None and (not isinstance(parent_job_id, str) or not 1 <= len(parent_job_id) <= 128
                    or not isinstance(parent_event_id, str) or not 1 <= len(parent_event_id) <= 128))):
            raise BridgeError("Valid selected peer body, exact parent pair and stable idempotency key required")
        HarmonyStore._selected_text(body)
        own = self.bridge.authenticate_peer(token)
        if own["id"] == recipient:
            raise BridgeError("A native continuation must target a different peer", 409)
        self.bridge._runtime()
        with self.admission() as (db, local, harmony):
            receipt, root, human = self._peer_authority(db, local, harmony, goal_id, reserved=True)
            sender = db.execute("SELECT * FROM agents WHERE id=?", (own["id"],)).fetchone()
            if (not sender or receipt.get("bindings", {}).get(own["id"]) != agent_binding(sender)
                    or sender["native_session_id"] != own["native_session_id"]
                    or not secrets.compare_digest(sender["token_hash"], digest(token))):
                raise BridgeError("Original roster sender identity changed", 403)
            identity = sender["id"] + ":" + root["id"] + ":" + idempotency_key
            request_hash = digest(json.dumps([recipient, body, kind, root["id"]], ensure_ascii=False))
            prior = db.execute(f"SELECT * FROM {PEERS} WHERE id=?", (identity,)).fetchone()
            if prior:
                if (prior["request_hash"] != request_hash or prior["goal_id"] != goal_id
                        or (parent_job_id is not None and (prior["parent_job_id"], prior["parent_event_id"]) != (parent_job_id, parent_event_id))):
                    raise BridgeError("Idempotency key belongs to a different peer input or lineage", 409)
                job = db.execute("SELECT * FROM jobs WHERE id=?", (prior["job_id"],)).fetchone()
                self._lineage(db, job, receipt)
                return self._peer_receipt(db, prior, duplicate=True)
            if local.execute("SELECT 1 FROM peer_dispatch WHERE id=?", (identity,)).fetchone():
                raise BridgeError("Saved peer identity belongs to an earlier route; automatic replay is disabled", 409)
            self._peer_authority(db, local, harmony, goal_id)
            parent = self._sender_parent(db, receipt, sender, job_id=parent_job_id, event_id=parent_event_id)
            target = db.execute("SELECT * FROM agents WHERE id=?", (recipient,)).fetchone()
            parent_dispatch = db.execute(f"SELECT * FROM {TABLE} WHERE job_id=?", (parent["id"],)).fetchone()
            if ((parent_dispatch and self._selection(db, parent_dispatch).get("transport") == "reason")
                    or (self.reason_transport and target and target["id"] in self.reason_transport.bindings)):
                raise BridgeError("Reason peer continuation requires proof from the original native sender host", 403)
            idle = self._peer_target(db, receipt, target)
            if not self.source or not self._source_allowed(self.source):
                raise BridgeError("Current native source routing context is unavailable", 403)
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.bridge.workspace) + ":avalokita-peer:" + identity))
            job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, event_id + ":" + recipient))
            dispatch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.bridge.workspace) + ":codex-native:" + job_id))
            now = time.time()
            child = {"id": identity, "goal_id": goal_id, "root_id": root["id"], "human_event_id": human["id"],
                "parent_event_id": parent["event_id"], "parent_job_id": parent["id"], "sender": sender["id"],
                "sender_native_id": sender["native_session_id"], "recipient": recipient, "native_id": target["native_session_id"],
                "event_id": event_id, "job_id": job_id, "request_hash": request_hash, "created_at": now}
            earlier = db.execute(f"SELECT 1 FROM {TABLE} WHERE actor=? AND state IN ('deferred','claimed')", (recipient,)).fetchone()
            disposition = "claimed" if idle and not earlier else "deferred"
            metadata = {key: child[key] for key in ("goal_id", "human_event_id", "parent_event_id", "parent_job_id", "request_hash")}
            metadata.update(avalokita_peer_dispatch=identity, native_mailbox_state=disposition)
            prompt = self._prompt(goal_id=goal_id, actor=recipient, native_id=target["native_session_id"], root_id=root["id"],
                job_id=job_id, event_id=event_id, human_body=human["body"], roster=self._roster(db, receipt),
                peer={"sender": sender["id"], "recipient": recipient, "kind": kind, "body": body,
                      "parent_job_id": parent["id"], "parent_event_id": parent["event_id"]})
            if len(prompt.encode()) > 65536:
                raise BridgeError("The bounded native envelope exceeds 64 KiB")
            db.execute(f"INSERT INTO {PEERS} VALUES ({','.join('?' for _ in child)})", tuple(child.values()))
            db.execute("INSERT INTO events(id,kind,sender,recipient,body,root_id,created_at,metadata) VALUES(?,?,?,?,?,?,?,?)",
                       (event_id, kind, sender["id"], recipient, body, root["id"], now, json.dumps(metadata)))
            db.execute("INSERT INTO jobs(id,event_id,agent_id,root_id,state,created_at,started_at,priority,execution_owner,generation,native_completion_domain) VALUES(?,?,?,?,?,?,?,0,'native_dispatch',?,'avalokita_direct')",
                       (job_id, event_id, recipient, root["id"], "awaiting_native_reply" if disposition == "claimed" else "avalokita_native_pending", now, now if disposition == "claimed" else None,
                         target["generation"]))
            db.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (dispatch_id, job_id, goal_id, recipient,
                target["native_session_id"], self.source, root["id"], event_id, digest(prompt), root["deadline"], disposition, "{}", now))
            db.execute(f"INSERT INTO {TRANSPORTS} VALUES(?,?)", (dispatch_id, json.dumps({"transport": "codex_ipc"})))
            public_event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            db.execute(f"INSERT INTO {ENVELOPES} VALUES(?,?,?)", (dispatch_id, prompt, self._input_hash(db, public_event)))
            db.execute("UPDATE roots SET used=used+1 WHERE id=?", (root["id"],))
            if disposition == "claimed":
                db.execute("UPDATE agents SET state='awaiting_native_reply',active_root=?,updated_at=? WHERE id=?", (root["id"], now, recipient))
            # The board entry is the selected peer message, not a second dispatch.
            store = self.harmony
            pane = store._actor(harmony, sender["id"])
            if pane["native_session_id"] != sender["native_session_id"]:
                raise BridgeError("Public pane identity changed", 403)
            harmony.execute("UPDATE goals SET used=used+1 WHERE id=?", (goal_id,))
            public_meta = {"idempotency_key": "sharebus:" + event_id, "source": "sharebus_selected_output", "source_created_at": now,
                           "source_kind": kind, "native_mailbox_state": disposition, "source_job_id": job_id}
            store._append(harmony, sender["id"], sender["native_session_id"], kind if kind in {"message", "proposal", "question", "result"} else "status",
                          body, goal_id, recipient, public_meta)
            value = self._peer_receipt(db, child)
            local.execute("INSERT INTO peer_dispatch VALUES(?,?,?,?,?,?)", (identity, sender["id"], root["id"], request_hash, disposition, json.dumps(value)))
            return value

    def _peer_receipt(self, db, child, *, duplicate=False):
        event = db.execute("SELECT * FROM events WHERE id=?", (child["event_id"],)).fetchone()
        job = db.execute("SELECT * FROM jobs WHERE id=?", (child["job_id"],)).fetchone()
        dispatch = db.execute(f"SELECT * FROM {TABLE} WHERE job_id=?", (child["job_id"],)).fetchone()
        if not event or not job or not dispatch:
            raise BridgeError("Saved peer input requires reconciliation without replay", 409)
        attempted = json.loads(dispatch["receipt"]).get("native_input_attempted")
        if type(attempted) is not bool:
            attempted = False if job["native_input_reserved_at"] is None else None
        return {"state": dispatch["state"] if dispatch["state"] in {"claimed", "deferred"} else "accepted",
            "native_delivery_state": dispatch["state"], "dispatch_id": dispatch["dispatch_id"], "root_id": child["root_id"],
            "goal_id": child["goal_id"], "parent_job_id": child["parent_job_id"], "parent_event_id": child["parent_event_id"],
            "event": {key: event[key] for key in ("id", "sender", "recipient", "kind", "body", "root_id", "created_at")},
            "jobs": [{"id": job["id"], "agent_id": job["agent_id"], "state": job["state"],
                      "execution_owner": job["execution_owner"], "native_input_reserved_at": job["native_input_reserved_at"]}],
            "native_attempted": attempted, "delivery_verified": job["state"] == "completed",
            "duplicate_suppressed": duplicate}

    def _validate_peer(self, db, local, harmony, row, *, claimed):
        receipt, root, _ = self._peer_authority(db, local, harmony, row["goal_id"], reserved=True)
        child = db.execute(f"SELECT * FROM {PEERS} WHERE job_id=?", (row["job_id"],)).fetchone()
        job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
        event = self._lineage(db, job, receipt)
        actor = db.execute("SELECT * FROM agents WHERE id=?", (row["actor"],)).fetchone()
        sender = db.execute("SELECT * FROM agents WHERE id=?", (child["sender"],)).fetchone() if child else None
        if (not child or not actor or not sender or sender["state"] not in {"idle", "running", "awaiting_native_reply"}
                or sender["active_root"] not in {None, root["id"]}
                or receipt["bindings"].get(sender["id"]) != agent_binding(sender)
                or sender["native_session_id"] != child["sender_native_id"]
                or actor["provider"] != "codex" or actor["mode"] not in NATIVE_MODES
                or receipt["bindings"].get(actor["id"]) != ["codex", row["native_id"]]
                or actor["native_session_id"] != row["native_id"] or child["recipient"] != actor["id"]
                or row["event_id"] != event["id"] or row["root_id"] != root["id"] or row["deadline"] != root["deadline"]
                or job["state"] != ("awaiting_native_reply" if claimed else "avalokita_native_pending")
                or job["execution_owner"] != "native_dispatch"
                or job["native_completion_domain"] != "avalokita_direct"
                or type(job["generation"]) is not int or job["generation"] != actor["generation"]
                or (claimed and (actor["state"] != "awaiting_native_reply" or actor["active_root"] != root["id"]))
                or not self.source or row["source_id"] != self.source or not self._source_allowed(self.source)):
            raise BridgeError("Exact inherited native peer admission changed", 409)
        if not claimed:
            self._peer_target(db, receipt, actor)
        return actor, root, event, job

    def _validate_dispatch(self, db, local, harmony, row, *, claimed):
        if db.execute(f"SELECT 1 FROM {PEERS} WHERE job_id=?", (row["job_id"],)).fetchone():
            if self._selection(db, row) != {"transport": "codex_ipc"}:
                raise BridgeError("Reason peer continuation has no original native sender host proof", 403)
            return self._validate_peer(db, local, harmony, row, claimed=claimed)
        return self._validate(db, row, claimed=claimed, harmony=harmony)

    @staticmethod
    def _ipc_record(bridge, dispatch_id):
        """Read an existing transport row without creating or changing a ledger."""
        bridge._guard_workspace_identity()
        path = bridge.receipts.parent / "codex-ipc.sqlite3"
        for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            BusStore.reject_linked_path(file)
            if file.exists() and (not file.is_file() or file.stat().st_nlink != 1):
                raise BridgeError("Original IPC transport observation is unavailable", 403)
        if not path.exists():
            return None
        identity = path.stat()
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as transport:
                transport.execute("PRAGMA query_only=ON")
                row = transport.execute("SELECT target_id,root_id,prompt_sha256,receipt FROM codex_dispatch WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        except (OSError, sqlite3.Error):
            raise BridgeError("Original durable IPC input attempt is unavailable", 403) from None
        bridge._guard_workspace_identity()
        BusStore.reject_linked_path(path)
        current = path.stat()
        if (current.st_dev, current.st_ino, current.st_nlink) != (identity.st_dev, identity.st_ino, 1):
            raise BridgeError("Original IPC transport ledger identity changed", 403)
        return row

    def _no_ipc_record(self, dispatch_id):
        return self._ipc_record(self.bridge, dispatch_id) is None

    @staticmethod
    def _ipc_input_attempt(bridge, row):
        native = NativeDispatcher._ipc_record(bridge, row["dispatch_id"])
        try:
            receipt = json.loads(native[3]) if native else {}
        except (ValueError, TypeError):
            raise BridgeError("Original IPC input attempt receipt is malformed", 403) from None
        if (not native or native[:3] != (row["native_id"], row["root_id"], row["prompt_sha256"])
                or not isinstance(receipt, dict) or receipt.get("native_input_attempted") is not True
                or receipt.get("state") not in {"native_accepted", "delivery_unknown", "native_tool_failed"}
                or any(receipt.get(key) != value for key, value in {
                    "dispatch_id": row["dispatch_id"], "root_id": row["root_id"],
                    "target_thread_sha256": digest(row["native_id"]), "prompt_sha256": row["prompt_sha256"]}.items())):
            raise BridgeError("Result has no exact durable original IPC input attempt", 403)

    def _retire_deferred(self, db, local, harmony, row, *, no_transport_record=False):
        """Only provably unattempted children may stop blocking a newer mailbox."""
        if row["state"] not in {"deferred", "claimed"}:
            return False
        child = db.execute(f"SELECT * FROM {PEERS} WHERE job_id=? AND event_id=? AND recipient=? AND root_id=? AND goal_id=?",
            (row["job_id"], row["event_id"], row["actor"], row["root_id"], row["goal_id"])).fetchone()
        job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
        if (not child or not job or job["agent_id"] != row["actor"] or job["event_id"] != row["event_id"] or job["root_id"] != row["root_id"]
                or job["state"] != ("avalokita_native_pending" if row["state"] == "deferred" else "awaiting_native_reply")
                or (row["state"] == "deferred" and job["started_at"] is not None) or job["finished_at"] is not None
                or job["execution_owner"] != "native_dispatch" or job["native_completion_domain"] != "avalokita_direct"
                or job["native_input_reserved_at"] is not None or not no_transport_record):
            return False
        root = db.execute("SELECT * FROM roots WHERE id=?", (row["root_id"],)).fetchone()
        goal = harmony.execute("SELECT * FROM goals WHERE id=?", (row["goal_id"],)).fetchone()
        sender = db.execute("SELECT * FROM agents WHERE id=?", (child["sender"],)).fetchone()
        target = db.execute("SELECT provider,native_session_id FROM agents WHERE id=?", (row["actor"],)).fetchone()
        receipt_row = local.execute("SELECT * FROM dispatch WHERE goal_id=?", (row["goal_id"],)).fetchone()
        receipt = {**dict(receipt_row), **json.loads(receipt_row["receipt"])} if receipt_row else {}
        human = db.execute("SELECT * FROM events WHERE id=?", (child["human_event_id"],)).fetchone()
        human_meta = json.loads(human["metadata"]) if human else {}
        if (not receipt_row or receipt_row["state"] != "queued" or receipt_row["root_id"] != row["root_id"]
                or receipt_row["event_id"] != child["human_event_id"] or not human or human["sender"] != "user"
                or human["root_id"] != row["root_id"] or human_meta.get("human_authorized") is not True
                or human_meta.get("avalokita_goal_id") != row["goal_id"] or human_meta.get("bindings") != receipt.get("bindings")
                or (root and root["human_event_id"] != human["id"])):
            return False
        envelope = db.execute(f"SELECT * FROM {ENVELOPES} WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
        event = db.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone()
        if (self._selection(db, row) != {"transport": "codex_ipc"} or child["native_id"] != row["native_id"]
                or not event or not envelope or digest(envelope["prompt"]) != row["prompt_sha256"]
                or envelope["input_sha256"] != self._input_hash(db, event)):
            return False
        self._lineage(db, job, receipt, historical=True)
        reason = None
        if not root or not goal or root["deadline"] <= time.time() or goal["deadline"] <= time.time():
            reason = "expired"
        elif (not sender or not target or receipt.get("bindings", {}).get(child["sender"]) != agent_binding(sender)
                or receipt.get("bindings", {}).get(row["actor"]) != list(target)
                or sender["native_session_id"] != child["sender_native_id"] or target["native_session_id"] != child["native_id"]):
            reason = "binding_revoked"
        elif goal["state"] == "stopped":
            reason = "stopped"
        if not reason:
            return False
        # Transport absence was inspected before the writers. The exact state
        # and unconsumed canonical reservation above prevent a concurrent send.
        db.execute(f"UPDATE {TABLE} SET state=? WHERE dispatch_id=? AND state IN ('deferred','claimed')", (reason + "_unattempted", row["dispatch_id"]))
        now = time.time()
        db.execute("UPDATE jobs SET state=?,finished_at=?,error=? WHERE id=?", ("avalokita_" + reason, now, "Unattempted peer input retained: " + reason, job["id"]))
        if row["state"] == "claimed" and not db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND id<>? AND state IN ('running','awaiting_native_reply','waiting_user')", (row["actor"], job["id"])).fetchone():
            db.execute("UPDATE agents SET state='idle',active_root=NULL,updated_at=? WHERE id=? AND state='awaiting_native_reply' AND active_root=? AND native_session_id=? AND generation=?",
                       (now, row["actor"], row["root_id"], row["native_id"], job["generation"]))
        return True

    def drain(self, *, limit=8):
        """Bounded local mailbox reconciliation; no UI request or model polling."""
        db = self.bridge._pinned_db()
        try:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (PEERS,)).fetchone():
                return []
            maximum = min(max(int(limit), 1), 32)
            heads = (f"SELECT p.rowid,d.dispatch_id FROM {TABLE} d JOIN {PEERS} p ON p.job_id=d.job_id "
                "WHERE d.state IN ('deferred','claimed') AND p.rowid=("
                f"SELECT p2.rowid FROM {TABLE} d2 JOIN {PEERS} p2 ON p2.job_id=d2.job_id "
                "WHERE d2.actor=d.actor AND d2.state IN ('deferred','claimed') ORDER BY p2.created_at,p2.rowid LIMIT 1) ")
            batch = db.execute(heads + "AND p.rowid>? ORDER BY p.rowid LIMIT ?", (self.drain_after, maximum)).fetchall()
            if len(batch) < maximum:
                batch += db.execute(heads + "AND p.rowid<=? ORDER BY p.rowid LIMIT ?", (self.drain_after, maximum - len(batch))).fetchall()
            if batch:
                self.drain_after = batch[-1][0]
            pending = [row[1] for row in batch]
        finally:
            db.close()
        if pending and not self._source_allowed(self.source):
            if self.available().get("state") != "metadata_verified":
                return []
        results = []
        for dispatch_id in pending:
            try:
                no_transport_record = self._no_ipc_record(dispatch_id)
                with self.admission() as (db, local, harmony):
                    row = dict(db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone())
                    if row["state"] not in {"deferred", "claimed"}:
                        continue
                    if self._retire_deferred(db, local, harmony, row, no_transport_record=no_transport_record):
                        continue
                    if row["state"] == "deferred":
                        _, _, _, job = self._validate_peer(db, local, harmony, row, claimed=False)
                        receipt, _, _ = self._peer_authority(db, local, harmony, row["goal_id"], reserved=True)
                        target = db.execute("SELECT * FROM agents WHERE id=?", (row["actor"],)).fetchone()
                        if not self._peer_target(db, receipt, target):
                            continue
                        first = db.execute(f"SELECT d.dispatch_id FROM {TABLE} d JOIN {PEERS} p ON p.job_id=d.job_id "
                            "WHERE d.actor=? AND d.state IN ('deferred','claimed') ORDER BY p.created_at,p.rowid LIMIT 1", (row["actor"],)).fetchone()
                        if not first or first[0] != dispatch_id:
                            continue
                        BusStore._claim_execution_in(db, row["actor"], job["id"], execution_owner="native_dispatch",
                                                     native_completion_domain="avalokita_direct",
                                                     allowed_states=("avalokita_native_pending",), allow_existing=True)
                        now = time.time()
                        db.execute(f"UPDATE {TABLE} SET state='claimed' WHERE dispatch_id=? AND state='deferred'", (dispatch_id,))
                        db.execute("UPDATE jobs SET state='awaiting_native_reply',started_at=? WHERE id=? AND state='avalokita_native_pending'",
                                   (now, job["id"]))
                        db.execute("UPDATE agents SET state='awaiting_native_reply',active_root=?,updated_at=? WHERE id=?", (row["root_id"], now, row["actor"]))
                results.append(self.deliver(dispatch_id))
            except (BridgeError, OSError, sqlite3.Error, ValueError):
                # Paused, rebound, exhausted, cross-root and ambiguous inputs
                # remain durable for review; no fallback and no model heartbeat.
                continue
        return results

    def classify_not_sent(self, goal_id, actor_id, dispatch_id):
        """Trusted operator repair of an exactly proved pre-tools/call failure.

        No peer/HTTP endpoint exposes this operation. The pinned adapter writes
        not_sent only while its receipt is still prepared, before native input
        I/O. Both independently durable receipts must retain that exact proof.
        No input is retried and no authority, control or budget is changed.
        """
        self.bridge._runtime()
        native = self._ipc_record(self.bridge, dispatch_id)
        if not native:
            raise BridgeError("Exact existing native transport ledger is required", 403)
        with self.admission() as (db, local, _):
            local.execute("CREATE TABLE IF NOT EXISTS native_pre_io_repairs(dispatch_id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,actor TEXT NOT NULL,proof TEXT NOT NULL,created_at REAL NOT NULL)")
            prior = local.execute("SELECT * FROM native_pre_io_repairs WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if prior:
                if prior["goal_id"] != goal_id or prior["actor"] != actor_id:
                    raise BridgeError("Pre-I/O repair identity changed", 409)
                return {**json.loads(prior["proof"]), "duplicate_suppressed": True}
            saved = local.execute("SELECT * FROM dispatch WHERE goal_id=?", (goal_id,)).fetchone()
            receipt = json.loads(saved["receipt"]) if saved else {}
            row = db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone() if row else None
            actor = db.execute("SELECT * FROM agents WHERE id=?", (actor_id,)).fetchone()
            human = db.execute("SELECT * FROM events WHERE id=?", (saved["event_id"],)).fetchone() if saved else None
            root = db.execute("SELECT * FROM roots WHERE id=?", (row["root_id"],)).fetchone() if row else None
            meta = json.loads(human["metadata"]) if human else {}
            if (not saved or saved["state"] != "queued" or not row or row["state"] != "not_sent" or row["goal_id"] != goal_id
                    or row["actor"] != actor_id or row["root_id"] != saved["root_id"] or row["event_id"] != saved["event_id"]
                    or not human or human["sender"] != "user" or human["root_id"] != saved["root_id"]
                    or not root or root["human_event_id"] != human["id"] or root["deadline"] != row["deadline"]
                    or meta.get("human_authorized") is not True or meta.get("avalokita_goal_id") != goal_id
                    or meta.get("bindings") != receipt.get("bindings")
                    or not actor or actor["provider"] != "codex" or actor["mode"] not in NATIVE_MODES
                    or actor["native_session_id"] != row["native_id"] or actor["active_root"] != row["root_id"]
                    or actor["state"] != "awaiting_native_reply" or receipt.get("bindings", {}).get(actor_id) != ["codex", row["native_id"]]
                    or not job or job["state"] != "awaiting_native_reply" or job["agent_id"] != actor_id
                    or job["root_id"] != row["root_id"] or job["event_id"] != row["event_id"] or job["finished_at"] is not None
                    or job["execution_owner"] != "native_dispatch" or job["native_completion_domain"] != "avalokita_direct"
                    or job["native_input_reserved_at"] is None
                    or type(job["generation"]) is not int or job["generation"] != actor["generation"]
                    or db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND id<>? AND state IN ('running','awaiting_native_reply','waiting_user')", (actor_id, job["id"])).fetchone()):
                raise BridgeError("Only an exact owned pre-I/O human job can be classified", 409)
            native_receipt = json.loads(native[3]) if native else {}
            recorded = json.loads(row["receipt"])
            if (not native or native[:3] != (row["native_id"], row["root_id"], row["prompt_sha256"])
                    or native_receipt != recorded or any(recorded.get(key) != value for key, value in {
                        "state": "not_sent", "native_accepted": False, "native_result_verified": False,
                        "automatic_retry": False, "fallback_attempted": False, "dispatch_id": dispatch_id,
                        "root_id": row["root_id"], "prompt_sha256": row["prompt_sha256"],
                        "target_thread_sha256": digest(row["native_id"])}.items())
                    or recorded.get("native_input_attempted", False) is not False):
                raise BridgeError("Both exact transport receipts must prove no native input attempt", 403)
            if any(recorded.get(key) is not False for key in ("native_accepted", "native_result_verified", "automatic_retry", "fallback_attempted")):
                raise BridgeError("Pre-I/O proof flags must be explicit false values", 403)
            now = time.time()
            db.execute("UPDATE jobs SET state='avalokita_not_sent',finished_at=?,error=? WHERE id=? AND state='awaiting_native_reply'",
                (now, "Exact pinned transport failed before native input; original request retained without replay", job["id"]))
            db.execute("UPDATE agents SET state='idle',active_root=NULL,error=NULL,updated_at=? WHERE id=? AND state='awaiting_native_reply' AND active_root=? AND native_session_id=? AND generation=?",
                (now, actor_id, row["root_id"], row["native_id"], job["generation"]))
            proof = {"classified": True, "dispatch_id": dispatch_id, "goal_id": goal_id, "actor": actor_id,
                     "job_id": job["id"], "state": "avalokita_not_sent", "native_attempted": False,
                     "automatic_retry": False, "transport_receipt_sha256": digest(json.dumps(recorded, sort_keys=True)),
                     "original_dispatch_preserved": True}
            local.execute("INSERT INTO native_pre_io_repairs VALUES(?,?,?,?,?)", (dispatch_id, goal_id, actor_id, json.dumps(proof), now))
            return proof

    def reserve(self, goal_id: str, actor_id: str):
        """Claim a still queued exact human job. A prior claim never falls back."""
        if ((not self.reason_transport or actor_id not in self.reason_transport.bindings)
                and (not self.source or not self._source_allowed(self.source))):
            raise BridgeError("Current native source routing context is unavailable", 403)
        receipt = self.bridge.dispatches().get(goal_id)
        binding = receipt.get("bindings", {}).get(actor_id) if receipt else None
        if not receipt or receipt["state"] != "queued" or not binding or binding[0] != "codex":
            raise BridgeError("Only an exact recorded Avalokita Codex human input can use IPC", 403)
        self.bridge._runtime()  # Same pinned already-running broker identity.
        with self.admission() as (db, _, harmony):
            job = db.execute("SELECT * FROM jobs WHERE event_id=? AND agent_id=?", (receipt["event_id"], actor_id)).fetchone()
            if not job:
                raise BridgeError("Exact original job does not exist", 409)
            prior = db.execute(f"SELECT * FROM {TABLE} WHERE job_id=?", (job["id"],)).fetchone()
            if prior:
                return {"dispatch_id": prior["dispatch_id"], "state": prior["state"], "duplicate_suppressed": True,
                        "transport": self._selection(db, prior)["transport"]}
            try:
                selected = self.reason_transport.selection(actor_id, binding[1], self.bridge.workspace) if self.reason_transport else None
            except (ReasonEngineError, OSError, ValueError):
                raise BridgeError("Trusted exact Reason binding is unavailable", 403) from None
            selected = selected or {"transport": "codex_ipc"}
            if selected["transport"] == "codex_ipc" and (not self.source or not self._source_allowed(self.source)):
                raise BridgeError("Current native source routing context is unavailable", 403)
            if job["state"] not in {"queued", "avalokita_native_pending"}:
                return {"state": "legacy_owned", "native_attempted": False, "fallback_attempted": False}
            root = db.execute("SELECT * FROM roots WHERE id=?", (job["root_id"],)).fetchone()
            if not root:
                raise BridgeError("Original human root is missing", 409)
            row = {"dispatch_id": str(uuid.uuid5(uuid.NAMESPACE_URL, str(self.bridge.workspace) + ":codex-native:" + job["id"])),
                   "job_id": job["id"], "goal_id": goal_id, "actor": actor_id, "native_id": binding[1],
                   "source_id": "user" if selected["transport"] == "reason" else self.source,
                   "root_id": job["root_id"], "event_id": job["event_id"], "deadline": root["deadline"]}
            actor, _, event, _ = self._validate(db, row, claimed=False, harmony=harmony, selection=selected)
            if db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND state IN ('running','awaiting_native_reply','waiting_user')", (actor_id,)).fetchone():
                return {"state": "legacy_owned", "native_attempted": False, "fallback_attempted": False}
            prompt = self._prompt(**{k: row[k] for k in ("goal_id", "actor", "native_id", "root_id", "job_id", "event_id")},
                                  human_body=event["body"], roster=self._roster(db, receipt,
                                      reason_actor=actor_id if selected["transport"] == "reason" else None))
            if len(prompt.encode()) > 65536:
                raise BridgeError("The bounded native envelope exceeds 64 KiB", 400)
            row["prompt_sha256"] = digest(prompt)
            row.update(state="claimed", receipt="{}", created_at=time.time())
            BusStore._claim_execution_in(db, actor_id, job["id"], execution_owner="native_dispatch",
                                         native_completion_domain="avalokita_direct",
                                         allowed_states=("queued", "avalokita_native_pending"), allow_existing=True)
            db.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row[key] for key in ("dispatch_id", "job_id", "goal_id", "actor", "native_id", "source_id", "root_id", "event_id", "prompt_sha256", "deadline", "state", "receipt", "created_at")))
            db.execute(f"INSERT INTO {TRANSPORTS} VALUES(?,?)", (row["dispatch_id"], json.dumps(selected, sort_keys=True)))
            db.execute(f"INSERT INTO {ENVELOPES} VALUES(?,?,?)", (row["dispatch_id"], prompt, self._input_hash(db, event)))
            db.execute("UPDATE jobs SET state='awaiting_native_reply',started_at=? WHERE id=? AND state IN ('queued','avalokita_native_pending')",
                       (row["created_at"], job["id"]))
            db.execute("UPDATE roots SET used=used+1 WHERE id=?", (job["root_id"],))
            db.execute("UPDATE agents SET state='awaiting_native_reply',active_root=?,updated_at=? WHERE id=?", (job["root_id"], row["created_at"], actor_id))
            return {"dispatch_id": row["dispatch_id"], "state": "claimed", "job_id": job["id"], "event_id": job["event_id"], "root_id": job["root_id"],
                    "transport": selected["transport"]}

    def deliver(self, dispatch_id: str):
        """No retries after admission, including crash/timeout before native ACK."""
        with closing(self.bridge._pinned_db(read_only=True)) as probe:
            saved = probe.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if not saved:
                raise BridgeError("Unknown durable native dispatch", 403)
            selected = self._selection(probe, saved)
        if selected.get("transport") == "reason":
            return self._deliver_reason(dispatch_id, selected)
        # Commit the one-attempt marker BEFORE any native I/O. A crash cannot
        # roll it back with the long admission transaction and enable replay.
        with self.admission() as (db, local, harmony):
            stored = db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone()
            if not stored:
                raise BridgeError("Unknown durable native dispatch", 403)
            row = dict(stored)
            if row["state"] != "claimed":
                return {"state": row["state"], "duplicate_suppressed": True, **json.loads(row["receipt"])}
            self._validate_dispatch(db, local, harmony, row, claimed=True)
            BusStore._reserve_native_input_in(db, row["actor"], row["job_id"], row["event_id"], row["root_id"], row["native_id"], provider="codex", native_completion_domain="avalokita_direct")
            db.execute(f"UPDATE {TABLE} SET state='attempting' WHERE dispatch_id=? AND state='claimed'", (dispatch_id,))
        with self.admission() as (db, local, harmony):
            row = dict(db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone())
            _, root, event, _ = self._validate_dispatch(db, local, harmony, row, claimed=True)
            BusStore._validate_native_input_in(db, row["actor"], row["job_id"], row["event_id"], row["root_id"], row["native_id"], require_reserved=True, provider="codex", native_completion_domain="avalokita_direct")
            prompt = self._envelope(db, row, event)
            if digest(prompt) != row["prompt_sha256"]:
                raise BridgeError("Native public envelope changed after authorization", 409)
            record = {"source_thread_id": self.source, "target_thread_id": row["native_id"], "root_id": row["root_id"],
                      "dispatch_id": dispatch_id, "prompt_sha256": row["prompt_sha256"], "deadline": row["deadline"],
                      "human_authorized": True, "root_active": True, "recipient_registered": True,
                      "workspace_paused": False, "recipient_paused": False,
                      # This is ONE reserved turn, debited at claim, rather than
                      # a reset of the root's unreserved remaining budget.
                      "budget_remaining": 1, "claim_state": "claimed"}
            def gate(supplied):
                if dict(supplied) != record:
                    return False
                try:
                    self._validate_dispatch(db, local, harmony, row, claimed=True)
                    BusStore._validate_native_input_in(db, row["actor"], row["job_id"], row["event_id"], row["root_id"], row["native_id"], require_reserved=True, provider="codex", native_completion_domain="avalokita_direct")
                    current = db.execute(f"SELECT state,prompt_sha256 FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone()
                    return (current["state"] == "attempting" and current["prompt_sha256"] == digest(prompt)
                            and local.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] != "true")
                except (BridgeError, ValueError):
                    return False
            # Preserve the legacy IPC protocol timeout. No model turn is awaited.
            adapter = self.adapter_factory(ledger_path=self.bridge.receipts.parent / "codex-ipc.sqlite3", authorize_send=gate,
                                           validate_source=self._source_allowed, timeout=2)
            try:
                result = adapter.send_authorized(row["native_id"], prompt, source_thread_id=self.source, root_id=row["root_id"],
                                                 dispatch_id=dispatch_id, deadline=row["deadline"], authorization_record=record)
            except Exception:
                result = {"state": "delivery_unknown", "native_accepted": False, "native_result_verified": False,
                          "automatic_retry": False, "fallback_attempted": False}
            state = result.get("state", "delivery_unknown")
            db.execute(f"UPDATE {TABLE} SET state=?,receipt=? WHERE dispatch_id=?", (state, json.dumps(result), dispatch_id))
            return result

    @staticmethod
    def _goal_identity(goal):
        return [goal["id"], goal["created_at"], goal["idempotency_key"], digest(goal["prompt"])]

    def _deliver_reason(self, dispatch_id, selected):
        # Nothing in the frozen adapter (ledger waits, executable hash, process
        # startup, stdio or cleanup) may run under the control writer locks.
        reason_transport = self.reason_transport
        with closing(self.bridge._pinned_db(read_only=True)) as probe:
            row = dict(probe.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone())
        if row["state"] != "claimed":
            return {"state": row["state"], "duplicate_suppressed": True, **json.loads(row["receipt"])}
        if reason_transport is None:
            raise BridgeError("Frozen Reason configuration is unavailable; fallback is disabled", 403)
        try:
            prepared = reason_transport.selection(row["actor"], row["native_id"], self.bridge.workspace, check_files=True)
        except (ReasonEngineError, OSError, ValueError):
            raise BridgeError("Frozen Reason preparation is unavailable", 403) from None
        if prepared != selected:
            raise BridgeError("Frozen Reason selection changed; fallback is disabled", 403)
        with self.admission() as (db, local, harmony):
            row = dict(db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone())
            if row["state"] != "claimed":
                return {"state": row["state"], "duplicate_suppressed": True, **json.loads(row["receipt"])}
            _, _, human, _ = self._validate_dispatch(db, local, harmony, row, claimed=True)
            receipt, _, _ = self._peer_authority(db, local, harmony, row["goal_id"], reserved=True)
            prompt = self._envelope(db, row, human)
            if digest(prompt) != row["prompt_sha256"]:
                raise BridgeError("Native public envelope changed after authorization", 409)
            goal = harmony.execute("SELECT * FROM goals WHERE id=?", (row["goal_id"],)).fetchone()
            goal_identity = self._goal_identity(goal)
            input_hash = self._input_hash(db, human)
            BusStore._reserve_native_input_in(db, row["actor"], row["job_id"], row["event_id"], row["root_id"], row["native_id"], provider="codex", native_completion_domain="avalokita_direct")
            db.execute(f"UPDATE {TABLE} SET state='attempting' WHERE dispatch_id=? AND state='claimed'", (dispatch_id,))
        binding = NativeBinding(**selected["binding"])
        record = {"authority_scope": "harmony_sharebus", "source_actor_id": "user", "target_actor_id": row["actor"],
                  "root_id": row["root_id"], "goal_id": row["goal_id"], "dispatch_id": row["dispatch_id"],
                  "input_id": row["event_id"], "job_id": row["job_id"], "event_id": row["event_id"],
                  "human_event_id": human["id"], "binding_sha256": binding.sha256,
                  "prompt_sha256": row["prompt_sha256"], "deadline": row["deadline"],
                  "participants": receipt["participants"], "bindings": receipt["bindings"],
                  "harmony_goal": goal_identity,
                  "human_authorized": True, "root_active": True, "recipient_registered": True,
                  "workspace_paused": False, "recipient_paused": False, "budget_remaining": 1, "claim_state": "claimed"}
        checks = 0
        granted = False

        def gate(supplied):
            nonlocal checks, granted
            checks += 1
            if dict(supplied) != record or checks not in {1, 2}:
                return False
            try:
                with self.admission() as (db, local, harmony):
                    current = db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone()
                    if not current or current["state"] != "attempting":
                        return False
                    self._validate_dispatch(db, local, harmony, current, claimed=True)
                    BusStore._validate_native_input_in(db, current["actor"], current["job_id"], current["event_id"], current["root_id"], current["native_id"], require_reserved=True, provider="codex", native_completion_domain="avalokita_direct")
                    authority, _, source = self._peer_authority(db, local, harmony, current["goal_id"], reserved=True)
                    goal = harmony.execute("SELECT * FROM goals WHERE id=?", (current["goal_id"],)).fetchone()
                    if (authority["participants"] != record["participants"] or authority["bindings"] != record["bindings"]
                            or source["id"] != record["human_event_id"] or self._goal_identity(goal) != goal_identity
                            or self._input_hash(db, source) != input_hash
                            or digest(self._envelope(db, current, source)) != record["prompt_sha256"]):
                        return False
                    if checks == 2:
                        # This committed fact is the pause/admission ordering
                        # point. A later pause stops new grants, not this one
                        # already admitted input. The first gate grants nothing.
                        fact = {**record, "selection_sha256": digest(self._routing_bytes(selected).decode()),
                                "input_sha256": input_hash, "granted_at": time.time()}
                        encoded = self._routing_bytes(fact).decode()
                        db.execute(f"INSERT INTO {GRANTS} VALUES(?,?,?)", (dispatch_id, encoded, digest(encoded)))
                        db.execute(f"UPDATE {TABLE} SET state='commit_granted' WHERE dispatch_id=? AND state='attempting'", (dispatch_id,))
                granted = checks == 2
                return True
            except (BridgeError, ReasonEngineError, ValueError, OSError):
                return False

        adapter = ReasonEngineAdapter(engine_path=selected["engine_path"], engine_sha256=selected["engine_sha256"],
            ledger_path=self.bridge.receipts.parent / "reason-transport.sqlite3", authorize_send=gate,
            # Rust reserves 2 seconds from this value for its own cleanup;
            # 60 seconds preserves its 30-second queue-helper wait ceiling.
            # This does not bound preparation/cleanup or total call latency.
            timeout=60, process_factory=reason_transport.process_factory)
        try:
            result = adapter.send_authorized(binding, prompt, source_actor_id="user", target_actor_id=row["actor"],
                root_id=row["root_id"], dispatch_id=row["dispatch_id"], deadline=row["deadline"], authorization_record=record)
        except Exception:
            result = {"state": "delivery_unknown", "native_accepted": False, "native_result_verified": False,
                      "automatic_retry": False, "fallback_attempted": False}
        result = {**result, "transport_engine": "reason", "commit_granted": granted}
        # Receipt reconciliation does not admit new input and must still work
        # after pause/expiry. Do not reacquire Harmony/control writers here.
        with closing(self.bridge._pinned_db()) as db, self.bridge._pinned_transaction(db):
            db.execute("BEGIN IMMEDIATE")
            db.execute(f"UPDATE {TABLE} SET state=?,receipt=? WHERE dispatch_id=? AND state IN ('attempting','commit_granted')",
                       (result["state"], json.dumps(result), dispatch_id))
        return result

    @staticmethod
    def _reason_grant(bridge, db, local, row, selected, *, harmony):
        """Verify immutable admission provenance, without granting new work."""
        grant = db.execute(f"SELECT * FROM {GRANTS} WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
        if not grant or digest(grant["fact"]) != grant["fingerprint"]:
            raise BridgeError("Original immutable Reason commit grant is unavailable", 403)
        fact = json.loads(grant["fact"])
        if not isinstance(fact, dict):
            raise BridgeError("Original Reason commit provenance is invalid", 403)
        binding = NativeBinding(**selected["binding"]) if selected.get("transport") == "reason" else None
        job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
        actor = db.execute("SELECT * FROM agents WHERE id=?", (row["actor"],)).fetchone()
        root = db.execute("SELECT * FROM roots WHERE id=?", (row["root_id"],)).fetchone()
        human = db.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone()
        envelope = db.execute(f"SELECT * FROM {ENVELOPES} WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
        saved = local.execute("SELECT * FROM dispatch WHERE goal_id=?", (row["goal_id"],)).fetchone()
        receipt = json.loads(saved["receipt"]) if saved else {}
        metadata = json.loads(human["metadata"]) if human else {}
        goal = harmony.execute("SELECT * FROM goals WHERE id=?", (row["goal_id"],)).fetchone()
        expected = {"authority_scope": "harmony_sharebus", "source_actor_id": "user", "target_actor_id": row["actor"], "root_id": row["root_id"],
                    "goal_id": row["goal_id"], "dispatch_id": row["dispatch_id"], "input_id": row["event_id"],
                    "job_id": row["job_id"], "event_id": row["event_id"], "human_event_id": row["event_id"],
                    "prompt_sha256": row["prompt_sha256"], "deadline": row["deadline"], "budget_remaining": 1,
                    "claim_state": "claimed", "human_authorized": True, "root_active": True,
                    "recipient_registered": True, "workspace_paused": False, "recipient_paused": False}
        if (not binding or binding.provider != "codex" or row["source_id"] != "user"
                or any(fact.get(key) != value for key, value in expected.items())
                or fact.get("binding_sha256") != binding.sha256
                or fact.get("selection_sha256") != digest(NativeDispatcher._routing_bytes(selected).decode())
                or not goal or fact.get("harmony_goal") != NativeDispatcher._goal_identity(goal)
                or type(fact.get("granted_at")) not in {int, float}
                or not row["created_at"] <= fact["granted_at"] < row["deadline"]
                or not job or job["agent_id"] != row["actor"] or job["event_id"] != row["event_id"] or job["root_id"] != row["root_id"]
                or job["execution_owner"] != "native_dispatch" or job["native_input_reserved_at"] is None
                or BusStore._native_completion_domain_in(db, job) != "avalokita_direct"
                or not actor or actor["provider"] != binding.provider or actor["native_session_id"] != binding.native_id
                or binding.native_id != row["native_id"] or actor["mode"] not in NATIVE_MODES
                or type(job["generation"]) is not int or job["generation"] != actor["generation"]
                or not root or not human or root["human_event_id"] != human["id"] or human["sender"] != "user"
                or human["root_id"] != row["root_id"] or metadata.get("human_authorized") is not True
                or metadata.get("avalokita_goal_id") != row["goal_id"] or metadata.get("bindings") != receipt.get("bindings")
                or fact.get("bindings") != receipt.get("bindings") or fact.get("participants") != receipt.get("participants")
                or receipt.get("bindings", {}).get(row["actor"]) != ["codex", row["native_id"]]
                or row["actor"] not in receipt.get("native_ipc_ids", [])
                or not saved or saved["root_id"] != row["root_id"] or saved["event_id"] != row["event_id"]
                or not envelope or digest(envelope["prompt"]) != row["prompt_sha256"]
                or envelope["input_sha256"] != fact.get("input_sha256")
                or NativeDispatcher._input_hash(db, human) != fact.get("input_sha256")):
            raise BridgeError("Immutable Reason grant no longer matches the exact original human input", 403)
        return actor, job, grant

    @staticmethod
    def _reason_input_attempt(bridge, row, selected):
        """A grant alone is not proof that the original native input was tried."""
        bridge._guard_workspace_identity()
        if selected.get("transport") != "reason":
            raise BridgeError("Original immutable Reason selection is required", 403)
        path = bridge.receipts.parent / "reason-transport.sqlite3"
        for file in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            BusStore.reject_linked_path(file)
            if file.exists() and (not file.is_file() or file.stat().st_nlink != 1):
                raise BridgeError("Original Reason transport observation is unavailable", 403)
        try:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as transport:
                native = transport.execute("SELECT receipt FROM avalokita_reason_dispatch WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
                attempted = transport.execute("SELECT fingerprint,receipt FROM avalokita_reason_attempts WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
            native = json.loads(native[0]) if native else {}
            engine = json.loads(attempted[1]) if attempted else {}
        except (OSError, ValueError, sqlite3.Error):
            raise BridgeError("Original durable Reason input attempt is unavailable", 403) from None
        bridge._guard_workspace_identity()
        if (not attempted or native.get("native_input_attempted") is not True or engine.get("native_input_attempted") is not True
                or native.get("engine_request_sha256") != attempted[0]
                or native.get("binding_sha256") != NativeBinding(**selected["binding"]).sha256
                or native.get("engine_sha256") != selected["engine_sha256"]
                or native.get("prompt_sha256") != row["prompt_sha256"] or native.get("target_actor_id") != row["actor"]
                or native.get("goal_id") != row["goal_id"] or native.get("input_id") != row["event_id"]
                or any(value.get("dispatch_id") != row["dispatch_id"] or value.get("root_id") != row["root_id"] for value in (native, engine))):
            raise BridgeError("Result has no exact durable original native input attempt", 403)

    @staticmethod
    def complete_reason(bridge, token, *, actor, native_session_id, job_id, event_id, text):
        """Compatibility entry to the same kernel, restricted to Reason."""
        return NativeDispatcher.complete_native(bridge, token, actor=actor, native_session_id=native_session_id,
            job_id=job_id, event_id=event_id, text=text, required_transport="reason")

    @staticmethod
    def complete_native(bridge, token, *, actor, native_session_id, job_id, event_id, text, required_transport=None):
        """One authenticated completion kernel for every exact direct route."""
        if not isinstance(text, str) or not 1 <= len(text.encode("utf-8")) <= 65536:
            raise BridgeError("Native selected result must contain 1 byte to 64 KiB of text")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in (actor, job_id, event_id)):
            raise BridgeError("Exact original native actor, session, job and event are required", 403)
        if not isinstance(native_session_id, str) or not 1 <= len(native_session_id) <= 256:
            raise BridgeError("Exact original native session identifier is required", 403)
        dispatcher = NativeDispatcher(bridge)
        # Transport inspection is outside all canonical writers. Immutable
        # selection and exact identity are revalidated in the final writer.
        with closing(bridge._pinned_db(read_only=True)) as probe:
            original = probe.execute(f"SELECT * FROM {TABLE} WHERE job_id=? AND event_id=? AND actor=?",
                (job_id, event_id, actor)).fetchone()
            if not original or original["native_id"] != native_session_id:
                raise BridgeError("Result does not identify the original native input", 403)
            selected = NativeDispatcher._selection(probe, original)
            expected = {key: original[key] for key in original.keys() if key not in {"state", "receipt"}}
            selection_bytes = NativeDispatcher._routing_bytes(selected)
            job = probe.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or BusStore._native_completion_domain_in(probe, job) != "avalokita_direct":
                raise BridgeError("Original direct completion domain is unavailable", 403)
        reason = selected["transport"] == "reason"
        if len(native_session_id) > (200 if reason else 128):
            raise BridgeError("Native session identifier exceeds the immutable transport limit", 403)
        if required_transport is not None and selected["transport"] != required_transport:
            raise BridgeError("Result does not identify the required immutable native route", 403)
        if reason:
            NativeDispatcher._reason_input_attempt(bridge, original, selected)
        else:
            NativeDispatcher._ipc_input_attempt(bridge, original)
        with dispatcher.harmony._connection(write=True) as harmony, bridge._db() as local, closing(bridge._pinned_db()) as db, bridge._pinned_transaction(db):
            local.execute("BEGIN IMMEDIATE")
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"SELECT * FROM {TABLE} WHERE job_id=? AND event_id=? AND actor=?",
                (job_id, event_id, actor)).fetchone()
            if not row or any(row[key] != value for key, value in expected.items()):
                raise BridgeError("Result does not identify the exact original native input", 403)
            selected = NativeDispatcher._selection(db, row)
            if NativeDispatcher._routing_bytes(selected) != selection_bytes:
                raise BridgeError("Original native transport selection changed", 403)
            if reason:
                owner, job, grant = NativeDispatcher._reason_grant(bridge, db, local, row, selected, harmony=harmony)
                result_meta = {"avalokita_reason_grant": grant["fingerprint"]}
            else:
                owner, job = dispatcher._ipc_completion_rows(db, local, harmony, row)
                result_meta = {"avalokita_direct_dispatch": row["dispatch_id"]}
            if not secrets.compare_digest(owner["token_hash"], digest(token)):
                raise BridgeError("Native result capability no longer matches the original actor", 403)
            final = db.execute("SELECT * FROM events WHERE sender=? AND root_id=? AND recipient='user' AND kind='result' "
                "AND json_extract(metadata,'$.job_id')=? AND json_extract(metadata,'$.state')='completed' ORDER BY seq LIMIT 1",
                (actor, row["root_id"], job_id)).fetchone()
            if job["state"] == "completed":
                if not final or digest(final["body"]) != digest(text):
                    raise BridgeError("A different final result already completed this exact input", 409)
                return {"completed": True, "job_id": job_id, "event_id": event_id, "root_id": row["root_id"], "duplicate_suppressed": True}
            if (job["state"] != "awaiting_native_reply" or owner["active_root"] != row["root_id"]
                    or job["generation"] != owner["generation"]
                    or db.execute("SELECT 1 FROM jobs WHERE agent_id=? AND id<>? AND state IN ('running','awaiting_native_reply','waiting_user')", (actor, job_id)).fetchone()
                    or row["state"] not in ({"commit_granted", "transport_accepted", "delivery_unknown"} if reason
                                            else {"attempting", "native_accepted", "delivery_unknown", "native_tool_failed"})):
                raise BridgeError("Native result no longer owns the original attempted input", 403)
            now = time.time()
            db.execute("UPDATE jobs SET state='completed',finished_at=?,error=NULL WHERE id=? AND state='awaiting_native_reply'", (now, job_id))
            db.execute("UPDATE agents SET state=CASE WHEN state IN ('paused','stopped') THEN state ELSE 'idle' END,"
                       "active_root=NULL,channel_verified=1,updated_at=? WHERE id=? AND active_root=? AND native_session_id=? AND generation=?",
                       (now, actor, row["root_id"], row["native_id"], job["generation"]))
            db.execute("INSERT INTO events(id,kind,sender,recipient,body,root_id,created_at,metadata) VALUES(?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "result", actor, "user", text, row["root_id"], now,
                 json.dumps({"job_id": job_id, "state": "completed", **result_meta})))
            return {"completed": True, "job_id": job_id, "event_id": event_id, "root_id": row["root_id"]}

    def _ipc_completion_rows(self, db, local, harmony, row):
        """Recheck canonical IPC identity and current controls in one writer."""
        saved = local.execute("SELECT * FROM dispatch WHERE goal_id=?", (row["goal_id"],)).fetchone()
        receipt = {**dict(saved), **json.loads(saved["receipt"])} if saved else {}
        owner = db.execute("SELECT * FROM agents WHERE id=?", (row["actor"],)).fetchone()
        job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
        root = db.execute("SELECT * FROM roots WHERE id=?", (row["root_id"],)).fetchone()
        human = db.execute("SELECT * FROM events WHERE id=?", (receipt.get("event_id"),)).fetchone()
        event = db.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone()
        metadata = json.loads(human["metadata"]) if human else {}
        envelope = db.execute(f"SELECT * FROM {ENVELOPES} WHERE dispatch_id=?", (row["dispatch_id"],)).fetchone()
        flags = dict(db.execute("SELECT key,value FROM config"))
        if (not saved or saved["state"] != "queued" or saved["root_id"] != row["root_id"]
                or not owner or owner["provider"] != "codex" or owner["mode"] not in NATIVE_MODES
                or owner["native_session_id"] != row["native_id"]
                or receipt.get("bindings", {}).get(row["actor"]) != ["codex", row["native_id"]]
                or row["actor"] not in receipt.get("native_ipc_ids", [])
                or not root or root["human_event_id"] != receipt.get("event_id") or root["deadline"] != row["deadline"]
                or not human or human["sender"] != "user" or human["root_id"] != row["root_id"]
                or metadata.get("human_authorized") is not True or metadata.get("avalokita_goal_id") != row["goal_id"]
                or metadata.get("bindings") != receipt.get("bindings")
                or not event or event["root_id"] != row["root_id"]
                or not job or job["agent_id"] != row["actor"] or job["event_id"] != row["event_id"] or job["root_id"] != row["root_id"]
                or BusStore._native_completion_domain_in(db, job) != "avalokita_direct"
                or job["native_input_reserved_at"] is None or type(job["generation"]) is not int or job["generation"] != owner["generation"]
                or not envelope or digest(envelope["prompt"]) != row["prompt_sha256"]
                or envelope["input_sha256"] != self._input_hash(db, event)):
            raise BridgeError("Exact original IPC input, root, binding or generation changed", 403)
        self._lineage(db, job, receipt)
        if job["state"] != "completed":
            self.harmony_guard(harmony, row["goal_id"], reserved=True)
            if (flags.get("paused") == "true" or flags.get("auto_suspended") == "true"
                    or local.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true"
                    or root["deadline"] <= time.time() or root["used"] > root["budget"]
                    or owner["state"] != "awaiting_native_reply"):
                raise BridgeError("Current controls prevent IPC result completion", 409)
        return owner, job

    def _completed_reason_observation(self, db, local, harmony, dispatch_id, result_id):
        row = db.execute(f"SELECT * FROM {TABLE} WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if not row:
            raise BridgeError("Original Reason dispatch is unavailable", 403)
        selected = self._selection(db, row)
        actor, job, grant = self._reason_grant(self.bridge, db, local, row, selected, harmony=harmony)
        result = db.execute("SELECT * FROM events WHERE id=?", (result_id,)).fetchone()
        final_meta = json.loads(result["metadata"]) if result else {}
        if (row["state"] not in {"commit_granted", "transport_accepted", "delivery_unknown", "native_result_verified"}
                or job["state"] != "completed" or actor["channel_verified"] != 1
                or not result or result["sender"] != row["actor"] or result["recipient"] != "user" or result["kind"] != "result"
                or result["root_id"] != row["root_id"] or final_meta.get("job_id") != row["job_id"]
                or final_meta.get("state") != "completed" or final_meta.get("avalokita_reason_grant") != grant["fingerprint"]):
            raise BridgeError("Selected callback does not prove the exact original Reason input completed", 403)
        return row, selected, {"kind": "result_completed", "actor_id": row["actor"], "goal_id": row["goal_id"],
                               "input_id": row["event_id"], "selected_response_sha256": digest(result["body"])}

    def reconcile_completed(self, *, limit=8):
        """Settle transport observation from exact canonical callbacks; never send."""
        maximum = min(max(int(limit), 1), 32)
        results = []
        with self.harmony._connection() as harmony, self.bridge._db() as local:
            db = self.bridge._pinned_db(read_only=True)
            try:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TRANSPORTS,)).fetchone():
                    return []
                query = (f"SELECT d.rowid,d.dispatch_id,e.id FROM {TABLE} d JOIN {TRANSPORTS} t ON t.dispatch_id=d.dispatch_id "
                         "JOIN jobs j ON j.id=d.job_id JOIN events e ON e.root_id=d.root_id AND e.sender=d.actor "
                         "AND e.recipient='user' AND e.kind='result' AND json_extract(e.metadata,'$.job_id')=j.id "
                         "AND json_extract(e.metadata,'$.state')='completed' "
                         "WHERE j.state='completed' AND json_extract(t.selection,'$.transport')='reason' ")
                batch = db.execute(query + "AND d.rowid>? ORDER BY d.rowid LIMIT ?", (self.reason_reconcile_after, maximum)).fetchall()
                if len(batch) < maximum:
                    batch += db.execute(query + "AND d.rowid<=? ORDER BY d.rowid LIMIT ?", (self.reason_reconcile_after, maximum - len(batch))).fetchall()
                if batch:
                    self.reason_reconcile_after = batch[-1][0]
                for _, dispatch_id, result_id in batch:
                    try:
                        row, selected, observation = self._completed_reason_observation(db, local, harmony, dispatch_id, result_id)
                        self._reason_input_attempt(self.bridge, row, selected)
                        def validate(value):
                            return (dict(value) == observation and self._completed_reason_observation(
                                db, local, harmony, dispatch_id, result_id)[2] == observation)
                        adapter = ReasonEngineAdapter(engine_path=selected["engine_path"], engine_sha256=selected["engine_sha256"],
                            ledger_path=self.bridge.receipts.parent / "reason-transport.sqlite3", timeout=2)
                        results.append(adapter.reconcile_native_observation(dispatch_id, row["root_id"], NativeBinding(**selected["binding"]),
                            observation, validate_observation=validate))
                    except (BridgeError, ReasonEngineError, OSError, ValueError, sqlite3.Error):
                        continue
            finally:
                db.close()
        return results


def complete(workspace, *, actor, native_session_id, job_id, event_id, text):
    """Authenticated selected result only; never creates a human authority root."""
    bridge = SharebusBridge(workspace)
    if not isinstance(actor, str) or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in actor):
        raise BridgeError("Exact actor ID required")
    token = private_read(bridge.private / "credentials" / (actor + ".token"), workspace_guard=bridge._guard_workspace_identity)
    result = NativeDispatcher.complete_native(bridge, token, actor=actor, native_session_id=native_session_id,
        job_id=job_id, event_id=event_id, text=text)
    if result.get("completed") is True:
        try:
            NativeDispatcher(bridge).reconcile_completed()
        except (BridgeError, OSError, ValueError, sqlite3.Error):
            # The authenticated canonical result is already committed. Keep a
            # transport observation failure distinct; later reconciliation is
            # observation only and cannot replay the native input.
            result["transport_observation_pending"] = True
    return result


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    result = sub.add_parser("complete")
    for key in ("workspace", "actor", "native-session-id", "job-id", "event-id", "text"):
        result.add_argument("--" + key, required=True)
    args = parser.parse_args()
    try:
        value = complete(args.workspace, actor=args.actor, native_session_id=args.native_session_id,
                         job_id=args.job_id, event_id=args.event_id, text=args.text)
        print(json.dumps(value, ensure_ascii=False))
    except Exception as exc:
        parser.exit(1, "Exact native completion failed (" + type(exc).__name__ + "); original work is preserved.\n")


if __name__ == "__main__":
    main()
