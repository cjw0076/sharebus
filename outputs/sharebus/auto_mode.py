"""One activation for existing native conversations; no managed workers."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from sharebus import Client
from store import BusStore


NATIVE_PROVIDERS = {"codex", "claude", "antigravity"}
INVITE_STATES = {"dispatching", "delivered", "uncertain", "failed"}


def _install(workspace: Path, home: Path | None, python: Path | None,
             watch_timeout: float | None = None, codex_native_queue: bool | None = None) -> dict[str, Any]:
    from install import install_auto
    return install_auto(workspace, home=home, python=python, apply_changes=True, replace=True,
                        watch_timeout=watch_timeout, codex_native_queue=codex_native_queue)


def _discover(workspace: Path) -> list[dict[str, Any]]:
    from discovery import discover
    return discover(workspace)


def _probe_codex_queue(workspace: Path) -> dict[str, Any]:
    try:
        from codex_bridge import probe_native_queue
        return probe_native_queue(workspace)
    except Exception:
        return {"supported": False, "reason": "unavailable"}


def _connect_origin(workspace: Path) -> dict[str, Any] | None:
    from auto_enroll import bootstrap_native_environment, connect_context
    context = bootstrap_native_environment(workspace)
    if context is None:
        return None
    binding = connect_context(context, "codex", workspace, request_timeout=3, start_daemon=False)
    if binding.get("provider") != "codex" or not _same_workspace(binding.get("workspace"), workspace):
        raise RuntimeError("Automatic native origin does not match this workspace")
    return {"connected": True, "evidence": "native_environment",
            **{key: binding[key] for key in ("agent_id", "provider", "native_session_id", "workspace")}}


def _same_workspace(value: Any, workspace: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return os.path.normcase(str(Path(value).resolve(strict=True))) == os.path.normcase(str(workspace))
    except (OSError, ValueError):
        return False


def _candidates(rows: list[dict[str, Any]], workspace: Path) -> list[dict[str, str]]:
    """Keep only public exact-workspace identities, never transcripts or titles."""
    result: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        if not isinstance(provider, str) or provider not in NATIVE_PROVIDERS:
            continue
        sessions = row.get("native_sessions", [row])
        if not isinstance(sessions, list):
            continue
        for session in sessions:
            if not isinstance(session, dict):
                continue
            native_id = session.get("native_session_id")
            if (not isinstance(native_id, str) or not 1 <= len(native_id) <= 256
                    or any(ord(char) < 32 for char in native_id)
                    or not _same_workspace(session.get("workspace", session.get("cwd")), workspace)):
                continue
            result[(provider, native_id)] = {"provider": provider, "native_session_id": native_id,
                                             "workspace": str(workspace)}
    return [result[key] for key in sorted(result)]


def _invite_table(store: BusStore) -> None:
    with store.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS auto_invite_plans (
            key TEXT PRIMARY KEY,epoch REAL NOT NULL,provider TEXT NOT NULL,
            native_session_id TEXT NOT NULL,state TEXT NOT NULL,updated_at REAL NOT NULL,
            UNIQUE(epoch,provider,native_session_id))""")


