"""Explicit, lazy ownership of subscribed CLI processes in one workspace."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from bridge import BridgeError, BusError, BusStore, WorkspaceRetired
from hosted_runtime import SUPPORTED
from sidecar_registry import RuntimeVerificationError, register_workspace, verified_runtime


class HostedRuntime:
    def __init__(self, bridge, *, adapter_factory=None, discovery=None, peer_callback=None, workspace_guard=None,
                 acquire_work=None, admission=None, native_completion_handler=None, initialize_on_prompt=True):
        self.bridge = bridge
        self.adapter_factory = adapter_factory
        self.discovery = discovery
        self.peer_callback = peer_callback
        self.acquire_work = acquire_work
        self.admission = admission
        self.native_completion_handler = native_completion_handler
        self.initialize_on_prompt = initialize_on_prompt
        self.server = None
        self.thread = None
        self.guard = None
        self.phase = "idle"
        self._workspace_guard = workspace_guard or bridge._guard_workspace
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._ready = threading.Event()
        self._runtime = None
        self._construction = None
        self._service_thread = None
        self._service_generation = None
        self._registered = None
        self._cache_lock = threading.Lock()
        self._service_cache = {"state": "idle", "reason_code": None, "retry_in": 0, "retry_count": 0}

    def _guard_workspace(self):
        if self.phase == "retired":
            raise WorkspaceRetired()
        try:
            self._workspace_guard()
        except WorkspaceRetired:
            self.phase = "retired"
            self._closing.set()
            self._ready.clear()
            raise
        except BridgeError:
            if self.bridge._workspace_retired:
                self.phase = "retired"
                self._closing.set()
                self._ready.clear()
                raise WorkspaceRetired() from None
            raise

    def _require_open(self):
        if self._closing.is_set() or self.phase in {"closing", "closed"}:
            if self.phase == "retired":
                raise WorkspaceRetired()
            raise BridgeError("Owned workspace service is closing", 409)
        self._guard_workspace()
        if self._closing.is_set():
            raise BridgeError("Owned workspace service is closing", 409)

    def _service_status(self, state, reason=None, *, retry_in=0, retry_count=0):
        if reason not in {"runtime_missing", "connect_refused", "denied", "invalid_metadata", "generation_mismatch", "uncertain"}:
            reason = "uncertain" if reason is not None else None
        with self._cache_lock:
            self._service_cache = {"state": state, "reason_code": reason,
                                   "retry_in": min(30, max(0, retry_in)),
                                   "retry_count": min(1000000, max(0, retry_count))}

    def service_snapshot(self):
        """Bounded cached observation; never starts or probes the service."""
        with self._cache_lock:
            return dict(self._service_cache)

    def start_service(self):
        with self._cache_lock:
            if self._closing.is_set() or self._service_thread is not None:
                return
            worker = threading.Thread(target=self._service_loop, name="avalokita-workspace-service", daemon=True)
            self._service_thread = worker
            try:
                worker.start()
            except BaseException:
                self._service_thread = None
                raise

    def begin_closing(self):
        # Signal before acquiring any construction lock or waiting for a worker.
        self._closing.set()
        self._ready.clear()

    def stop_service(self):
        self.begin_closing()
        worker = self._service_thread
        if worker is not None and worker.ident is not None and worker is not threading.current_thread():
            worker.join(timeout=3)

    def _service_loop(self):
        delay, failures = 1, 0
        while not self._closing.is_set():
            try:
                retry, progress = self._service_once()
            except Exception as error:
                if self._closing.is_set():
                    break
                self._service_status("retrying", getattr(error, "reason_code", "uncertain"))
                retry, progress = True, False
            if self._closing.is_set():
                break
            if progress:
                delay, failures = 1, 0
            if retry:
                failures += 1
                cached = self.service_snapshot()
                self._service_status(cached["state"], cached["reason_code"], retry_in=delay, retry_count=failures)
                wait = delay
                delay = min(30, delay * 2)
            else:
                wait = 1 if self.server is not None else 30
            deadline = time.monotonic() + wait
            while not self._closing.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closing.wait(remaining):
                    break
        self._service_status("retired" if self.phase == "retired" else "closing")
        self._ready.clear()

    def _service_once(self):
        release = self.acquire_work() if self.acquire_work is not None else (lambda: None)
        if release is None:
            self.begin_closing()
            return False, False
        try:
            owned = self._ensure_bus()
            if owned is None:
                self._service_status("external_owner_preserved")
                return False, False
            runtime = owned[4]
            progress = self._service_generation is not runtime
            self._service_generation = runtime
            if self._registered is runtime:
                self._service_status("ready")
                return False, progress
            try:
                self._verify_owned(owned)
                register_workspace(self.bridge.workspace, runtime=runtime,
                                   workspace_guard=lambda: self._guard_owned(owned))
                self._guard_owned(owned)
            except Exception as error:
                if self._closing.is_set():
                    raise
                self._service_status("ready_degraded", getattr(error, "reason_code", "uncertain"))
                return True, progress
            self._registered = runtime
            self._service_status("ready")
            return False, True
        finally:
            release()

    @staticmethod
    def _same_runtime(expected, current):
        return all(type(expected.get(key)) is type(current.get(key)) and expected.get(key) == current.get(key)
                   for key in ("workspace", "url", "workspace_id", "pid", "nonce"))

    def _guard_owned(self, owned):
        self._require_open()
        server, thread, guard, ready, runtime = owned
        with self._lock:
            if (self.server is not server or self.thread is not thread or self.guard is not guard or guard is None
                    or self._runtime is not runtime or not thread.is_alive() or not server.serving.is_set()):
                raise RuntimeVerificationError("Owned workspace generation changed.", "generation_mismatch")

    def _verify_owned(self, owned):
        self._guard_owned(owned)
        current = verified_runtime(self.bridge.workspace, workspace_guard=lambda: self._guard_owned(owned))
        if not self._same_runtime(owned[4], current):
            raise RuntimeVerificationError("Owned workspace generation changed.", "generation_mismatch")
        self._guard_owned(owned)

    @staticmethod
    def _provider_setting(db):
        row = db.execute("SELECT value FROM settings WHERE key='hosted_providers'").fetchone()
        if row is None:
            return None
        try:
            providers = json.loads(row[0])
        except (TypeError, ValueError):
            raise BridgeError("Select codex, claude or antigravity hosted providers", 409) from None
        if (not isinstance(providers, list) or len(providers) > 3
                or any(not isinstance(item, str) or item not in SUPPORTED for item in providers)
                or len(set(providers)) != len(providers)):
            raise BridgeError("Select codex, claude or antigravity hosted providers", 409)
        return providers

    def providers(self):
        self._guard_workspace()
        with self.bridge._db() as db:
            return self._provider_setting(db) or []

    def _provider_candidates(self):
        from discovery import resolve_executable
        if self.discovery is not None:
            return [dict(item) for item in self.discovery]
        candidates = []
        for provider in SUPPORTED:
            executable = resolve_executable(provider)
            supported = bool(executable and (os.name != "nt" or Path(executable).suffix.lower() not in {".cmd", ".bat"}))
            candidates.append({"provider": provider, "installed": bool(executable), "managed_supported": supported})
        return candidates

    def state(self):
        self._guard_workspace()
        providers = self.providers()
        base = {"enabled": bool(providers), "providers": providers, "supported": list(SUPPORTED), "sessions": [], "approvals": []}
        try:
            value = self.bridge.request("/api/hosted")
            return {**value, "enabled": bool(providers), "providers": providers}
        except BridgeError:
            return {**base, "state": "blocked" if providers else "disabled", "reason": "hosted_transport_unavailable" if providers else "hosted_not_enabled"}

    def _ensure_bus(self):
        with self._lock:
            self._require_open()
            if self.server is not None and self.thread.is_alive() and self._ready.is_set():
                return self.server, self.thread, self.guard, self._ready, self._runtime
            pending = self._construction
            owner = pending is None
            if owner:
                pending = {"done": threading.Event(), "error": None, "result": None}
                self._construction = pending
        if not owner:
            while not pending["done"].wait(0.05):
                if self._closing.is_set():
                    raise BridgeError("Owned workspace service is closing", 409)
            if pending["error"] is not None:
                raise pending["error"]
            return pending["result"]
        try:
            pending["result"] = self._construct_bus()
            return pending["result"]
        except BaseException as error:
            pending["error"] = error
            raise
        finally:
            with self._lock:
                if self._construction is pending:
                    self._construction = None
                pending["done"].set()

    def _construct_bus(self):
        from daemon import BusHTTPServer, workspace_guard, _legacy_owner_alive, _archive_daemon_metadata, _write_daemon_metadata
        with self._lock:
            previous = (self.server, self.thread, self.guard)
            if self.server is not None:
                self._ready.clear()
                self.server = self.thread = self.guard = self._runtime = self._registered = None
        if previous[0] is not None:
            self._dispose(*previous)
        self._require_open()
        try:
            verified_runtime(self.bridge.workspace, workspace_guard=self._guard_workspace)
        except RuntimeVerificationError as initial:
            if initial.reason_code not in {"runtime_missing", "connect_refused"}:
                raise
            eligibility, refused = initial.reason_code, initial.runtime
        else:
            self._service_status("external_owner_preserved")
            return None
        self.phase = "constructing"
        self._service_status("starting")
        server = thread = guard = None
        ready = threading.Event()

        def admit():
            if not ready.is_set() or self._closing.is_set():
                return None
            return self.acquire_work() if self.acquire_work is not None else (lambda: None)

        try:
            self._require_open()
            store = BusStore(self.bridge.workspace, workspace_guard=self._guard_workspace)
            lease = workspace_guard(store)
            lease.__enter__()
            guard = lease
            self._require_open()
            try:
                verified_runtime(self.bridge.workspace, workspace_guard=self._guard_workspace)
            except RuntimeVerificationError as fresh:
                if (fresh.reason_code != eligibility or eligibility == "connect_refused"
                        and (refused is None or fresh.runtime is None or not self._same_runtime(refused, fresh.runtime)
                             or refused["workspace_id"] != store.workspace_id)):
                    raise RuntimeVerificationError("Workspace service ownership changed.", "generation_mismatch") from None
            else:
                self._service_status("external_owner_preserved")
                return None
            if _legacy_owner_alive(store):
                raise BridgeError("Existing native workspace service requires review; it was preserved", 409)
            candidates = self._provider_candidates()
            self._require_open()
            server = BusHTTPServer(store.workspace, store=store, discovery=candidates,
                                   adapter_factory=self.adapter_factory, hosted_only=True, acquire_work=admit,
                                   hosted_admission=self.admission)
            if self.native_completion_handler is not None:
                server._avalokita_completion_unregister = store.register_native_completion_handler(self.native_completion_handler)
            server.avalokita_peer = self.peer_callback
            self._require_open()
            for name in ("runtime.json", "daemon.lock"):
                _archive_daemon_metadata(store, name)
            self._require_open()
            runtime = server.runtime()
            _write_daemon_metadata(store, "runtime.json", runtime)

            def serve():
                try:
                    server.serve_forever(poll_interval=0.25)
                finally:
                    # This Event belongs to this generation, even after replacement.
                    ready.clear()

            thread = threading.Thread(target=serve, name="avalokita-owned-bus", daemon=True)
            self._require_open()
            thread.start()
            with self._lock:
                self.guard, self.server, self.thread = guard, server, thread
                self._ready, self._runtime = ready, runtime
            owned = server, thread, guard, ready, runtime
            # Health/HTTP and readiness waits never hold the construction RLock.
            if not server.serving.wait(timeout=5):
                raise RuntimeVerificationError("Owned workspace listener is not ready.")
            self._verify_owned(owned)
            with self._lock:
                self._require_open()
                self.phase = "serving"
                ready.set()
            self._service_status("ready_degraded")
            return owned
        except BaseException:
            ready.clear()
            with self._lock:
                if self.server is server:
                    self.server = self.thread = self.guard = self._runtime = self._registered = None
            self._dispose(server, thread, guard, cleanup_metadata=False)
            guard = None
            if not self._closing.is_set():
                self.phase = "idle"
            raise
        finally:
            if server is None and guard is not None:
                guard.__exit__(None, None, None)

    def configure(self, payload):
        if (not isinstance(payload, dict) or set(payload) != {"providers"} or not isinstance(payload["providers"], list)
                or len(payload["providers"]) > 3 or any(not isinstance(item, str) or item not in SUPPORTED for item in payload["providers"])
                or len(set(payload["providers"])) != len(payload["providers"])):
            raise BridgeError("Select codex, claude or antigravity hosted providers")
        with self._lock:
            self._require_open()
            providers = payload["providers"]
            with self.bridge._db() as db:
                db.execute("INSERT INTO settings VALUES ('hosted_providers',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(providers),))
        owned = None
        try:
            if providers:
                owned = self._ensure_bus()
        except RuntimeVerificationError:
            raise BridgeError("Workspace service ownership requires review", 409) from None
        self._require_open()
        try:
            return self.bridge.request("/api/hosted", {"providers": providers})
        except BridgeError as exc:
            if exc.status not in {404, 409}:
                raise
            if owned is not None:
                observed = {"enabled": bool(providers), "providers": providers, "supported": list(SUPPORTED),
                            "sessions": [], "approvals": []}
                try:
                    self._verify_owned(owned)
                    candidate = self.bridge.request("/api/hosted")
                    self._verify_owned(owned)
                    observed = candidate
                    sessions = observed.get("sessions", [])
                    if (owned[3].is_set() and observed.get("state") == "ready" and observed.get("enabled") is True
                            and set(observed.get("providers", [])) == set(providers)
                            and {item["provider"] for item in sessions if item.get("enabled")} == set(providers)
                            and all(not item.get("blocked", True) for item in sessions if item.get("enabled"))):
                        return observed
                except (BridgeError, RuntimeVerificationError):
                    pass
                return {**observed, "state": "blocked", "reason": "hosted_native_adapter_unavailable"}
            return {**self.state(), "state": "blocked" if providers else "disabled", "reason": "hosted_native_adapter_unavailable"}

    def before_prompt(self):
        """Prepare only a new human goal; discovery never grants native input."""
        self._require_open()
        with self.bridge._db() as db:
            providers = self._provider_setting(db)
        if providers == [] or (providers is None and not self.initialize_on_prompt) or self.bridge.paused():
            return None
        candidates = self._provider_candidates()
        available = [item["provider"] for item in candidates if item.get("provider") in SUPPORTED
                     and item.get("installed") is True and item.get("managed_supported") is True]
        if providers is None and not available:
            raise BridgeError("Hosted provider CLI is unavailable", 409)
        with self.bridge._db() as db:
            if self._provider_setting(db) == []:
                return None
        try:
            owned = self._ensure_bus()
            if owned is None:
                raise BridgeError("Workspace service ownership requires review", 409)
            self._verify_owned(owned)
            supervisor = owned[0].supervisor
            # Acquire its existing configure lock before the local writer, just
            # as the HTTP handler does. No lifecycle lock spans HTTP or startup.
            with supervisor._lock, self._lock, self.bridge._db() as db:
                db.execute("BEGIN IMMEDIATE")
                self._guard_owned(owned)
                if not owned[3].is_set():
                    raise RuntimeVerificationError("Owned workspace listener is not ready.")
                providers = self._provider_setting(db)
                if providers == [] or db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0] == "true":
                    return None
                if providers is None:
                    providers = available
                if not providers or any(item not in available for item in providers):
                    raise BridgeError("Hosted provider CLI is unavailable", 409)
                with owned[0].store.connection() as bus:
                    flags = dict(bus.execute("SELECT key,value FROM config"))
                    if flags.get("paused") == "true" or flags.get("auto_suspended") == "true":
                        return None
                    held = bus.execute("SELECT provider FROM agents WHERE state IN ('paused','stopped')").fetchall()
                    if any(row[0] in providers for row in held):
                        raise BridgeError("Hosted provider CLI is unavailable", 409)
                db.execute("INSERT OR IGNORE INTO settings VALUES ('hosted_providers',?)", (json.dumps(providers),))
                # Apply to this exact in-process owner under the same writer as
                # explicit OFF. An automatic HTTP request could arrive after OFF.
                owned[0].discovery[:] = candidates
                result = supervisor.configure_hosted(providers)
                self._guard_owned(owned)
                return result
        except RuntimeVerificationError:
            raise BridgeError("Workspace service ownership requires review", 409) from None

    def repair_startup(self, payload):
        """Repair readiness only; never construct a service or replay old work."""
        if (not isinstance(payload, dict) or set(payload) != {"provider"}
                or not isinstance(payload["provider"], str) or payload["provider"] not in SUPPORTED):
            raise BridgeError("Select one supported hosted provider")
        provider = payload["provider"]
        self._require_open()
        with self._lock:
            owned = self.server, self.thread, self.guard, self._ready, self._runtime
        if owned[0] is None:
            raise BridgeError("Current owned workspace listener is required", 409)
        server = owned[0]
        supervisor = server.supervisor
        with supervisor._lock, self._lock:
            try:
                self._guard_owned(owned)
            except RuntimeVerificationError:
                raise BridgeError("Current owned workspace listener is required", 409) from None
            if (self.phase != "serving" or not owned[3].is_set() or not server.native_enabled
                    or server.stopping or server._closed.is_set() or not supervisor.hosted.execution_ready()):
                raise BridgeError("Current owned workspace listener is required", 409)
            with server.store.connection() as bus:
                if not supervisor.hosted.exists(bus):
                    raise BridgeError("Exactly one existing hosted owner is required", 409)
                owners = bus.execute("SELECT * FROM avalokita_hosted_owners WHERE provider=?", (provider,)).fetchall()
                if len(owners) != 1:
                    raise BridgeError("Exactly one existing hosted owner is required", 409)
                owner = dict(owners[0])
                agent = bus.execute("SELECT * FROM agents WHERE id=?", (owner["actor"],)).fetchone()
                expected = owner, dict(agent) if agent else None
            # Match before_prompt's lock and database order. Nothing inside
            # these writers performs HTTP, acquires a lifecycle lock or starts IO.
            with self.bridge._db() as local:
                local.execute("BEGIN IMMEDIATE")
                providers = self._provider_setting(local)
                paused = local.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
                if not providers or provider not in providers or not paused or paused[0] != "false":
                    raise BridgeError("Hosted provider is disabled or paused", 409)
                with server.store.connection() as bus:
                    bus.execute("BEGIN IMMEDIATE")
                    if (self._closing.is_set() or not owned[3].is_set() or not server.serving.is_set()
                            or server.stopping or server._closed.is_set() or not server.native_enabled):
                        raise BridgeError("Current owned workspace listener is required", 409)
                    result = supervisor.repair_hosted_startup(bus, provider, expected)
                    if (self._closing.is_set() or not owned[3].is_set() or not server.serving.is_set()
                            or server.stopping or server._closed.is_set() or not server.native_enabled
                            or not supervisor.hosted.execution_ready() or supervisor._stop.is_set()):
                        raise BridgeError("Current owned workspace listener is required", 409)
                    return result

    def reply(self, payload):
        self._require_open()
        if not isinstance(payload, dict) or set(payload) != {"agent_id", "approval_id", "result"}:
            raise BridgeError("Exact hosted approval fields required")
        if any(not isinstance(payload[key], str) or not 1 <= len(payload[key]) <= 128 for key in ("agent_id", "approval_id")):
            raise BridgeError("Exact hosted identity required")
        return self.bridge.request("/api/hosted/approval", payload)

    def _dispose(self, server, thread, guard, *, cleanup_metadata=True):
        """Release retained resources even when lexical workspace paths retired."""
        try:
            if server is not None:
                try:
                    if thread is not None and thread.is_alive() and server.serving.is_set():
                        server.shutdown()
                finally:
                    try:
                        server.close()
                    finally:
                        if thread is not None and thread.ident is not None and thread is not threading.current_thread():
                            thread.join(timeout=3)
                if not cleanup_metadata:
                    return
                try:
                    self._guard_workspace()
                    from daemon import _archive_daemon_metadata, _daemon_path
                    from bridge import private_read
                    path = _daemon_path(server.store, "runtime.json")
                    expected = {"workspace_id": server.store.workspace_id, "pid": os.getpid(), "nonce": server.nonce}
                    server.store.guard_workspace()
                    value = json.loads(private_read(path, workspace_guard=server.store.guard_workspace)) if path.exists() else {}
                    if all(value.get(key) == item for key, item in expected.items()):
                        _archive_daemon_metadata(server.store, "runtime.json")
                        server.store.guard_workspace()
                        value = json.loads(private_read(path, workspace_guard=server.store.guard_workspace))
                        if all(value.get(key) == item for key, item in expected.items()):
                            server.store.guard_workspace()
                            path.unlink()
                except (BridgeError, BusError, OSError, ValueError, TypeError):
                    pass
        finally:
            if server is not None:
                unregister = getattr(server, "_avalokita_completion_unregister", None)
                if unregister is not None:
                    unregister()
                    server._avalokita_completion_unregister = None
            if guard is not None:
                guard.__exit__(None, None, None)

    def close(self):
        self.stop_service()
        with self._lock:
            if self.phase == "closed":
                return
            if self.phase != "retired":
                self.phase = "closing"
            server, thread, guard = self.server, self.thread, self.guard
            self.server = self.thread = self.guard = self._runtime = self._registered = None
        try:
            self._dispose(server, thread, guard)
        finally:
            with self._lock:
                if self.phase != "retired":
                    self.phase = "closed"
            self._service_status("retired" if self.phase == "retired" else "closed")
