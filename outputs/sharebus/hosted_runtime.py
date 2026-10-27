"""Owned CLI identities and write-ahead delivery facts; never native transcripts."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import tomllib
import uuid

from store import BusError, BusStore, WorkspaceRetired

SUPPORTED = ("codex", "claude", "antigravity")
PREFIX = "avalokita-hosted/1:"


def resolve_mcp_python():
    """Use a verified existing interpreter; never install or alter global config."""
    from install import installed_resources_root
    try:
        installed = installed_resources_root()
    except ValueError:
        raise BusError("Bundled runtime metadata could not be validated; no provider input was sent", 409) from None
    if os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or installed is not None:
        executable = Path(sys.executable)
        resources = installed or Path(os.environ.get("AVALOKITA_RESOURCES_ROOT", ""))
        if (not resources.is_absolute() or not executable.is_absolute() or not executable.is_file()
                or not executable.resolve().is_relative_to((resources / "python").resolve())
                or (installed is not None and Path(__file__).resolve().parent != installed / "sharebus")):
            raise BusError("Packaged MCP requires the app-owned bundled Python runtime; no provider input was sent", 409)
        BusStore.reject_linked_path(executable)
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
        try:
            result = subprocess.run([str(executable), "-B", "-I", "-c",
                "import importlib.metadata; from mcp.server.fastmcp import FastMCP; print(importlib.metadata.version('mcp'))"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0 and result.stdout.strip() == "1.30.0":
                return str(executable.resolve())
        except (OSError, subprocess.TimeoutExpired):
            pass
        raise BusError("Bundled MCP 1.30.0 is unavailable; no provider input was sent", 409)
    candidates = [sys.executable]
    repository = Path(__file__).resolve().parents[2]
    expected = {(repository / "outputs/sharebus/mcp_server.py").resolve(),
                (repository / "outputs/session-relay/server.py").resolve()}
    own_mcp = Path(__file__).with_name("mcp_server.py")
    own_hash = hashlib.sha256(own_mcp.read_bytes()).hexdigest()
    def matching_source(value):
        if not isinstance(value, str) or not Path(value).is_absolute():
            return False
        path = Path(value)
        if path.resolve() in expected:
            return True
        if path.name != "mcp_server.py" or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return False
        BusStore.reject_linked_path(path)
        return hashlib.sha256(path.read_bytes()).hexdigest() == own_hash
    config = Path.home() / ".codex/config.toml"
    if config.is_file() and config.stat().st_size <= 1024 * 1024:
        BusStore.reject_linked_path(config)
        servers = tomllib.loads(config.read_text(encoding="utf-8")).get("mcp_servers", {})
        for key in ("sharebus", "cross_session"):
            value = servers.get(key, {})
            command, arguments = value.get("command"), value.get("args", [])
            if (isinstance(command, str) and Path(command).is_absolute() and isinstance(arguments, list)
                    and any(matching_source(item) for item in arguments)):
                candidates.append(command)
    for command in dict.fromkeys(candidates):
        path = Path(command)
        if path.name.lower() not in {"python.exe", "python", "python3", "python3.11", "python3.12", "python3.13", "python3.14"} or not path.is_file():
            continue
        BusStore.reject_linked_path(path)
        environment = {key: value for key, value in os.environ.items() if key.upper() not in {"PYTHONPATH", "PYTHONHOME"}}
        try:
            result = subprocess.run([str(path), "-I", "-c", "import importlib.metadata; from mcp.server.fastmcp import FastMCP; print(importlib.metadata.version('mcp'))"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0 and result.stdout.strip() == "1.30.0":
                return str(path.resolve())
        except (OSError, subprocess.TimeoutExpired):
            continue
    raise BusError("An existing configured MCP 1.30.0 Python runtime is required; no provider input was sent", 409)


def is_hosted(agent):
    return (agent.get("mode") in {"managed_new", "managed_resume"}
            and isinstance(agent.get("native_address"), str) and agent["native_address"].startswith(PREFIX))


class HostedLedger:
    def __init__(self, store, *, execution_ready=None):
        self.store = store
        self._execution_ready = execution_ready
        self.identity = None
        if store._workspace_guard is None:
            info = store.workspace.stat()
            if os.name == "nt" and info.st_ino == 0:
                raise BusError("Hosted workspace identity metadata is unavailable", 503)
            self.identity = info.st_dev, info.st_ino
        self.retired = False

    def execution_ready(self):
        try:
            return self._execution_ready is not None and self._execution_ready() is True
        except Exception:
            return False

    def guard(self):
        if self.retired:
            raise WorkspaceRetired()
        if self.store._workspace_guard is not None:
            try:
                self.store.guard_workspace()
            except WorkspaceRetired:
                self.retired = True
                raise
            return
        if os.name == "nt" and self.identity[1] == 0:
            raise BusError("Hosted workspace identity metadata is unavailable", 503)
        try:
            self.store.guard_workspace()
            for path in (*reversed(self.store.workspace.parents), self.store.workspace):
                info = path.lstat()
                if os.name == "nt" and info.st_ino == 0:
                    raise BusError("Hosted workspace identity metadata is unavailable", 503)
                if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or not stat.S_ISDIR(info.st_mode)):
                    self.retired = True
                    raise WorkspaceRetired()
            if (info.st_dev, info.st_ino) != self.identity:
                self.retired = True
                raise WorkspaceRetired()
        except FileNotFoundError:
            self.retired = True
            raise WorkspaceRetired() from None
        except OSError:
            raise BusError("Hosted workspace identity could not be checked", 503) from None

    @staticmethod
    def exists(db):
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='avalokita_hosted_owners'").fetchone())

    def recover(self):
        """Only our owned interrupted inputs are fenced. Never replay them."""
        self.guard()
        with self.store.connection() as db:
            if not self.exists(db):
                return
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT j.id,j.agent_id,j.root_id,a.input_attempted,a.state FROM jobs j JOIN avalokita_hosted_owners o ON o.actor=j.agent_id JOIN agents n ON n.id=o.actor AND n.native_address=o.owner_id LEFT JOIN avalokita_hosted_attempts a ON a.job_id=j.id WHERE j.state IN ('running','awaiting_native_reply')").fetchall()
            for row in rows:
                reason = "Owned runtime exited during an input; automatic replay is disabled"
                db.execute("UPDATE jobs SET state='needs_human',error=? WHERE id=?", (reason, row["id"]))
                db.execute("UPDATE agents SET state=CASE WHEN state IN ('paused','stopped') THEN state ELSE 'needs_human' END,error=? WHERE id=?", (reason, row["agent_id"]))
                if row["input_attempted"] and row["state"] != "completed":
                    db.execute("UPDATE avalokita_hosted_attempts SET state='delivery_unknown',updated_at=? WHERE job_id=?", (time.time(), row["id"]))
                db.execute("UPDATE avalokita_hosted_approvals SET state='expired' WHERE job_id=? AND state IN ('pending','sending')", (row["id"],))

    def configure(self, providers, candidates):
        self.guard()
        if (not isinstance(providers, list) or len(providers) > 3 or len(set(providers)) != len(providers)
                or any(item not in SUPPORTED for item in providers)):
            raise BusError("Select supported hosted providers")
        candidates = {item.get("provider"): item for item in candidates}
        with self.store.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS avalokita_hosted_owners (actor TEXT PRIMARY KEY,provider TEXT UNIQUE NOT NULL,owner_id TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL,native_session_id TEXT,generation INTEGER NOT NULL DEFAULT 0,pid INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS avalokita_hosted_attempts (job_id TEXT PRIMARY KEY,actor TEXT NOT NULL,event_id TEXT NOT NULL,root_id TEXT NOT NULL,owner_id TEXT NOT NULL,generation INTEGER NOT NULL,state TEXT NOT NULL,input_attempted INTEGER NOT NULL DEFAULT 0,native_session_id TEXT,native_turn_id TEXT,updated_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS avalokita_hosted_approvals (approval_id TEXT PRIMARY KEY,actor TEXT NOT NULL,job_id TEXT NOT NULL,generation INTEGER NOT NULL,request TEXT NOT NULL,state TEXT NOT NULL,result TEXT)")
        for provider in providers:
            candidate = candidates.get(provider, {})
            if not candidate.get("installed") or not candidate.get("managed_supported"):
                raise BusError("Hosted provider CLI is unavailable: " + provider, 409)
            with self.store.connection() as db:
                existing = db.execute("SELECT * FROM avalokita_hosted_owners WHERE provider=?", (provider,)).fetchone()
            if existing:
                continue
            # Recover only our exact unused registration after a configure crash.
            unused = [agent for agent in self.store.agents() if agent["provider"] == provider and is_hosted(agent)]
            if len(unused) > 1:
                raise BusError("Multiple hosted owners require review", 409)
            if unused:
                agent = unused[0]
            else:
                address = PREFIX + str(uuid.uuid4())
                agent, _ = self.store.register(provider, provider + " · Avalokita", ["public-collaboration", "native-cli-session"],
                                               "managed_new", native_address=address)
            with self.store.connection() as db:
                db.execute("INSERT INTO avalokita_hosted_owners (actor,provider,owner_id,enabled) VALUES (?,?,?,0)",
                           (agent["id"], provider, agent["native_address"]))
        with self.store.connection() as db:
            db.execute("UPDATE avalokita_hosted_owners SET enabled=0")
            for provider in providers:
                db.execute("UPDATE avalokita_hosted_owners SET enabled=1 WHERE provider=?", (provider,))
        self.guard()
        return self.state()

    def state(self):
        self.guard()
        sessions, approvals = [], []
        with self.store.connection() as db:
            if not self.exists(db):
                return {"enabled": False, "providers": [], "supported": list(SUPPORTED), "state": "disabled", "sessions": [], "approvals": []}
            for owner in db.execute("SELECT * FROM avalokita_hosted_owners ORDER BY provider"):
                agent = db.execute("SELECT * FROM agents WHERE id=?", (owner["actor"],)).fetchone()
                if not agent:
                    continue
                last = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE actor=? ORDER BY updated_at DESC LIMIT 1", (owner["actor"],)).fetchone()
                blocked = bool(db.execute("SELECT 1 FROM avalokita_hosted_attempts WHERE actor=? AND input_attempted=1 AND state IN ('accepted','delivery_unknown','needs_human') LIMIT 1", (owner["actor"],)).fetchone()) and agent["state"] not in {"running", "waiting_for_user"}
                pending = db.execute("SELECT request,job_id FROM avalokita_hosted_approvals WHERE actor=? AND state='pending'", (owner["actor"],)).fetchall()
                for approval in pending:
                    request = json.loads(approval["request"])
                    approvals.append({**request, "agent_id": owner["actor"], "job_id": approval["job_id"]})
                sessions.append({"agent_id": owner["actor"], "provider": owner["provider"], "transport": "hosted_cli",
                    "enabled": bool(owner["enabled"]), "owner_id": owner["owner_id"], "generation": owner["generation"],
                    "pid": owner["pid"],
                    "identity_state": "observed" if owner["native_session_id"] else "pending", "native_session_id": owner["native_session_id"],
                    "state": agent["state"], "blocked": blocked, "approval_pending": bool(pending),
                    "native_turn_id": last["native_turn_id"] if last else None, "delivery_state": last["state"] if last else "not_sent"})
        providers = [item["provider"] for item in sessions if item["enabled"]]
        return {"enabled": bool(providers), "providers": providers, "supported": list(SUPPORTED),
                "state": "blocked" if any(item["blocked"] or item["state"] in {"needs_human", "unavailable"} for item in sessions if item["enabled"]) else "ready" if providers else "disabled",
                "sessions": sessions, "approvals": approvals}

    def begin(self, agent, job, generation):
        self.guard()
        if not self.execution_ready():
            raise BusError("Avalokita hosted execution authority is unavailable", 409)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT * FROM avalokita_hosted_owners WHERE actor=?", (agent["id"],)).fetchone()
            if (not owner or not owner["enabled"] or owner["owner_id"] != agent["native_address"] or owner["native_session_id"] != agent["native_session_id"]
                    or db.execute("SELECT 1 FROM avalokita_hosted_attempts WHERE actor=? AND input_attempted=1 AND state IN ('accepted','delivery_unknown','needs_human')", (agent["id"],)).fetchone()):
                raise BusError("Owned native identity or unresolved delivery requires review", 409)
            if db.execute("SELECT 1 FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone():
                raise BusError("Hosted input was already attempted; replay refused", 409)
            db.execute("UPDATE avalokita_hosted_owners SET generation=? WHERE actor=?", (generation, agent["id"]))
            db.execute("INSERT INTO avalokita_hosted_attempts (job_id,actor,event_id,root_id,owner_id,generation,state,updated_at,native_session_id) VALUES (?,?,?,?,?,?,'not_sent',?,?)",
                       (job["id"], agent["id"], job["event_id"], job["root_id"], owner["owner_id"], generation, time.time(), owner["native_session_id"]))

    def event(self, agent_id, job, generation, event):
        self.guard()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=? AND actor=? AND generation=?", (job["id"], agent_id, generation)).fetchone()
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            owner = db.execute("SELECT * FROM avalokita_hosted_owners WHERE actor=?", (agent_id,)).fetchone()
            if (not attempt or not agent or not owner or not is_hosted(dict(agent)) or agent["native_address"] != attempt["owner_id"]
                    or owner["owner_id"] != attempt["owner_id"] or owner["generation"] != generation or agent["generation"] != generation
                    or agent["provider"] != owner["provider"] or agent["native_session_id"] != owner["native_session_id"]):
                raise BusError("Hosted callback no longer owns this generation", 409)
            native = event.get("session_id")
            if native is not None:
                if (not isinstance(native, str) or not 1 <= len(native) <= 256 or any(ord(c) < 32 for c in native)
                        or (owner["native_session_id"] and native != owner["native_session_id"])):
                    raise BusError("Native session changed; adoption refused", 409)
                db.execute("UPDATE avalokita_hosted_owners SET native_session_id=? WHERE actor=?", (native, agent_id))
                db.execute("UPDATE agents SET native_session_id=?,mode='managed_resume',updated_at=? WHERE id=?", (native, time.time(), agent_id))
                db.execute("UPDATE avalokita_hosted_attempts SET native_session_id=? WHERE job_id=?", (native, job["id"]))
            kind = event.get("type")
            if kind == "input_attempt":
                root = db.execute("SELECT * FROM roots WHERE id=?", (job["root_id"],)).fetchone()
                current = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
                flags = dict(db.execute("SELECT key,value FROM config"))
                if (not self.execution_ready() or not owner["enabled"] or not root or root["deadline"] <= time.time() or root["used"] > root["budget"] or flags.get("paused") == "true"
                        or flags.get("auto_suspended") == "true" or agent["state"] != "running" or agent["active_root"] != job["root_id"]
                        or not current or current["state"] != "running" or attempt["input_attempted"]):
                    raise BusError("Human control or exact input ownership prevents native write", 409)
                db.execute("UPDATE avalokita_hosted_attempts SET input_attempted=1,state='delivery_unknown',updated_at=? WHERE job_id=?", (time.time(), job["id"]))
            elif kind == "turn" and event.get("status") == "accepted":
                turn = event.get("turn_id")
                if not attempt["input_attempted"] or not isinstance(turn, str) or not 1 <= len(turn) <= 256:
                    raise BusError("Accepted turn lacks an exact attempted input", 409)
                db.execute("UPDATE avalokita_hosted_attempts SET state='accepted',native_turn_id=?,updated_at=? WHERE job_id=?", (turn, time.time(), job["id"]))

    def finish(self, job_id, state):
        self.guard()
        with self.store.connection() as db:
            db.execute("UPDATE avalokita_hosted_attempts SET state=?,updated_at=? WHERE job_id=?", (state, time.time(), job_id))
            db.execute("UPDATE avalokita_hosted_approvals SET state='expired' WHERE job_id=? AND state='pending'", (job_id,))

    def complete(self, job, generation, result):
        """Commit an observed result and its exact ownership in one transaction."""
        self.guard()
        if not isinstance(result.text, str) or len(result.text.encode("utf-8")) > 65536:
            raise BusError("Native selected result exceeds the public message limit", 400)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=?", (job["id"],)).fetchone()
            owner = db.execute("SELECT * FROM avalokita_hosted_owners WHERE actor=?", (job["agent_id"],)).fetchone()
            actor = db.execute("SELECT * FROM agents WHERE id=?", (job["agent_id"],)).fetchone()
            current = db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if (not attempt or not owner or not actor or not current or not is_hosted(dict(actor))
                    or attempt["generation"] != generation or owner["generation"] != generation or actor["generation"] != generation
                    or actor["provider"] != owner["provider"] or actor["native_address"] != owner["owner_id"] or owner["owner_id"] != attempt["owner_id"]
                    or not result.session_id or result.session_id != owner["native_session_id"] or actor["native_session_id"] != owner["native_session_id"]
                    or attempt["native_session_id"] != result.session_id or not attempt["input_attempted"]
                    or current["event_id"] != job["event_id"] or current["root_id"] != job["root_id"] or current["agent_id"] != job["agent_id"]
                    or actor["active_root"] != job["root_id"] or current["state"] not in {"running", "awaiting_native_reply", "waiting_user"}):
                raise BusError("Native completion no longer owns this exact input; identity was preserved", 409)
            now = time.time()
            db.execute("UPDATE avalokita_hosted_attempts SET state='completed',updated_at=? WHERE job_id=?", (now, job["id"]))
            db.execute("UPDATE avalokita_hosted_approvals SET state='expired' WHERE job_id=? AND state='pending'", (job["id"],))
            if current["state"] == "waiting_user":
                return
            db.execute("UPDATE jobs SET state='completed',finished_at=?,error=NULL WHERE id=?", (now, job["id"]))
            db.execute("UPDATE agents SET state=CASE WHEN state IN ('paused','stopped') THEN state ELSE 'idle' END,active_root=NULL,error=NULL,channel_verified=1,updated_at=? WHERE id=?", (now, job["agent_id"]))
            if result.text:
                self.store._insert_event(db, "result", job["agent_id"], "user", result.text, job["root_id"],
                    {"job_id": job["id"], "event_id": job["event_id"], "state": "completed", "native_turn_id": attempt["native_turn_id"]})

    def attention(self, job, generation, reason):
        self.guard()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            attempt = db.execute("SELECT * FROM avalokita_hosted_attempts WHERE job_id=? AND generation=?", (job["id"], generation)).fetchone()
            if not attempt or attempt["state"] == "completed":
                return
            uncertain = bool(attempt["input_attempted"])
            db.execute("UPDATE avalokita_hosted_attempts SET state=?,updated_at=? WHERE job_id=?", ("delivery_unknown" if uncertain else "not_sent", time.time(), job["id"]))
            db.execute("UPDATE avalokita_hosted_approvals SET state='expired' WHERE job_id=? AND state='pending'", (job["id"],))
            db.execute("UPDATE jobs SET state=?,error=? WHERE id=? AND event_id=? AND root_id=? AND agent_id=? AND state IN ('running','awaiting_native_reply')",
                ("awaiting_native_reply" if uncertain else "needs_human", reason, job["id"], job["event_id"], job["root_id"], job["agent_id"]))
            owner = db.execute("SELECT * FROM avalokita_hosted_owners WHERE actor=?", (job["agent_id"],)).fetchone()
            if owner:
                db.execute("UPDATE agents SET state=CASE WHEN state IN ('paused','stopped') THEN state ELSE 'needs_human' END,error=?,updated_at=? WHERE id=? AND native_address=? AND provider=? AND generation=? AND native_session_id IS ? AND active_root=?",
                    (reason, time.time(), job["agent_id"], attempt["owner_id"], owner["provider"], generation, owner["native_session_id"], job["root_id"]))

    def process(self, actor, pid):
        self.guard()
        with self.store.connection() as db:
            db.execute("UPDATE avalokita_hosted_owners SET pid=? WHERE actor=?", (pid, actor))

    def approval(self, agent_id, job, generation, request):
        self.guard()
        # The native adapter already selected these public fields. No raw params.
        allowed = {"approval_id", "provider", "method", "summary", "questions", "session_id", "turn_id"}
        selected = {key: value for key, value in request.items() if key in allowed}
        details = request.get("details", {})
        if isinstance(details, dict):
            selected["summary"] = str(details.get("summary", "This exact native request requires human review."))[:2000]
            selected["questions"] = details.get("questions", [])
            public = {key: value for key, value in details.items() if key in {"title", "summary", "questions", "command", "cwd", "paths"}}
            raw = json.dumps(public, ensure_ascii=False)
            secret = re.search(r"(?i)(api[_-]?key|access[_-]?token|password|secret|authorization|bearer\s|sk-[a-z0-9]{8,})", raw)
            selected["can_approve"] = not bool(secret) and len(raw) <= 14000 and bool(public.get("questions") or public.get("command") or public.get("paths"))
            if secret or len(raw) > 14000:
                public = {"summary": "Native request details require review in the original client; sensitive or oversized content was omitted."}
                selected.update(summary=public["summary"], questions=[], reason="native_approval_details_redacted")
            for field in ("cwd", "paths"):
                paths = [public[field]] if field == "cwd" and public.get(field) else public.get(field, [])
                if not isinstance(paths, list):
                    selected["can_approve"] = False
                    continue
                for value in paths:
                    try:
                        path = Path(value)
                        path = path if path.is_absolute() else self.store.workspace / path
                        if os.path.commonpath([self.store.workspace, path.absolute()]) != str(self.store.workspace) or ".." in path.parts:
                            selected["can_approve"] = False
                            selected["reason"] = "native_approval_scope_requires_original_client"
                    except (TypeError, ValueError):
                        selected["can_approve"] = False
            selected["details"] = public
        if not isinstance(selected.get("approval_id"), str) or len(json.dumps(selected)) > 16384:
            raise BusError("Invalid bounded native approval", 400)
        self.event(agent_id, job, generation, {"type": "status", "session_id": selected.get("session_id")})
        selected.update(root_id=job["root_id"], native_turn_id=selected.pop("turn_id", None))
        with self.store.connection() as db:
            db.execute("INSERT INTO avalokita_hosted_approvals VALUES (?,?,?,?,?,'pending',NULL)", (selected["approval_id"], agent_id, job["id"], generation, json.dumps(selected)))
            db.execute("UPDATE agents SET state='waiting_for_user',updated_at=? WHERE id=? AND active_root=?", (time.time(), agent_id, job["root_id"]))

    def reply(self, agent_id, approval_id, result, adapter):
        self.guard()
        canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
        if not isinstance(result, dict) or len(canonical) > 16384:
            raise BusError("Invalid bounded native approval response")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM avalokita_hosted_approvals WHERE approval_id=? AND actor=?", (approval_id, agent_id)).fetchone()
            if not row:
                raise BusError("Exact pending hosted approval not found", 404)
            if row["state"] == "answered" and row["result"] == canonical:
                return {"accepted": True, "duplicate_suppressed": True}
            selected = json.loads(row["request"])
            denied = result.get("decision") in {"decline", "cancel"} or result.get("behavior") == "deny" or result.get("action") in {"decline", "cancel"}
            if not selected.get("can_approve") and not denied:
                raise BusError("Native approval details are incomplete; use the original client", 409)
            job = db.execute("SELECT j.*,r.deadline FROM jobs j JOIN roots r ON r.id=j.root_id WHERE j.id=?", (row["job_id"],)).fetchone()
            agent = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            flags = dict(db.execute("SELECT key,value FROM config"))
            if (row["state"] != "pending" or not job or not agent or job["state"] not in {"running", "awaiting_native_reply"}
                    or job["deadline"] <= time.time() or agent["active_root"] != job["root_id"] or agent["generation"] != row["generation"]
                    or flags.get("paused") == "true" or flags.get("auto_suspended") == "true" or agent["state"] in {"paused", "stopped"} or adapter is None):
                raise BusError("Approval no longer owns an active unpaused native input", 409)
            # Mark before native I/O. An uncertain response is never replayed.
            db.execute("UPDATE avalokita_hosted_approvals SET state='sending',result=? WHERE approval_id=?", (canonical, approval_id))
        try:
            adapter.respond_approval(approval_id, result)
        except ValueError:
            # NativeAdapter validates before enqueueing or writing a response.
            # A rejected shape is not an uncertain native write.
            with self.store.connection() as db:
                db.execute("UPDATE avalokita_hosted_approvals SET state='pending',result=NULL WHERE approval_id=? AND state='sending'", (approval_id,))
            raise BusError("Native approval response is invalid or no longer active", 409) from None
        with self.store.connection() as db:
            db.execute("UPDATE avalokita_hosted_approvals SET state='answered' WHERE approval_id=? AND state='sending'", (approval_id,))
            db.execute("UPDATE agents SET state='running',updated_at=? WHERE id=? AND state='waiting_for_user'", (time.time(), agent_id))
        return {"accepted": True, "duplicate_suppressed": False}