def invitation_plan(store: BusStore, candidates: list[dict[str, Any]], *,
                    origin_provider: str | None = None, origin_native_id: str | None = None) -> list[dict[str, Any]]:
    """A dispatch claim is persisted before an app sends, so uncertainty is not retried."""
    _invite_table(store)
    auto = store.auto_state()
    if not auto["active"]:
        return []
    epoch = float(auto.get("epoch", auto["updated_at"]))
    agents = {(agent["provider"], agent["native_session_id"]): agent for agent in store.agents()
              if agent.get("native_session_id")}
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for candidate in _candidates(candidates, store.workspace):
            identity = (candidate["provider"], candidate["native_session_id"])
            agent = agents.get(identity)
            if identity == (origin_provider, origin_native_id):
                state = "origin"
            elif agent and agent["state"] in {"paused", "stopped", "needs_human"}:
                state = "blocked"
            elif agent:
                state = "registered"
            else:
                state = "pending"
            key = hashlib.sha256(json.dumps([store.workspace_id, epoch, *identity], separators=(",", ":")).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO auto_invite_plans VALUES (?,?,?,?,?,?)",
                       (key, epoch, *identity, state, time.time()))
            # An actual registration can close an invite, but cannot turn an
            # uncertain send back into a pending invitation.
            if state in {"origin", "registered", "blocked"}:
                db.execute("UPDATE auto_invite_plans SET state=?,updated_at=? WHERE key=?",
                           (state, time.time(), key))
        rows = [dict(row) for row in db.execute(
            "SELECT key,epoch,provider,native_session_id,state FROM auto_invite_plans WHERE epoch=? ORDER BY provider,native_session_id",
            (epoch,))]
    for row in rows:
        row["workspace"] = str(store.workspace)
        row["via"] = {"codex": "codex_app_native", "claude": "claude_native",
                      "antigravity": "antigravity_sidecar"}[row["provider"]]
    return rows


def mark_invitation(store: BusStore, key: str, state: str) -> dict[str, Any]:
    if state not in INVITE_STATES or not isinstance(key, str) or len(key) != 64:
        raise RuntimeError("Use an existing invite key and dispatching, delivered, uncertain, or failed")
    _invite_table(store)
    auto = store.auto_state()
    if state == "dispatching" and not auto["active"]:
        raise RuntimeError("Automatic participation is disabled, suspended, or paused")
    epoch = float(auto.get("epoch", auto["updated_at"]))
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM auto_invite_plans WHERE key=? AND epoch=?", (key, epoch)).fetchone()
        if row is None:
            raise RuntimeError("Invitation does not belong to this active automatic-mode epoch")
        claimed = state == "dispatching" and row["state"] == "pending"
        if claimed or (state != "dispatching" and row["state"] == "dispatching"):
            db.execute("UPDATE auto_invite_plans SET state=?,updated_at=? WHERE key=?", (state, time.time(), key))
            current = state
        else:
            current = row["state"]
    return {"key": key, "state": current, "claimed": claimed}


def _running(client: Client) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not (client.private / "runtime.json").exists():
        return None, None
    try:
        runtime = client.runtime(verify=False)
    except RuntimeError:
        if os.name == "nt":
            return None, None
        raise
    try:
        return runtime, client.request("/api/state")
    except RuntimeError:
        if os.name == "nt":
            # A stored PID may now belong to an unrelated app. Only the normal
            # scheduler path may validate/restart this workspace's owned task.
            return None, None
        from daemon import _pid_exists
        pid = runtime.get("pid")
        if not isinstance(pid, int) or pid <= 0 or _pid_exists(pid):
            raise RuntimeError("Existing workspace broker is unreachable; no replacement was launched") from None
        return None, None


