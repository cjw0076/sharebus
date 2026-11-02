"""Independent authenticated help service and click-launcher client.

This module never starts, controls or writes configuration in a business room.
Only explicit human help messages can create a subscription-native turn.
"""
from __future__ import annotations

import argparse
import hashlib
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
import webbrowser

from adapters import NativeNeedsHuman
from sharebus import Client, NoRedirect
from sidecar_registry import RuntimeVerificationError, _verified_runtime
from support_native import GuideNativeAdapter, GuideProfileUnavailable, verified_executable
from support_diagnostics import business_runtime, project_diagnostic
from support_auth import (ConnectionUnavailable, ProtocolError, SERVICE, SIGNED_HEADERS, signed_exchange,
                          token_key, verify_request, response_signature)
from support_store import (ERRORS, REPLY_LIMIT, SupportError, SupportPaths, SupportStore,
                           check_directory, digest, read_private, write_private)


HERE = Path(__file__).resolve().parent
MAX_BODY = 65536
STATIC = {
    "/": ("web/support.html", "text/html; charset=utf-8"),
    "/support.html": ("web/support.html", "text/html; charset=utf-8"),
    "/assets/PretendardVariable.woff2": ("web/assets/PretendardVariable.woff2", "font/woff2"),
    "/assets/favicon.svg": ("web/assets/favicon.svg", "image/svg+xml"),
    **{"/assets/characters/sharebus-" + provider + ".svg": (
        "web/assets/characters/sharebus-" + provider + ".svg", "image/svg+xml")
       for provider in ("claude", "codex", "gemini")},
}


def load_spec() -> tuple[str, str]:
    try:
        path = HERE / "support_spec.md"
        raw = read_static(path)
        if len(raw) > 32768:
            raise ValueError
        return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError):
        raise SupportError("storage", 503) from None


def read_static(path: Path) -> bytes:
    """A finite asset is still refused if swapped for a private link or reparse."""
    check_directory(path.parent)
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 4 * 1024 * 1024):
        raise SupportError("request", 403)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
            raise SupportError("request", 403)
        raw = source.read(4 * 1024 * 1024 + 1)
    check_directory(path.parent)
    current = path.lstat()
    if (len(raw) > 4 * 1024 * 1024 or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
            or current.st_nlink != 1 or getattr(current, "st_file_attributes", 0) & 0x400):
        raise SupportError("request", 403)
    return raw


class DiagnosticClient(Client):
    """The existing loopback client restricted to one guarded, read-only path."""
    def __init__(self, paths: SupportPaths):
        paths.target_check()
        super().__init__(paths.selection.path)
        self.paths = paths

    def runtime(self, *, verify: bool = True) -> dict:
        # Always verify before a credential leaves this controller.
        return _verified_runtime(self.workspace, opener=self.opener, timeout=2,
                                 workspace_guard=self.paths.target_check)

    def request(self, path: str, body: dict | None = None) -> dict:
        if path != "/api/support/diagnose" or body is not None:
            raise SupportError("request", 403)
        self.paths.target_check()
        runtime = self.runtime()
        token = read_private(self.private / "operator.token").strip()
        self.paths.target_check()
        status, raw = signed_exchange(self.opener, business_runtime(runtime), token_key(token),
            "business-diagnostic", path, {}, timeout=3, maximum=8192)
        self.paths.target_check()
        if status != 200:
            raise SupportError("request", 502)
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise SupportError("request", 502)
        return state


