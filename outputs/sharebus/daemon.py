"""Authenticated loopback HTTP broker and optional human workspace UI."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import stat
import threading
import time
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from store import BusError, BusStore
from supervisor import Supervisor
from hosted_runtime import is_hosted


class BusHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, workspace: str | Path, *, providers: str = "none", port: int = 0,
                 enable_native: bool = True, discovery: list[dict[str, Any]] | None = None,
                 adapter_factory: Any = None, hosted_only: bool = False, store: BusStore | None = None,
                 acquire_work=None):
        self.store = store if store is not None else BusStore(workspace)
        self.store.guard_workspace()
        if store is not None and self.store.workspace != Path(workspace).absolute():
            raise BusError("Owned store belongs to another workspace", 403)
        if not hosted_only:
            self.store.recover()
        self.discovery = discovery if discovery is not None else []
        self.discovery_state = "ready" if discovery is not None else "pending"
        self._closed = threading.Event()
        self.serving = threading.Event()
        self.avalokita_peer = None
        self.acquire_work = acquire_work
        self.supervisor = Supervisor(self.store, self.discovery, adapter_factory, hosted_only=hosted_only,
            hosted_execution_ready=lambda: not self._closed.is_set() and callable(self.avalokita_peer),
            acquire_work=acquire_work)
        if hosted_only:
            self.supervisor.hosted.recover()
        if providers != "none" and discovery is not None:
            self.supervisor.bootstrap(providers)
        self.native_enabled = enable_native
        self.stopping = False
        self.nonce = secrets.token_hex(16)
        self.cookie_name = "sharebus_" + self.store.workspace_id.replace("-", "")[:12]
        self.web = Path(__file__).parent / "web"
        super().__init__(("127.0.0.1", port), BusHandler)
        self.url = f"http://127.0.0.1:{self.server_port}"
        try:
            if enable_native:
                self.supervisor.start()
            if discovery is None:
                threading.Thread(target=self._discover, args=(providers,), name="sharebus-discovery", daemon=True).start()
        except BaseException:
            self.close()
            raise

    def serve_forever(self, poll_interval=0.5):
        self.serving.set()
        try:
            return super().serve_forever(poll_interval=poll_interval)
        finally:
            self.serving.clear()

    def _discover(self, providers: str) -> None:
        try:
            _startup_log(self.store.workspace, "discovery_begin", store=self.store)
            from discovery import discover
            found = discover(self.store.workspace)
            if self._closed.is_set():
                return
            self.store.guard_workspace()
            self.discovery[:] = found
            if providers != "none":
                self.supervisor.bootstrap(providers)
                self.supervisor.wake()
            self.discovery_state = "ready"
            _startup_log(self.store.workspace, "discovery_ready", store=self.store)
        except Exception:
            self.discovery_state = "failed"
            try:
                _startup_log(self.store.workspace, "discovery_failed", store=self.store)
            except BusError:
                pass

    def runtime(self) -> dict[str, Any]:
        self.store.guard_workspace()
        return {"version": 1, "workspace": str(self.store.workspace), "workspace_id": self.store.workspace_id,
                "url": self.url, "pid": os.getpid(), "nonce": self.nonce, "started_at": time.time()}

    def state(self, actor: str = "user") -> dict[str, Any]:
        agents = self.store.agents()
        jobs = self.store.jobs()
        hosted = self.supervisor.hosted_state()
        owned = {item["agent_id"]: item for item in hosted["sessions"]}
        for agent in agents:
            managed = agent["mode"].startswith("managed")
            native = agent["mode"] == "native_attached"
            agent["wake_supported"] = (managed or (native and agent["channel_verified"])) and self.native_enabled
            agent["wake_channel_available"] = (managed or native) and self.native_enabled
            agent["channel"] = "native_cli" if managed else f"{agent['provider']}_native_bridge" if native else "mcp_poll"
            agent["pending_jobs"] = sum(job["agent_id"] == agent["id"] and job["state"] == "queued" for job in jobs)
            agent["tool_scope"] = "native_cli_session" if managed else "attached_app_session"
            agent["attachment_status"] = "managed" if managed else "native_delivery_verified" if native and agent["channel_verified"] else "native_bridge_pending_verification" if native else "registered_polling"
            if is_hosted(agent):
                session = owned.get(agent["id"], {})
                agent.update(transport="hosted_cli", hosted_enabled=session.get("enabled", False) and callable(self.avalokita_peer),
                             hosted_blocked=session.get("blocked", True), hosted_owner_verified=bool(session and session.get("owner_id") == agent["native_address"] and session.get("native_session_id") == agent["native_session_id"]),
                             identity_state=session.get("identity_state", "pending"), native_turn_id=session.get("native_turn_id"),
                             delivery_state=session.get("delivery_state", "not_sent"), attachment_status="hosted_cli",
                             desktop_capabilities_verified=False)
        return {"workspace": {"path": str(self.store.workspace), "id": self.store.workspace_id},
                "paused": self.store.paused(), "agents": agents, "discovery": self.discovery, "jobs": jobs,
                "discovery_state": self.discovery_state,
                "auto": self.store.auto_state(),
                "active_jobs": self.store.active_jobs(),
                "own_agent": next((agent for agent in agents if agent["id"] == actor), None),
                "boundaries": {"same_os_user_isolation": False, "external_gui_autowake_verified": False}}

    def close(self) -> None:
        self.stopping = True
        self._closed.set()
        try:
            self.supervisor.close()
        finally:
            self.server_close()


class BusHandler(BaseHTTPRequestHandler):
    server: BusHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Request paths, bodies, capabilities, and native output are not logged.
        return

    def _headers(self, status: int, content_type: str, length: int, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, data: Any, status: int = 200, extra: dict[str, str] | None = None) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(payload), extra)
        self.wfile.write(payload)

    def _check_host(self) -> None:
        if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            raise BusError("Unrecognized loopback Host", 403)

    def _actor(self) -> str:
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return self.server.store.authenticate(authorization[7:])
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
            value = cookies.get(self.server.cookie_name)
            return self.server.store.authenticate(value.value if value else "")
        except Exception:
            raise BusError("Authentication required", 401) from None

    @staticmethod
    def _operator(actor: str) -> None:
        if actor != "user":
            raise BusError("Human operator capability required", 403)

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise BusError("Transfer encoding is unsupported", 400)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise BusError("Invalid request size") from None
        if not 0 < length <= 2 * 1024 * 1024:
            raise BusError("Request must be 1 byte to 2 MiB", 413)
        self.connection.settimeout(10)
        raw = self.rfile.read(length)
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {self.server.url, f"http://localhost:{self.server.server_port}"}:
            raise BusError("Cross-origin mutation rejected", 403)
        if self.headers.get("X-Sharebus-Request") != "1":
            raise BusError("Sharebus request header required", 403)
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise BusError("Content-Type application/json required", 415)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise BusError("Invalid UTF-8 JSON") from None
        if not isinstance(body, dict):
            raise BusError("JSON object required")
        return body

    def _static(self, path: str) -> None:
        if path in {"/", "/index.html"}:
            source = self.server.web / "index.html"
        elif path.startswith("/assets/"):
            parts = unquote(path)[1:].split("/")
            if any(part in {"", ".", ".."} or ":" in part or "\\" in part for part in parts):
                raise BusError("Invalid static path", 404)
            source = self.server.web.joinpath(*parts)
        else:
            raise BusError("Not found", 404)
        root = self.server.web.absolute()
        source = source.absolute()
        if not source.resolve().is_relative_to(root.resolve()):
            raise BusError("Invalid static path", 404)
        current = root
        for part in ("", *source.relative_to(root).parts):
            if part:
                current = current / part
            if current.is_symlink() or self.server.store._reparse(current):
                raise BusError("Invalid static path", 404)
        if not source.is_file():
            if path == "/":
                payload = b"<!doctype html><title>Sharebus</title><p>Sharebus is running. Human UI is being installed.</p>"
            else:
                raise BusError("Not found", 404)
        else:
            payload = source.read_bytes()
        media = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        self._headers(200, media, len(payload))
        self.wfile.write(payload)

    def do_GET(self) -> None:
        try:
            self._check_host()
            self.server.store.guard_workspace()
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._json({"ok": not self.server.stopping, "stopping": self.server.stopping, "workspace_id": self.server.store.workspace_id, "pid": os.getpid(), "nonce": self.server.nonce}, 503 if self.server.stopping else 200)
                return
            if not parsed.path.startswith("/api/"):
                self._static(parsed.path)
                return
            actor = self._actor()
            if parsed.path == "/api/state":
                self._json(self.server.state(actor))
            elif parsed.path == "/api/hosted":
                self._operator(actor)
                if not callable(self.server.avalokita_peer):
                    raise BusError("Avalokita hosted peer integration is unavailable in this daemon", 409)
                self._json(self.server.supervisor.hosted_state())
            elif parsed.path == "/api/avalokita/context":
                if actor == "user" or not callable(self.server.avalokita_peer):
                    raise BusError("Owned native peer integration is unavailable", 409)
                self._json(self.server.supervisor.hosted_context(actor))
            elif parsed.path == "/api/policy":
                from policy import read_policy
                self.server.store.guard_workspace()
                self._json(read_policy(self.server.store.workspace, workspace_guard=self.server.store.guard_workspace))
            elif parsed.path == "/api/agent/pending":
                if actor == "user":
                    raise BusError("Agent capability required", 403)
                self._json(self.server.store.auto_pending(actor))
            elif parsed.path == "/api/events":
                try:
                    after = int(parse_qs(parsed.query).get("after", ["0"])[0])
                except ValueError:
                    raise BusError("Invalid event cursor") from None
                self._json(self.server.store.events(after))
            elif parsed.path == "/api/artifacts":
                artifacts = self.server.store.artifacts()
                for artifact in artifacts:
                    artifact["name"] = artifact["filename"]
                    artifact["download_url"] = "/api/artifacts/" + artifact["id"]
                self._json({"artifacts": artifacts})
            elif parsed.path.startswith("/api/artifacts/"):
                artifact_id = parsed.path.removeprefix("/api/artifacts/")
                record, source = self.server.store.artifact_file(artifact_id)
                self.server.store.guard_workspace()
                payload = source.read_bytes()
                self._headers(200, "application/octet-stream", len(payload), {"Content-Disposition": f"attachment; filename=\"sharebus-{record['id']}\""})
                self.wfile.write(payload)
            else:
                raise BusError("Not found", 404)
        except BusError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self._json({"error": "Internal broker error"}, 500)

    def do_POST(self) -> None:
        release = None
        try:
            self._check_host()
            self.server.store.guard_workspace()
            body = self._body()
            self.server.store.guard_workspace()
            path = urlsplit(self.path).path
            if path == "/api/auth":
                actor = self.server.store.authenticate(str(body.get("token", "")))
                self._operator(actor)
                cookie = f"{self.server.cookie_name}={self.server.store.operator_token}; Path=/; HttpOnly; SameSite=Strict"
                self._json({"ok": True}, extra={"Set-Cookie": cookie})
                return
            if path == "/api/auto/redeem":
                self._json(self.server.store.redeem_auto(body))
                return
            actor = self._actor()
            if path in {"/api/hosted", "/api/hosted/approval"} and self.server.acquire_work is not None:
                release = self.server.acquire_work()
                if release is None:
                    raise BusError("Owned workspace service is closing", 409)
            if path == "/api/message":
                if self.server.discovery_state == "pending" and not self.server.store.agents() and body.get("to", "all") != "user":
                    raise BusError("Agent discovery is still running; retry this input after participants are ready", 409)
                kwargs = {"budget": body.get("budget", 24), "lifetime": body.get("deadline_seconds", 3600)} if actor == "user" else {}
                kind = body.get("kind", "message")
                if kind == "human":
                    self._operator(actor)
                    kind = "message"
                own = self.server.store.agent(actor) if actor != "user" else None
                if own and is_hosted(own):
                    if not callable(self.server.avalokita_peer) or body.get("to", "all") == "all":
                        raise BusError("Use sharebus_peer publish for the public board, or one exact directed recipient", 409)
                    context = self.server.supervisor.hosted_context(actor)
                    key = hashlib.sha256(json.dumps([context["parent_job_id"], body.get("to"), kind, body.get("body")], ensure_ascii=False).encode()).hexdigest()
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    self.server.store.guard_workspace()
                    self._json(self.server.avalokita_peer(token, {**context, "operation": "send-keys", "recipient": body.get("to"),
                        "body": body.get("body", ""), "kind": kind, "idempotency_key": "hosted-mcp-" + key}))
                    return
                result = self.server.store.send(actor, body.get("to", "all"), body.get("body", ""), kind,
                                                in_reply_to=body.get("in_reply_to"), **kwargs)
                self.server.supervisor.wake()
                self._json(result)
            elif path == "/api/control":
                self._operator(actor)
                self._json(self._control(body))
            elif path == "/api/hosted":
                self._operator(actor)
                if set(body) != {"providers"} or not self.server.native_enabled:
                    raise BusError("Hosted native execution is unavailable or fields are invalid", 409)
                if not callable(self.server.avalokita_peer):
                    raise BusError("Avalokita hosted peer integration is unavailable in this daemon", 409)
                self._json(self.server.supervisor.configure_hosted(body["providers"]))
            elif path == "/api/hosted/approval":
                self._operator(actor)
                if set(body) != {"agent_id", "approval_id", "result"}:
                    raise BusError("Exact hosted approval fields required")
                self._json(self.server.supervisor.hosted_reply(body["agent_id"], body["approval_id"], body["result"]))
            elif path == "/api/avalokita/peer":
                if actor == "user" or not callable(self.server.avalokita_peer):
                    raise BusError("Owned native peer integration is unavailable", 409)
                context = self.server.supervisor.hosted_context(actor)
                if body.get("actor") != actor or body.get("root_id") != context["root_id"]:
                    raise BusError("Exact owned human root and actor required", 403)
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                self.server.store.guard_workspace()
                self._json(self.server.avalokita_peer(token, body))
            elif path == "/api/auto/config":
                self._operator(actor)
                if set(body) - {"enabled", "suspended", "codex_delivery"}:
                    raise BusError("Unknown automatic setting")
                self._json({"auto": self.server.store.configure_auto(body.get("enabled"), body.get("suspended"), body.get("codex_delivery"))})
            elif path == "/api/policy":
                self._operator(actor)
                from policy import PolicyConflict, save_team
                if set(body) != {"team", "expected_revision"}:
                    raise BusError("Policy updates require team and expected_revision")
                try:
                    self.server.store.guard_workspace()
                    result = save_team(self.server.store.workspace, body["team"], expected_revision=body["expected_revision"],
                                       workspace_guard=self.server.store.guard_workspace)
                except BusError:
                    raise
                except PolicyConflict:
                    raise BusError("Policy changed; reload before saving", 409) from None
                except ValueError:
                    raise BusError("Invalid team preferences; existing policy was preserved") from None
                self._json(result)
            elif path == "/api/auto/observe":
                if actor == "user":
                    raise BusError("Agent capability required", 403)
                self._json(self.server.store.observe_auto(actor, body.get("state"), body.get("note", "")))
            elif path == "/api/agent/register":
                self._json(self._register(actor, body))
            elif path == "/api/agent/recovery":
                self._operator(actor)
                self._json(self.server.store.create_recovery(str(body.get("agent_id", ""))))
            elif path == "/api/agent/heartbeat":
                if actor == "user":
                    raise BusError("Agent capability required", 403)
                agent = self.server.store.heartbeat(actor, body.get("state"), body.get("native_session_id"))
                if agent["provider"] == "codex" and agent["mode"] == "native_attached":
                    self.server.supervisor.wake()
                self._json({"agent": agent})
            elif path == "/api/agent/inbox":
                if actor == "user":
                    raise BusError("Agent capability required", 403)
                self._json(self.server.store.external_inbox(actor, int(body.get("after", 0)), body.get("ack", []), claim_work=body.get("claim", True)))
            elif path == "/api/native/input":
                self._operator(actor)
                native_id = body.get("native_session_id")
                matching = [agent for agent in self.server.store.agents() if agent["provider"] == body.get("provider") and agent["native_session_id"] == native_id and agent["mode"] in {"external_attached", "native_attached"}]
                if body.get("agent_id"):
                    matching = [agent for agent in matching if agent["id"] == body["agent_id"]]
                if len(matching) != 1 or not native_id:
                    raise BusError("Native input must identify one attached conversation in this workspace", 409)
                result = self.server.store.send("user", "all", body.get("body", ""), "task", native_origin=matching[0]["id"])
                self.server.supervisor.wake()
                self._json(result)
            elif path == "/api/native/result":
                if actor == "user":
                    raise BusError("Bound native agent capability required", 403)
                self._json(self.server.store.native_result(actor, body.get("native_session_id", ""), body.get("last_assistant_message", ""), body.get("job_id"), body.get("event_id"), body.get("requires_review_if_uncorrelated") is True))
            elif path in {"/api/artifact/publish", "/api/artifact/copy"}:
                if path.endswith("copy"):
                    artifact = self.server.store.publish(actor, _copy_id=body.get("artifact_id"), filename=body.get("filename"))
                else:
                    artifact = self.server.store.publish(actor, source=body.get("source"), text=body.get("text"), filename=body.get("filename"))
                self._json({"artifact": artifact})
            else:
                raise BusError("Not found", 404)
        except BusError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (TypeError, ValueError):
            self._json({"error": "Invalid request values"}, 400)
        except Exception:
            self._json({"error": "Internal broker error"}, 500)
        finally:
            if release is not None:
                release()

    def _register(self, actor: str, body: dict[str, Any]) -> dict[str, Any]:
        for field in ("workspace", "cwd"):
            if body.get(field) and Path(body[field]).resolve() != self.server.store.workspace:
                raise BusError("Native attachment belongs to another workspace", 403)
        if actor == "user":
            if body.get("mode", "external_attached") not in {"external", "external_attached"}:
                raise BusError("Use human start_agent control for managed sessions", 403)
            native_id = body.get("native_session_id")
            provider = str(body.get("provider", "other"))
            if body.get("recovery_code"):
                agent, token = self.server.store.redeem_recovery(provider, native_id, body["recovery_code"])
                return {"agent": agent, "token": token, "session_token": token, "recovered": True}
            existing = [item for item in self.server.store.agents() if native_id and item["provider"] == provider and item["native_session_id"] == native_id]
            if existing:
                raise BusError("Native conversation is already registered; rejoin using its original private session token", 409)
            agent, token = self.server.store.register(str(body.get("provider", "other")), body.get("label", ""),
                                                       body.get("capabilities", []), "external_attached", body.get("role", ""),
                                                       native_session_id=native_id or None, native_address=body.get("native_address") or None)
            return {"agent": agent, "token": token, "session_token": token}
        agent = self.server.store.agent(actor)
        if body.get("provider", agent["provider"]) != agent["provider"]:
            raise BusError("Bound provider cannot be changed", 403)
        updates: dict[str, Any] = {key: body[key] for key in ("label", "role", "capabilities", "native_address") if key in body}
        native_id = body.get("native_session_id")
        if native_id and native_id != agent["native_session_id"]:
            if agent["native_session_id"] or agent["mode"] not in {"external", "external_attached"}:
                raise BusError("Bound native conversation cannot be rebound", 403)
            updates["native_session_id"] = str(native_id)
        return {"agent": self.server.store.update_agent(actor, **updates)}

    def _control(self, body: dict[str, Any]) -> dict[str, Any]:
        action, target = body.get("action"), body.get("agent", "all")
        if action == "pause":
            if target == "all":
                self.server.store.set_paused(True)
                self.server.store.touch_auto_control()
            else:
                for agent in self.server.supervisor.select(target):
                    self.server.store.update_agent(agent["id"], state="paused")
                    self.server.store.touch_auto_control(agent["id"])
            self.server.supervisor.interrupt(target)
        elif action == "resume":
            if target == "all":
                self.server.store.set_paused(False)
                self.server.store.touch_auto_control()
            else:
                for agent in self.server.supervisor.select(target):
                    self.server.store.update_agent(agent["id"], state="idle", error=None)
                    self.server.store.touch_auto_control(agent["id"])
            self.server.supervisor.wake()
        elif action == "stop":
            if target == "all":
                self.server.store.configure_auto(suspended=True)
                self.server.stopping = True
                self.server.store.set_paused(True)
                self.server.supervisor.interrupt()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                for agent in self.server.supervisor.select(target):
                    self.server.store.update_agent(agent["id"], state="stopped")
                    self.server.store.touch_auto_control(agent["id"])
                self.server.supervisor.interrupt(target)
        elif action in {"start_agent", "restart_agent"}:
            provider = body.get("provider")
            if action == "start_agent" and provider:
                if provider not in {"codex", "claude", "antigravity"}:
                    raise BusError("No verified managed native adapter for this provider")
                native_id = body.get("native_session_id")
                if provider == "codex" and native_id:
                    existing = [item for item in self.server.store.agents() if item["provider"] == "codex" and item["native_session_id"] == native_id]
                    auto = self.server.store.auto_state()
                    if (body.get("mode") != "native_attached" or len(existing) != 1
                            or not auto["active"] or auto["codex_delivery"] != "native_queue"
                            or existing[0]["state"] != "idle"
                            or not self.server.store.auto_codex_attested(existing[0]["id"])):
                        raise BusError("Codex native queue requires this active workspace's proof-attested existing idle conversation", 409)
                    agent = self.server.store.reconcile_auto_codex(existing[0]["id"])
                    if agent["mode"] != "native_attached":
                        raise BusError("Finish or review active work before attaching the native queue", 409)
                    self.server.supervisor.wake()
                    return {"agent": agent, "state": self.server.state()}
                candidate = next((item for item in self.server.discovery if item.get("provider") == provider), {})
                if not candidate.get("installed") or not candidate.get("managed_supported"):
                    raise BusError("Provider CLI is not installed and verified", 409)
                if native_id:
                    if provider not in {"claude", "antigravity"} or body.get("mode") != "native_attached":
                        raise BusError("Existing GUI conversations must join as external_attached; concurrent CLI resume is not a verified attachment", 409)
                    existing = [item for item in self.server.store.agents() if item["provider"] == provider and item["native_session_id"] == native_id]
                    if provider == "claude":
                        from discovery import discover_native_sessions
                        targets = discover_native_sessions(provider, self.server.store.workspace)
                        if not any(item.get("native_session_id") == native_id for item in targets):
                            raise BusError("Exact existing native session is not discoverable in this workspace", 409)
                    elif not existing:
                        raise BusError("Antigravity must register its exact native conversation through its workspace sidecar first", 409)
                    if existing:
                        if len(existing) != 1 or existing[0]["mode"] not in {"external_attached", "native_attached"}:
                            raise BusError("Native conversation is already bound to another managed owner", 409)
                        agent = existing[0]
                        if agent["state"] in {"running", "awaiting_native_reply"}:
                            raise BusError("Finish or review the active native input before attaching a delivery bridge", 409)
                        updates: dict[str, Any] = {"mode": "native_attached", "channel_verified": False}
                        for key in ("label", "role", "native_address"):
                            if body.get(key):
                                updates[key] = body[key]
                        agent = self.server.store.update_agent(agent["id"], **updates)
                        self.server.supervisor.wake()
                        return {"agent": agent, "state": self.server.state()}
                agent, _ = self.server.store.register(provider, body.get("label", provider), body.get("capabilities", []), "native_attached" if native_id else "managed_new", body.get("role", ""),
                                                     native_session_id=native_id or None, native_address=body.get("native_address") or None)
                return {"agent": agent, "state": self.server.state()}
            self.server.supervisor.restart(target)
        else:
            raise BusError("Unknown control action")
        return {"ok": True, "state": self.server.state()}


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _daemon_path(store: BusStore, name: str, *, directory: bool = False) -> Path:
    store.guard_workspace()
    path = store.private / name
    if ".." in path.parts or not path.is_relative_to(store.private):
        raise BusError("Invalid daemon metadata path", 403)
    BusStore.reject_linked_path(path)
    store.guard_workspace()
    if path.exists():
        store.guard_workspace()
        valid_type = path.is_dir() if directory else path.is_file()
        if not valid_type:
            raise BusError("Daemon metadata must use regular nonlinked private paths", 403)
        if not directory:
            store.guard_workspace()
            if path.stat().st_nlink != 1:
                raise BusError("Daemon metadata must use regular nonlinked private paths", 403)
    store.guard_workspace()
    return path


@contextmanager
def workspace_guard(store: BusStore):
    """The OS lease owns the workspace; a PID or stale JSON file never does.

    Keep this inode permanently: unlinking a locked guard lets another process
    lock a replacement inode while the original daemon is still alive.
    """
    path = _daemon_path(store, "daemon.guard")
    store.guard_workspace()
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
            raise BusError("Daemon guard must be a regular nonlinked lease file", 403)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                cast(Any, fcntl).flock(descriptor, cast(Any, fcntl).LOCK_EX | cast(Any, fcntl).LOCK_NB)
            acquired = True
        except OSError:
            raise BusError("A Sharebus daemon holds this workspace's operating-system lease", 409) from None
        if not info.st_size:
            store.guard_workspace()
            os.write(descriptor, b"0")
        store.guard_workspace()
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
                    cast(Any, fcntl).flock(descriptor, cast(Any, fcntl).LOCK_UN)
        finally:
            os.close(descriptor)


def _legacy_owner_alive(store: BusStore) -> bool:
    """Preserve a pre-lease daemon only when its pinned HTTP identity matches."""
    runtime_path = _daemon_path(store, "runtime.json")
    store.guard_workspace()
    if not runtime_path.exists():
        return False
    try:
        store.guard_workspace()
        if runtime_path.stat().st_size > 65536:
            raise BusError("Daemon runtime metadata requires local review", 409)
        store.guard_workspace()
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        if not isinstance(runtime, dict):
            return False
        parsed = urlsplit(runtime.get("url", ""))
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                 and not parsed.username and not parsed.password and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment and runtime.get("workspace") == str(store.workspace)
                 and runtime.get("workspace_id") == store.workspace_id and type(runtime.get("pid")) is int
                 and runtime["pid"] > 0 and isinstance(runtime.get("nonce"), str)
                 and 16 <= len(runtime["nonce"]) <= 128)
        if not valid:
            return False
    except BusError:
        raise
    except (OSError, ValueError, TypeError):
        return False
    # HTTPConnection neither follows redirects nor uses proxy environment
    # settings. No capability, proof or model input is sent to this listener.
    # Measured Windows connection refusal arrived after 2 seconds.
    connection = HTTPConnection("127.0.0.1", parsed.port, timeout=5)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        raw = response.read(65537)
        if len(raw) > 65536:
            return False
        health = json.loads(raw)
        store.guard_workspace()
        if (isinstance(health, dict) and (health.get("ok") is True or health.get("stopping") is True)
                and health.get("workspace_id") == runtime["workspace_id"] and health.get("pid") == runtime["pid"]
                and health.get("nonce") != runtime["nonce"]):
            raise BusError("Existing workspace listener nonce is uncertain; no replacement daemon was started", 409)
        return (isinstance(health, dict) and (health.get("ok") is True or health.get("stopping") is True)
                and all(health.get(key) == runtime[key] for key in ("workspace_id", "pid", "nonce")))
    except TimeoutError:
        raise BusError("Existing listener identity is uncertain; no replacement daemon was started", 409) from None
    except BusError:
        raise
    except (OSError, HTTPException, ValueError):
        return False
    finally:
        connection.close()


def _archive_daemon_metadata(store: BusStore, name: str) -> None:
    source = _daemon_path(store, name)
    store.guard_workspace()
    if not source.exists():
        return
    store.guard_workspace()
    if source.stat().st_size > 65536:
        raise BusError("Daemon metadata requires local review before replacement", 409)
    history = _daemon_path(store, "daemon-history", directory=True)
    store.guard_workspace()
    history.mkdir(exist_ok=True)
    target = _daemon_path(store, "daemon-history/" + str(time.time_ns()) + "-" + secrets.token_hex(8) + "-" + name)
    store.guard_workspace()
    with target.open("xb") as stream:
        store.guard_workspace()
        content = source.read_bytes()
        store.guard_workspace()
        stream.write(content)
    store.guard_workspace()
    os.chmod(target, 0o600)


def _write_daemon_metadata(store: BusStore, name: str, value: dict[str, Any]) -> None:
    target = _daemon_path(store, name)
    temporary = _daemon_path(store, ".daemon-" + secrets.token_hex(16) + ".tmp")
    guard_failure = None

    def guard():
        nonlocal guard_failure
        if guard_failure is not None:
            raise guard_failure
        try:
            store.guard_workspace()
        except Exception as error:
            guard_failure = error
            raise

    try:
        guard()
        with temporary.open("x", encoding="utf-8") as stream:
            guard()
            json.dump(value, stream, indent=2)
        guard()
        os.chmod(temporary, 0o600)
        guard()
        os.replace(temporary, target)
    finally:
        if guard_failure is None:
            try:
                guard()
                if temporary.exists():
                    guard()
                    temporary.unlink()
            except BusError:
                pass


def _startup_log(workspace: str | Path, stage: str, *, store: BusStore | None = None) -> None:
    """Log lifecycle markers only, before any model request or private input."""
    if store is not None:
        store.guard_workspace()
    root = store.workspace if store is not None else BusStore.checked_workspace(workspace)
    for folder in (root / ".sharebus", root / ".sharebus/private"):
        if store is not None:
            store.guard_workspace()
        BusStore.reject_linked_path(folder)
        if store is not None:
            store.guard_workspace()
        folder.mkdir(exist_ok=True)
    log_path = root / ".sharebus/private/startup.log"
    if store is not None:
        store.guard_workspace()
    BusStore.reject_linked_path(log_path)
    if store is not None:
        store.guard_workspace()
    if log_path.exists():
        if store is not None:
            store.guard_workspace()
        if not log_path.is_file():
            raise BusError("Daemon startup log cannot be linked", 403)
        if store is not None:
            store.guard_workspace()
        if log_path.stat().st_nlink != 1:
            raise BusError("Daemon startup log cannot be linked", 403)
    if store is not None:
        store.guard_workspace()
    with log_path.open("a", encoding="utf-8") as log:
        if store is not None:
            store.guard_workspace()
        log.write(f"{time.time():.3f} pid={os.getpid()} stage={stage}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sharebus loopback daemon")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--agents", default="none")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    store = None
    server = None
    try:
        _startup_log(args.workspace, "store_begin")
        store = BusStore(args.workspace)
        _startup_log(args.workspace, "store_ready")
        _startup_log(args.workspace, "guard_begin")
        with workspace_guard(store):
            _startup_log(args.workspace, "guard_acquired")
            if _legacy_owner_alive(store):
                raise BusError("A verified existing Sharebus daemon owns this workspace", 409)
            for name in ("daemon.lock", "runtime.json"):
                _archive_daemon_metadata(store, name)
            nonce = secrets.token_hex(16)
            _write_daemon_metadata(store, "daemon.lock", {"version": 2, "pid": os.getpid(), "nonce": nonce,
                                                          "workspace_id": store.workspace_id, "workspace": str(store.workspace)})
            try:
                _startup_log(args.workspace, "bind_begin")
                server = BusHTTPServer(args.workspace, providers=args.agents, port=args.port)
                _startup_log(args.workspace, "http_ready")
                _write_daemon_metadata(store, "runtime.json", server.runtime())
                _startup_log(args.workspace, "serving")
                server.serve_forever(poll_interval=0.25)
            finally:
                if server:
                    server.close()
                expected = {"daemon.lock": nonce, "runtime.json": server.nonce if server else None}
                for name, owned_nonce in expected.items():
                    try:
                        path = _daemon_path(store, name)
                        if owned_nonce and json.loads(path.read_text(encoding="utf-8")).get("nonce") == owned_nonce:
                            _archive_daemon_metadata(store, name)
                            path.unlink()
                    except (OSError, ValueError):
                        pass
    except Exception as exc:
        if store is not None:
            with _daemon_path(store, "daemon.log").open("a", encoding="utf-8") as log:
                log.write(f"{time.time():.3f} broker failure: {type(exc).__name__}\n")
        raise


if __name__ == "__main__":
    main()