def _upgrade(client: Client, store: BusStore, runtime: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if isinstance(state.get("auto"), dict) and state["auto"].get("codex_delivery") in {"stop_hook", "native_queue"}:
        return state
    if any(job.get("state") in {"running", "awaiting_native_reply", "waiting_user"}
           for job in state.get("jobs", []) + state.get("active_jobs", [])):
        raise RuntimeError("Finish or review active native work before upgrading this workspace broker")
    if any(agent.get("state") in {"running", "awaiting_native_reply", "waiting_user", "waiting_for_user"}
           for agent in state.get("agents", [])):
        raise RuntimeError("Finish or review active native work before upgrading this workspace broker")
    current = client.runtime()
    room = state.get("workspace") or {}
    if (any(current.get(key) != runtime.get(key) for key in ("workspace", "workspace_id", "url", "pid", "nonce"))
            or current.get("workspace_id") != store.workspace_id
            or not _same_workspace(current.get("workspace"), store.workspace)
            or room.get("id") != store.workspace_id or not _same_workspace(room.get("path"), store.workspace)
            or type(current.get("pid")) is not int or current["pid"] <= 0
            or not isinstance(current.get("nonce"), str) or len(current["nonce"]) < 16):
        raise RuntimeError("Workspace broker identity changed; no stop or replacement was attempted")
    paused, suspended = bool(state.get("paused")), bool(store.auto_state()["suspended"])
    # This is one verified operator request. An uncertain POST is never retried.
    client.request("/api/control", {"action": "stop"})
    from daemon import _pid_exists
    deadline = time.monotonic() + 35
    while _pid_exists(int(runtime["pid"])):
        if time.monotonic() >= deadline:
            raise RuntimeError("Broker shutdown is still pending; no replacement or force stop was attempted")
        time.sleep(0.1)
    client.start("none")
    upgraded = client.request("/api/state")
    if (not isinstance(upgraded.get("auto"), dict)
            or upgraded["auto"].get("codex_delivery") not in {"stop_hook", "native_queue"}):
        raise RuntimeError("Restarted workspace broker does not expose current automatic-delivery support")
    if store.auto_state()["suspended"] != suspended:
        store.configure_auto(suspended=suspended)
    if not paused and upgraded.get("paused"):
        client.request("/api/control", {"action": "resume"})
        upgraded = client.request("/api/state")
    return upgraded


def status(workspace: Path) -> dict[str, Any]:
    """Reading an unconfigured directory must not create a bus or credentials."""
    workspace = workspace.resolve(strict=True)
    private = workspace / ".sharebus/private"
    default = {"enabled": False, "suspended": False, "paused": False, "active": False,
               "updated_at": 0, "epoch": 0, "codex_delivery": "stop_hook", "codex_delivery_configured": False,
               "registered_count": 0, "observations": []}
    result: dict[str, Any] = {"workspace": str(workspace), "running": False, "auto": default,
                              "pending_invites": [], "managed_workers_started": 0}
    database = private / "bus.sqlite3"
    if not database.exists():
        return result
    for entry in (workspace / ".sharebus", private, database):
        if entry.is_symlink() or BusStore._reparse(entry) or (entry.is_file() and entry.stat().st_nlink != 1):
            raise RuntimeError("Automatic-mode status cannot read linked workspace storage")
    # The unavailable/disabled status path does not initialize/migrate SQLite.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        values = dict(db.execute("SELECT key,value FROM config WHERE key IN ('auto_enabled','auto_suspended','auto_updated_at','auto_epoch','auto_codex_delivery','paused')"))
        auto = {"enabled": values.get("auto_enabled") == "true", "suspended": values.get("auto_suspended") == "true",
                "paused": values.get("paused") == "true", "updated_at": float(values.get("auto_updated_at", "0")),
                "epoch": float(values.get("auto_epoch", values.get("auto_updated_at", "0")))}
        auto["active"] = auto["enabled"] and not auto["suspended"] and not auto["paused"]
        configured_delivery = values.get("auto_codex_delivery") in {"stop_hook", "native_queue"}
        auto["codex_delivery"] = values["auto_codex_delivery"] if configured_delivery else "stop_hook"
        auto["codex_delivery_configured"] = configured_delivery
        result["auto"] = {**default, **auto}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_invite_plans'").fetchone():
            invitations = [dict(row) for row in db.execute(
                "SELECT key,epoch,provider,native_session_id,state FROM auto_invite_plans WHERE epoch=? ORDER BY provider,native_session_id",
                (auto["epoch"],))]
            for row in invitations:
                row["workspace"] = str(workspace)
            result["invitations"] = invitations
            if auto["active"]:
                result["pending_invites"] = [row for row in invitations if row["state"] == "pending"]
    client = Client(workspace)
    runtime, state = _running(client)
    if runtime and state:
        result.update({"running": True, "url": runtime["url"], "auto": state.get("auto", result["auto"]),
                       "agents": state.get("agents", []), "discovery_state": state.get("discovery_state", "unknown")})
    return result


def configure(workspace: str | Path, action: str = "on", *, open_console: bool = True,
              candidates: list[dict[str, Any]] | None = None, home: Path | None = None,
              python: Path | None = None, origin_provider: str | None = None,
              origin_native_id: str | None = None, invite_key: str | None = None,
              invite_state: str | None = None, watch_timeout: float | None = None) -> dict[str, Any]:
    if watch_timeout is not None:
        from auto_watch import MAX_TIMEOUT
        if (action != "on" or type(watch_timeout) not in (int, float)
                or not math.isfinite(watch_timeout) or not 0 < watch_timeout <= MAX_TIMEOUT):
            raise RuntimeError("Use auto on with a finite watch timeout greater than zero and at most 7 days")
    workspace = Path(workspace).resolve(strict=True)
    if action == "status":
        return status(workspace)
    if action not in {"on", "off", "resume", "invite"}:
        raise RuntimeError("Unknown automatic-mode action")
    if action == "off" and not (workspace / ".sharebus/private/bus.sqlite3").exists():
        return status(workspace)
    if action == "invite" and not (workspace / ".sharebus/private/bus.sqlite3").exists():
        raise RuntimeError("No invitation plan exists in this workspace")
    store = BusStore(workspace)
    if action == "invite":
        return mark_invitation(store, invite_key or "", invite_state or "")
    if action == "off":
        return {"workspace": str(workspace), "auto": store.configure_auto(enabled=False),
                "managed_workers_started": 0, "pending_invites": []}
    before = store.auto_state()
    if action == "resume" and not before["enabled"]:
        raise RuntimeError("Automatic mode is disabled; enable it with auto first")
    receipt = None
    if action == "on":
        delivery = before.get("codex_delivery") if before.get("codex_delivery_configured") else None
        if delivery is None and _probe_codex_queue(workspace).get("supported") is True:
            delivery = "native_queue"
        receipt = _install(workspace, home, python, watch_timeout=watch_timeout,
                           codex_native_queue=None if delivery is None else delivery == "native_queue")
        if delivery is None and receipt.get("codex_native_queue") is True:
            delivery = "native_queue"
        if delivery is not None:
            store.configure_auto(codex_delivery=delivery)
    if action == "on" and not before["enabled"]:
        store.configure_auto(enabled=True)
    elif action == "resume" and before["suspended"]:
        store.configure_auto(suspended=False)
    client = Client(workspace)
    runtime, state = _running(client)
    auto = store.auto_state()
    if action == "resume":
        if runtime and state:
            state = _upgrade(client, store, runtime, state)
        else:
            client.start("none")
        client.request("/api/control", {"action": "resume"})
    elif not auto["suspended"] and not auto["paused"]:
        if runtime and state:
            _upgrade(client, store, runtime, state)
        else:
            client.start("none")
    origin = None
    if store.auto_state()["active"]:
        try:
            origin = _connect_origin(workspace)
        except (OSError, RuntimeError, ValueError):
            origin = {"connected": False, "status": "pending"}
    result = status(workspace)
    result["installation"] = receipt
    result["native_origin"] = origin or {"connected": False, "status": "unavailable"}
    if origin and origin.get("connected"):
        origin_provider, origin_native_id = origin["provider"], origin["native_session_id"]
    if result["auto"]["active"]:
        rows = candidates if candidates is not None else (state.get("discovery", []) if state else [])
        if candidates is None and not rows:
            rows = _discover(workspace)
        invitations = invitation_plan(store, rows, origin_provider=origin_provider, origin_native_id=origin_native_id)
        result["invitations"] = invitations
        result["pending_invites"] = [row for row in invitations if row["state"] == "pending"]
    if open_console and result["running"] and (action == "resume" or not before["enabled"] or runtime is None):
        client.open()
        result["console_opened"] = True
    else:
        result["console_opened"] = False
    result["native_messages_sent"] = 0
    result["native_apps_started"] = 0
    return result


def load_candidates(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError("Native candidate metadata is too large")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list) or len(data) > 1000:
        raise RuntimeError("Native candidates must be an array of at most 1000 public metadata rows")
    return data