class SupportController:
    def __init__(self, paths: SupportPaths, *, adapter_factory=GuideNativeAdapter, diagnostic_reader=None):
        self.paths = paths
        self._lease = paths.lease()
        self._lease.__enter__()
        self._closed = False
        self._lease_released = False
        self._lock = threading.RLock()
        self._native_lock = threading.Lock()
        self._adapters = {}
        self._threads = []
        self._adapter_factory = adapter_factory
        self._diagnostic_reader = diagnostic_reader
        self._diagnostic_cache = None
        self._volatile_unknown = False
        self.url = None
        try:
            self.store = SupportStore(paths)
            self.store.open("claude", load_spec()[1])
            self.store.recover()
            token_path = paths.file("operator.token")
            try:
                write_private(token_path, secrets.token_urlsafe(32), exclusive=True)
            except FileExistsError:
                pass
            self.operator = read_private(token_path).strip()
            if not 32 <= len(self.operator) <= 128:
                raise SupportError("storage", 403)
        except Exception:
            self._lease.__exit__(*sys.exc_info())
            raise

    def open(self, provider="claude") -> dict:
        with self._lock:
            if self._closed:
                raise SupportError("connection", 503)
            _, spec_hash = load_spec()
            binding = self.store.open(provider, spec_hash)
            if (not self._volatile_unknown and binding["reason_code"] is not None
                    and binding["state"] in {"ready", "error"}):
                try:
                    verified_executable()
                    reason = None
                except GuideProfileUnavailable as error:
                    reason = error.reason_code
                self.store.refresh_profile(binding["epoch"], reason)
        return self.state()

    def diagnostic(self, *, fresh: bool = True) -> dict:
        self.paths.target_check()
        if not fresh and self._diagnostic_cache and self._diagnostic_cache[0] > time.monotonic() - 2:
            return self._diagnostic_cache[1]
        try:
            state = (self._diagnostic_reader() if self._diagnostic_reader is not None
                     else DiagnosticClient(self.paths).request("/api/support/diagnose"))
            result = project_diagnostic(state) if self._diagnostic_reader is not None else state
        except FileNotFoundError:
            result = project_diagnostic(None, "offline")
        except RuntimeVerificationError as error:
            offline = error.reason_code in {"runtime_missing", "connect_refused"}
            result = project_diagnostic(None, "offline" if offline else "unknown")
        except (OSError, RuntimeError, ValueError):
            result = project_diagnostic(None)
        self.paths.target_check()
        self._diagnostic_cache = (time.monotonic(), result)
        return result

    def state(self) -> dict:
        try:
            status = self.diagnostic(fresh=False)["connection"]
        except SupportError:
            status = "unknown"
        result = self.store.public(status)
        if self._volatile_unknown:
            result["guide"].update(state="unknown", can_send=False)
        return result

    def message(self, text: str, request_id: str) -> dict:
        with self._lock:
            if self._closed or not self.url:
                raise SupportError("connection", 503)
            if self._volatile_unknown:
                raise SupportError("unknown", 409)
            _, spec_hash = load_spec()
            self.store.open(self.store.binding()["provider"], spec_hash)
            job, capability = self.store.create_job(text, request_id)
            if capability is not None:
                worker = None
                try:
                    worker = threading.Thread(target=self._work, args=(job, capability), daemon=True,
                                              name="sharebus-help")
                    worker.start()
                except Exception:
                    if worker is not None and worker.is_alive():
                        self._threads.append(worker)
                    try:
                        self.store.cancel(request_id)
                    except Exception:
                        self._volatile_unknown = True
                    raise SupportError("connection", 503) from None
                self._threads.append(worker)
            return {"ok": True, "job_id": job["id"], "state": job["state"]}

    def _work(self, job: dict, capability: str) -> None:
        identifier = job["id"]
        adapter = None
        state, text, native_id = "failed", "", None
        terminal_native_failure = False
        reason_code = None
        with self._native_lock:
            try:
                spec, expected_spec = load_spec()
                binding = self.store.binding()
                if self._closed or binding["state"] != "working" or binding["epoch"] != job["epoch"]:
                    return
                if expected_spec != binding["spec_hash"]:
                    raise SupportError("stale", 409)

                def checked():
                    self.paths.target_check()
                    if self._closed or load_spec()[1] != expected_spec:
                        raise SupportError("stale", 409)

                def event(value):
                    checked()
                    if value.get("type") in {"input_attempt", "session"}:
                        self.store.receipt(identifier, value)

                def reserved(value):
                    checked()
                    self.store.reserve_native(identifier, value)

                def identity(value):
                    checked()
                    self.store.receipt(identifier, {"type": "session", "session_id": value})

                def approval(_value):
                    raise NativeNeedsHuman("This support request requires the real human in the native app.")

                context = self.paths.file("diagnostic-" + identifier + ".json")
                write_private(context, json.dumps({"runtime": self.service_runtime, "job_id": identifier,
                    "token": capability, "deadline": job["deadline"]}), exclusive=True)
                adapter = self._adapter_factory(self.paths.directory, context, on_event=event,
                    on_reserved=reserved, on_identity=identity,
                    on_profile=lambda: self.store.profile_ready(identifier), on_approval=approval)
                with self._lock:
                    if self._closed:
                        raise SupportError("connection", 503)
                    self._adapters[identifier] = adapter
                if binding["native_id"]:
                    adapter.session_id = binding["native_id"]
                snapshot = self.diagnostic()
                checked()
                prompt = (spec + "\n\n현재 작업실의 제한된 진단:\n" + json.dumps(snapshot, ensure_ascii=False)
                          + "\n\n사용자의 도움 요청:\n" + job["text"])
                remaining = job["deadline"] - time.time()
                if remaining <= 0:
                    raise SupportError("unknown", 409)
                result = adapter.turn(prompt, timeout=min(remaining, 180))
                checked()
                native_id = result.session_id
                if result.delivery_unknown:
                    state = "unknown"
                elif result.needs_human:
                    state = "needs_human"
                elif result.success:
                    state, text = "completed", result.text
                elif result.input_attempted and not result.accepted:
                    state = "unknown"
                elif result.status == "failed" and result.accepted:
                    terminal_native_failure = True
            except GuideProfileUnavailable as error:
                reason_code = error.reason_code
            except NativeNeedsHuman:
                state = "needs_human"
            except Exception:
                # Private OS/provider exception values never reach UI or model.
                state = "failed"
            finally:
                if adapter is not None:
                    try:
                        adapter.close()
                    except Exception:
                        state, text = "unknown", ""
                    if getattr(adapter, "pid", None) is not None:
                        state, text = "unknown", ""
                with self._lock:
                    if adapter is None or getattr(adapter, "pid", None) is None:
                        self._adapters.pop(identifier, None)
                try:
                    if len(text) > REPLY_LIMIT:
                        state, text = "unknown", ""
                    self.store.finish(identifier, text=text, state=state, native_id=native_id,
                                      terminal_native_failure=terminal_native_failure,
                                      reason_code=reason_code if state == "failed" else None)
                except Exception:
                    # The committed in-flight record is recovered as unknown by
                    # the next leased owner; no substitute session is generated.
                    self._volatile_unknown = True

    def cancel(self, request_id: str) -> dict:
        with self._lock:
            state = self.store.cancel(request_id)
            with self.store.connection() as db:
                row = db.execute("SELECT id FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            adapter = self._adapters.get(row["id"])
        if adapter is not None:
            adapter.interrupt()
        return {"ok": True, "state": state}

    def diagnose_for(self, identifier: str) -> dict:
        job = self.store.diagnostic_job(identifier)
        result = self.diagnostic()
        # Revalidate after the read: cancel/expiry during I/O must revoke output.
        current = self.store.diagnostic_job(identifier)
        if current["id"] != job["id"] or current["epoch"] != job["epoch"]:
            raise SupportError("auth", 403)
        return result

    def close(self) -> None:
        with self._lock:
            if self._lease_released:
                return
            self._closed = True
            with self.store.connection() as db:
                active = db.execute("SELECT request_id FROM jobs WHERE state IN ('working','needs_human')").fetchall()
            for row in active:
                self.store.cancel(row["request_id"])
            adapters = list(self._adapters.values())
        for adapter in adapters:
            try:
                adapter.interrupt()
            except Exception:
                pass
        for worker in self._threads:
            if worker is not threading.current_thread():
                worker.join(timeout=6)
        if (any(worker.is_alive() for worker in self._threads)
                or any(getattr(adapter, "pid", None) is not None for adapter in adapters)):
            # A bounded join is observation, not proof of termination. Keep the
            # OS lease and the revoked/unknown ledger until actual ownership ends.
            raise SupportError("busy", 409)
        self._lease.__exit__(None, None, None)
        self._lease_released = True


class SupportHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, controller: SupportController):
        self.controller = controller
        self.nonce = secrets.token_urlsafe(24)
        super().__init__(("127.0.0.1", 0), SupportHandler)
        self.url = "http://127.0.0.1:" + str(self.server_port)
        self.cookie_name = "sharebus_support_" + digest(self.nonce)[:12]
        self.browser_cookie = secrets.token_urlsafe(32)
        self.browser_proof = secrets.token_urlsafe(32)
        self._auth_lock = threading.Lock()
        self._used_nonces = {}
        self._tickets = {}
        controller.url = self.url
        controller.service_runtime = self.runtime()

    def runtime(self) -> dict:
        return {"service": SERVICE, "key": self.controller.paths.key, "nonce": self.nonce,
                "pid": os.getpid(), "url": self.url}

    def consume_nonce(self, actor: str, nonce: str, expiry: int):
        with self._auth_lock:
            now = time.time()
            self._used_nonces = {key: end for key, end in self._used_nonces.items() if end >= now}
            key = actor, nonce
            if key in self._used_nonces or len(self._used_nonces) >= 2048:
                raise SupportError("auth", 403)
            self._used_nonces[key] = expiry

    def issue_ticket(self) -> str:
        with self._auth_lock:
            now = time.time()
            self._tickets = {key: end for key, end in self._tickets.items() if end > now}
            if len(self._tickets) >= 32:
                raise SupportError("busy", 429)
            ticket = secrets.token_urlsafe(32)
            self._tickets[digest(ticket)] = now + 60
            return ticket

    def consume_ticket(self, ticket: str):
        with self._auth_lock:
            expiry = self._tickets.pop(digest(ticket), 0)
            if expiry <= time.time():
                raise SupportError("auth", 403)


class SupportHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: SupportHTTPServer

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        return

    def _send(self, status: int, data: bytes, content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                         "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        signed = getattr(self, "_signed", None)
        if signed:
            actor, nonce, key = signed
            self.send_header("X-Sharebus-Request-Nonce", nonce)
            self.send_header("X-Sharebus-Response-Signature", response_signature(
                key, self.server.runtime(), actor, nonce, status, data))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def _json(self, status: int, value: dict, extra=None):
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), extra=extra)

    def _host(self) -> str:
        hosts = self.headers.get_all("Host", [])
        allowed = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
        if len(hosts) != 1 or hosts[0] not in allowed:
            raise SupportError("auth", 403)
        return "http://" + hosts[0]

    def _body(self) -> dict:
        origin = self._host()
        if (self.headers.get_all("Origin", []) != [origin]
                or self.headers.get_all("X-Sharebus-Request", []) != ["1"]
                or self.headers.get_all("Content-Type", []) != ["application/json"]
                or self.headers.get_all("Transfer-Encoding", [])):
            raise SupportError("request", 403)
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or len(lengths[0]) > 8 or not lengths[0].isascii()
                or not lengths[0].isdigit()):
            raise SupportError("request", 400)
        length = int(lengths[0])
        if not 2 <= length <= MAX_BODY:
            raise SupportError("input", 413)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            value = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(value, dict):
                raise ValueError
            self._request_body = raw
            return value
        except (OSError, ValueError, RecursionError):
            raise SupportError("request") from None

    def _signed_actor(self, expected: str | None = None) -> str:
        actors = self.headers.get_all("X-Sharebus-Actor", [])
        if len(actors) != 1 or self.headers.get_all("Authorization", []):
            raise SupportError("auth", 403)
        actor = actors[0]
        if expected is not None and actor != expected:
            raise SupportError("auth", 403)
        if actor == "operator":
            key = token_key(self.server.controller.operator)
        elif actor.startswith("diagnostic:") and len(actor) == 43:
            job = self.server.controller.store.diagnostic_job(actor[11:])
            key = job["cap_hash"]
        else:
            raise SupportError("auth", 403)
        try:
            nonce, expiry = verify_request(key, self.server.runtime(), actor, self.command, self.path,
                getattr(self, "_request_body", b""), self.headers)
        except ProtocolError:
            raise SupportError("auth", 403) from None
        self.server.consume_nonce(actor, nonce, expiry)
        self._signed = actor, nonce, key
        return actor

    def _operator(self, *, signed_only=False) -> None:
        if self.headers.get_all("X-Sharebus-Actor", []):
            self._signed_actor("operator")
            return
        if signed_only or self.headers.get_all("Authorization", []):
            raise SupportError("auth", 403)
        cookies = self.headers.get_all("Cookie", [])
        proofs = self.headers.get_all("X-Sharebus-Support-Proof", [])
        if len(cookies) != 1 or len(cookies[0]) > 4096 or len(proofs) != 1 or not proofs[0].isascii():
            raise SupportError("auth", 403)
        try:
            cookie = SimpleCookie()
            cookie.load(cookies[0])
            value = cookie[self.server.cookie_name].value
        except (CookieError, KeyError):
            raise SupportError("auth", 403) from None
        if (not value.isascii() or not secrets.compare_digest(value, self.server.browser_cookie)
                or not secrets.compare_digest(proofs[0], self.server.browser_proof)):
            raise SupportError("auth", 403)

    def _run(self, mutation: bool):
        self._host()
        if "?" in self.path or "#" in self.path:
            raise SupportError("request", 404)
        controller = self.server.controller
        if not mutation:
            if self.path == "/health":
                if any(self.headers.get_all(name, []) for name in SIGNED_HEADERS):
                    self._signed_actor()
                self._json(200, {"ok": True, **self.server.runtime()})
            elif self.path == "/api/support/state":
                self._operator()
                self._json(200, controller.state())
            elif self.path in STATIC:
                relative, mime = STATIC[self.path]
                path = HERE / relative
                self._send(200, read_static(path), mime)
            else:
                raise SupportError("request", 404)
            return
        body = self._body()
        if self.path == "/api/auth":
            if (set(body) != {"ticket"} or not isinstance(body["ticket"], str)
                    or not body["ticket"].isascii() or not 32 <= len(body["ticket"]) <= 128):
                raise SupportError("auth", 403)
            self.server.consume_ticket(body["ticket"])
            cookie = self.server.cookie_name + "=" + self.server.browser_cookie + "; HttpOnly; SameSite=Strict; Path=/"
            self._json(200, {"ok": True, "browser_proof": self.server.browser_proof}, {"Set-Cookie": cookie})
            return
        if self.path == "/api/internal/diagnose":
            if body:
                raise SupportError("request", 400)
            actor = self._signed_actor()
            if not actor.startswith("diagnostic:"):
                raise SupportError("auth", 403)
            self._json(200, controller.diagnose_for(actor[11:]))
            return
        if self.path == "/api/support/ticket" and not body:
            self._operator(signed_only=True)
            self._json(200, {"ticket": self.server.issue_ticket(), "expires_in": 60})
            return
        self._operator()
        if self.path == "/api/support/open" and set(body) == {"provider"}:
            self._json(200, controller.open(body["provider"]))
        elif self.path == "/api/support/message" and set(body) == {"text", "request_id"}:
            self._json(202, controller.message(body["text"], body["request_id"]))
        elif self.path == "/api/support/cancel" and set(body) == {"request_id"} and isinstance(body["request_id"], str):
            self._json(200, controller.cancel(body["request_id"]))
        else:
            raise SupportError("request", 404)

    def _handle(self, mutation):
        try:
            self._run(mutation)
        except SupportError as error:
            self._json(error.status, {"code": error.code, "message": str(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception:
            self._json(500, {"code": "connection", "message": ERRORS["connection"]})

    def do_GET(self):
        self._handle(False)

    def do_POST(self):
        self._handle(True)

    def do_OPTIONS(self):
        self._json(405, {"code": "request", "message": ERRORS["request"]})


class SupportClient:
    def __init__(self, workspace: Path, *, home: Path | None = None, root: Path | None = None):
        self.paths = SupportPaths(Path(workspace), home=home, root=root)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self._process = None

    def _runtime(self, *, observed: list[str] | None = None, timeout: float = 2) -> dict:
        self.paths.target_check()
        try:
            raw_runtime = read_private(self.paths.file("runtime.json"))
            if observed is not None:
                observed.append(raw_runtime)
            runtime = json.loads(raw_runtime)
            parsed = urlsplit(runtime.get("url", ""))
            if (runtime.get("service") != SERVICE or runtime.get("key") != self.paths.key
                    or parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                    or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password
                    or type(runtime.get("pid")) is not int or runtime["pid"] <= 0
                    or not isinstance(runtime.get("nonce"), str) or not 16 <= len(runtime["nonce"]) <= 128):
                raise SupportError("auth", 403)
            try:
                operator = read_private(self.paths.file("operator.token")).strip()
            except FileNotFoundError:
                raise SupportError("auth", 403)
            status, raw = signed_exchange(self.opener, runtime, token_key(operator), "operator",
                                          "/health", timeout=timeout, maximum=4096)
            if status != 200:
                raise SupportError("auth", 403)
            health = json.loads(raw)
            if not isinstance(health, dict) or health.get("ok") is not True or any(
                    type(health.get(key)) is not type(runtime.get(key)) or health.get(key) != runtime.get(key)
                    for key in ("service", "key", "nonce", "pid", "url")):
                raise SupportError("auth", 403)
            self.paths.target_check()
            return runtime
        except FileNotFoundError:
            raise
        except ConnectionUnavailable:
            raise SupportError("connection", 503) from None
        except ProtocolError:
            raise SupportError("auth", 403) from None
        except (OSError, ValueError, AttributeError, TypeError):
            raise SupportError("auth", 403) from None

    def _request(self, path: str, body: dict | None = None) -> dict:
        runtime = self._runtime()
        token = read_private(self.paths.file("operator.token")).strip()
        try:
            status, raw = signed_exchange(self.opener, runtime, token_key(token), "operator", path, body, timeout=10)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise SupportError("connection", 502)
            if status >= 400:
                raise SupportError(value.get("code") if value.get("code") in ERRORS else "connection", status)
            self._last_runtime = runtime
            return value
        except ProtocolError:
            raise SupportError("auth", 403) from None
        except (OSError, ValueError):
            raise SupportError("connection", 503) from None

    def start(self, provider="claude", *, open_browser=True) -> dict:
        if provider != "claude":
            raise SupportError("provider", 409)
        if type(open_browser) is not bool:
            raise SupportError("request")
        self.paths.ensure()
        stale = []
        try:
            self._runtime(observed=stale)
        except (FileNotFoundError, SupportError) as error:
            if isinstance(error, SupportError) and error.code not in {"connection", "auth"}:
                raise
            # Failure to authenticate an old endpoint never grants reuse. Only
            # the actual OS lease permits a new owner candidate; its child must
            # reacquire that same lease before opening the existing ledger.
            try:
                with self.paths.lease():
                    self.paths.target_check()
                    self.paths.check()
            except SupportError as ownership:
                if ownership.code == "busy":
                    if isinstance(error, SupportError):
                        raise error
                    raise SupportError("connection", 503) from None
                raise
            try:
                previous = json.loads(stale[0]) if stale else None
                old_nonce = previous.get("nonce") if isinstance(previous, dict) else None
            except ValueError:
                old_nonce = None
            self.paths.target_check()
            self.paths.check()
            command = [sys.executable, "-B", str(Path(__file__).resolve()), "--serve",
                       "--workspace", str(self.paths.selection.path), "--root", str(self.paths.root)]
            kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                      "cwd": str(self.paths.directory)}
            if os.name == "nt":
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                if pythonw.is_file():
                    command[0] = str(pythonw)
                kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                self._process = subprocess.Popen(command, **kwargs)
            except OSError:
                raise SupportError("connection", 503) from None
            deadline = time.monotonic() + 15
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SupportError("connection", 503)
                current_snapshot = []
                try:
                    runtime = self._runtime(observed=current_snapshot, timeout=min(2, remaining))
                except (FileNotFoundError, SupportError) as current:
                    if (isinstance(current, SupportError) and current.code != "connection"
                            and not (current.code == "auth" and len(stale) == 1 and current_snapshot == stale)):
                        raise
                else:
                    if runtime["nonce"] != old_nonce:
                        break
                # A lock-losing child may exit while a concurrent winner starts.
                # Never spawn again or accept the authenticated old generation.
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        state = self._request("/api/support/open", {"provider": provider})
        if open_browser:
            self.open()
        return state

    def open(self) -> None:
        value = self._request("/api/support/ticket", {})
        ticket = value.get("ticket")
        if (not isinstance(ticket, str) or not ticket.isascii() or not 32 <= len(ticket) <= 128
                or type(value.get("expires_in")) is not int or not 0 < value["expires_in"] <= 60):
            raise SupportError("auth", 403)
        webbrowser.open(self._last_runtime["url"] + "/#ticket=" + ticket)

    def state(self) -> dict:
        return self._request("/api/support/state")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="ShareBus의 저장되는 도움 대화")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.serve:
        parser.print_help()
        return 0
    controller = server = None
    try:
        controller = SupportController(SupportPaths(args.workspace, root=args.root))
        controller.open()
        server = SupportHTTPServer(controller)
        write_private(controller.paths.file("runtime.json"), json.dumps(server.runtime()))
        server.serve_forever(poll_interval=0.2)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError):
        return 1
    finally:
        if server is not None:
            server.server_close()
        if controller is not None:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
