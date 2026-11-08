"""Owned shutdown: isolated HTTP, Events and fake adapters; native/model input zero."""
from contextlib import ExitStack, redirect_stdout
from http.client import HTTPConnection, RemoteDisconnected
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError, BusError, BusStore, WorkspaceRetired
from daemon import BusHTTPServer
from server import Handler, Runtime, Server, main
from supervisor import Supervisor


class FakeNative:
    instances = []

    def __init__(self, provider, workspace, actor, token_file, on_event=None, on_approval=None, **options):
        self.provider, self.actor, self.on_event = provider, actor, on_event
        self.pid, self.starts, self.inputs = None, 0, 0
        self.interrupted = False
        self.entered, self.release = threading.Event(), threading.Event()
        self.session_id = "synthetic-owned-session"
        self.instances.append(self)

    def start(self, resume_id=None):
        self.starts += 1
        self.pid = 12345
        if self.on_event:
            self.on_event({"type": "session", "session_id": self.session_id})
        return self.session_id

    def turn(self, prompt, timeout=240):
        self.inputs += 1
        if self.on_event:
            self.on_event({"type": "input_attempt", "session_id": self.session_id})
            self.on_event({"type": "turn", "status": "accepted", "session_id": self.session_id,
                           "turn_id": "synthetic-owned-turn"})
        self.entered.set()
        if not self.release.wait(8):
            raise TimeoutError("Synthetic adapter gate was not released")
        if self.interrupted:
            return SimpleNamespace(success=False, needs_human=False, error=None, text="", session_id=self.session_id,
                                   status="delivery_unknown", input_attempted=True, delivery_only=False)
        return SimpleNamespace(success=True, needs_human=False, error=None, text="Selected synthetic result",
                               session_id=self.session_id, status="completed", delivery_only=False)

    def interrupt(self):
        self.interrupted = True
        self.release.set()

    def close(self):
        self.pid = None
        self.release.set()


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="avalokita-owner-shutdown-")
        self.workspace = Path(self.temp.name)
        FakeNative.instances = []
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False, hosted_factory=FakeNative,
            hosted_discovery=[{"provider": "codex", "installed": True, "managed_supported": True}])
        self.servers, self.threads, self.supervisors, self.permits = [], [], [], []
        self.handlers = set()
        self.handler_lock = threading.Lock()
        for cls in (Server, BusHTTPServer):
            original = cls.process_request_thread

            def request(server, *args, _original=original, **kwargs):
                thread = threading.current_thread()
                with self.handler_lock:
                    self.handlers.add(thread)
                try:
                    return _original(server, *args, **kwargs)
                finally:
                    with self.handler_lock:
                        self.handlers.discard(thread)
            self.enterContext(patch.object(cls, "process_request_thread", request))

    def serve(self, *, runtime=None, start=True):
        server = Server(self.workspace, runtime=runtime or self.runtime)
        self.servers.append(server)
        if start:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02},
                                      name="owner-fixture-http", daemon=True)
            self.threads.append(thread)
            thread.start()
            self.assertTrue(server._serving.wait(2))
        return server

    def spawn(self, action):
        results, errors = [], []

        def run():
            try:
                results.append(action())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run, name="owner-fixture-operation", daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread, results, errors

    def permit(self):
        release = self.runtime.try_acquire_work()
        self.assertIsNotNone(release)
        self.permits.append(release)
        return release

    def owner_body(self, server):
        return {"contract": "avalokita-owner-shutdown/1", "instance_id": server.instance_id,
                "pid": server.owner_pid, "workspace": server.owner_workspace}

    def owner(self, server, *, body=None, drop=(), add=(), raw=None):
        payload = json.dumps(self.owner_body(server) if body is None else body).encode() if raw is None else raw
        headers = [("Host", f"127.0.0.1:{server.server_port}"), ("Origin", server.url),
                   ("Sec-Fetch-Site", "same-origin"), ("X-Avalokita-Request", "1"),
                   ("X-Avalokita-Instance", server.instance_id),
                   ("Authorization", "Bearer " + server.owner_token),
                   ("Content-Type", "application/json"), ("Content-Length", str(len(payload)))]
        headers = [(key, value) for key, value in headers if key not in drop] + list(add)
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.putrequest("POST", "/api/owner/shutdown", skip_host=True, skip_accept_encoding=True)
            for key, value in headers:
                connection.putheader(key, value)
            connection.endheaders(payload)
            response = connection.getresponse()
            content = response.read()
            self.assertFalse(server.owner_token.encode() in content)
            return response.status, content
        finally:
            connection.close()

    def http(self, server, path, payload=None, *, bus=False, token=None):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        try:
            headers = {"Authorization": "Bearer " + (token or (server.store.operator_token if bus else server.token)),
                       "X-Sharebus-Request" if bus else "X-Avalokita-Request": "1"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request("GET" if payload is None else "POST", path,
                               None if payload is None else json.dumps(payload), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def tearDown(self):
        for release in self.permits:
            release()
        for adapter in FakeNative.instances:
            adapter.release.set()
        for server in self.servers:
            server.server_close()
            worker = server._shutdown_thread
            if worker is not None and worker.ident is not None:
                worker.join(5)
                self.assertFalse(worker.is_alive())
        self.runtime.stop_peer_reconciler()
        bus = self.runtime.hosted.server
        self.runtime.hosted.close()
        for supervisor in self.supervisors:
            supervisor.close()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        with self.handler_lock:
            handlers = list(self.handlers)
        for thread in handlers:
            thread.join(2)
        self.assertFalse(self.handlers)
        for server in self.servers:
            self.assertEqual(server.socket.fileno(), -1)
        if bus is not None:
            self.assertEqual(bus.socket.fileno(), -1)
            self.assertFalse(bus.supervisor._dispatcher.is_alive())
            self.assertEqual(bus.supervisor._workers, {})
        self.assertEqual(self.runtime._active_work, 0)
        self.resource_cleanup = {"open_listeners": 0, "live_owned_handlers": 0, "live_fixture_threads": 0,
                                 "active_work": 0, "owner_secret_recorded": False}
        self.temp.cleanup()
        self.assertFalse(Path(self.temp.name).exists())


class OwnerHTTPTests(_Fixture):
    def test_owner_is_distinct_and_ui_cookie_ui_bearer_peer_cannot_shutdown(self):
        server = self.serve()
        self.assertFalse(server.owner_token == server.token)
        for headers in ([('Authorization', 'Bearer ' + server.token)],
                        [('Cookie', 'avalokita=' + server.token)], [('Authorization', 'Bearer synthetic-peer')], []):
            with self.subTest(authority="non_owner"):
                self.assertEqual(self.owner(server, drop=("Authorization",), add=headers)[0], 401)
        self.assertIsNone(server._shutdown_thread)
        self.assertFalse(self.runtime._closing)

    def test_owner_rejects_duplicate_or_foreign_security_headers_and_cl_te(self):
        server = self.serve()
        cases = [((), [("Host", f"127.0.0.1:{server.server_port}")], 403),
                 ((), [("Origin", server.url)], 403), ((), [("Sec-Fetch-Site", "same-origin")], 403),
                 ((), [("X-Avalokita-Request", "1")], 403),
                 ((), [("X-Avalokita-Instance", server.instance_id)], 403),
                 ((), [("Authorization", "Bearer synthetic-peer")], 401),
                 ((), [("Content-Length", "1")], 411), ((), [("Content-Type", "application/json")], 415),
                 ((), [("Transfer-Encoding", "chunked")], 400),
                 (("Host",), [("Host", f"localhost:{server.server_port}")], 403),
                 (("Origin",), [("Origin", "https://foreign.invalid")], 403),
                 (("Sec-Fetch-Site",), [("Sec-Fetch-Site", "cross-site")], 403),
                 (("X-Avalokita-Request",), [], 403), (("X-Avalokita-Instance",), [], 403)]
        for index, (drop, add, status) in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(self.owner(server, drop=drop, add=add)[0], status)
        self.assertFalse(self.runtime._closing)
        self.assertIsNone(server._shutdown_thread)

    def test_exact_bounded_owner_body_is_required(self):
        server = self.serve()
        expected = self.owner_body(server)
        for field, value in (("contract", "other"), ("instance_id", "other"), ("pid", True),
                             ("workspace", str(self.workspace / "other")), ("extra", 1)):
            with self.subTest(field=field):
                self.assertEqual(self.owner(server, body={**expected, field: value})[0], 403)
        missing = dict(expected)
        missing.pop("pid")
        self.assertEqual(self.owner(server, body=missing)[0], 403)
        duplicate = json.dumps(expected).replace('"pid":', '"pid":0,"pid":').encode()
        self.assertEqual(self.owner(server, raw=duplicate)[0], 400)
        self.assertEqual(self.owner(server, raw=b"\xff")[0], 400)
        self.assertEqual(self.owner(server, raw=b" " * 4097)[0], 413)
        self.assertFalse(self.runtime._closing)

    def test_ack_keeps_listener_for_grace_repeat_is_one_worker_and_new_work_is_refused(self):
        server = self.serve()
        release = self.permit()
        self.assertEqual(self.owner(server)[0], 202)
        worker = server._shutdown_thread
        self.assertTrue(self.runtime._closing)
        self.assertEqual(self.owner(server)[0], 202)
        self.assertIs(server._shutdown_thread, worker)
        self.assertTrue(server._serving.is_set())
        self.assertEqual(self.http(server, "/api/control", {"action": "pause"})[0], 200)
        self.assertEqual(self.http(server, "/api/prompt", {"text": "Refused new input", "idempotency_key": "closed"})[0], 409)
        self.assertEqual(self.runtime.store.snapshot()["goals"], [])
        release()
        self.assertTrue(server._closed.wait(3))

    def test_retired_owner_shutdown_uses_no_workspace_metadata_resolve_or_database(self):
        server = self.serve()
        self.runtime.stop_peer_reconciler()
        with self.assertRaises(WorkspaceRetired):
            self.runtime._retire_workspace()
        attempts = []

        def forbidden(*args, **kwargs):
            attempts.append(True)
            raise AssertionError("Owner endpoint attempted filesystem or database access")
        with ExitStack() as stack:
            for name in ("lstat", "stat", "resolve", "exists", "open", "unlink", "mkdir", "read_text", "read_bytes"):
                stack.enter_context(patch.object(Path, name, forbidden))
            stack.enter_context(patch.object(sqlite3, "connect", forbidden))
            stack.enter_context(patch.object(self.runtime, "_guard_workspace", forbidden))
            stack.enter_context(patch.object(self.runtime, "_guard_workspace_identity", forbidden))
            self.assertEqual(self.owner(server)[0], 202)
            self.assertTrue(server._closed.wait(3))
        self.assertEqual(attempts, [])

    def test_ack_write_disconnect_still_schedules_shutdown(self):
        server = self.serve()
        original = Handler._send
        attempted = threading.Event()

        def send(handler, status, *args, **kwargs):
            if status == 202:
                attempted.set()
                raise BrokenPipeError("Injected ACK disconnect")
            return original(handler, status, *args, **kwargs)
        with patch.object(Handler, "_send", send):
            with self.assertRaises(RemoteDisconnected):
                self.owner(server)
            self.assertTrue(attempted.wait(2))
            self.assertTrue(server._closed.wait(3))
        self.assertIsNotNone(server._shutdown_thread)

    def test_unstarted_server_closes_without_shutdown_and_repeated_close_is_safe(self):
        server = self.serve(start=False)
        with patch.object(server, "shutdown", side_effect=AssertionError("Unstarted loop cannot shut down")) as shutdown:
            server.begin_closing()
            server.schedule_shutdown()
            worker = server._shutdown_thread
            self.assertTrue(server._closed.wait(2))
            server.schedule_shutdown()
            server.server_close()
            self.assertIs(server._shutdown_thread, worker)
            shutdown.assert_not_called()
        server.serve_forever()
        self.assertFalse(server._serving.is_set())

    def test_private_ready_owner_capability_and_stdout_four_field_allowlist(self):
        server = self.serve(start=False)
        ready = self.workspace / "fixture-ready.json"
        captured = {}

        def inspect_ready(*args, **kwargs):
            value = json.loads(ready.read_text(encoding="utf-8"))
            captured["owner_present"] = bool(value.get("owner_token"))
            captured["owner_matches"] = value.get("owner_token") == server.owner_token
            captured["distinct"] = value.get("owner_token") != value.get("token")
        output = io.StringIO()
        with patch("server.Server", return_value=server), patch.object(server, "serve_forever", inspect_ready), \
                patch("server.signal.signal"), patch.object(sys, "argv", ["server", "--workspace", str(self.workspace), "--ready-file", str(ready)]), \
                redirect_stdout(output):
            main()
        public = json.loads(output.getvalue())
        self.assertEqual(set(public), {"url", "pid", "workspace", "instance_id"})
        self.assertFalse(server.owner_token in output.getvalue())
        self.assertEqual(captured, {"owner_present": True, "owner_matches": True, "distinct": True})
        self.assertFalse(ready.exists())

    def test_grace_expiry_closes_owned_worker_and_keeps_unknown_attempt_without_replay(self):
        configured = self.runtime.configure_hosted({"providers": ["codex"]})
        actor = configured["sessions"][0]["agent_id"]
        bus = self.runtime.hosted.server
        self.runtime.prompt({"text": "Synthetic owned work", "idempotency_key": "owned-grace", "recipient_ids": [actor]})
        deadline = time.monotonic() + 4
        while not FakeNative.instances and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(FakeNative.instances)
        adapter = FakeNative.instances[0]
        self.assertTrue(adapter.entered.wait(3))
        server = self.serve()
        self.assertEqual(self.owner(server)[0], 202)
        self.assertFalse(server._closed.is_set())
        self.assertEqual(self.runtime._active_work, 1)
        self.assertTrue(server._closed.wait(7))
        self.assertTrue(self.runtime.wait_idle(2))
        self.assertEqual((adapter.starts, adapter.inputs), (1, 1))
        jobs = bus.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "awaiting_native_reply")
        with bus.store.connection() as db:
            attempt = db.execute("SELECT state,input_attempted FROM avalokita_hosted_attempts WHERE job_id=?", (jobs[0]["id"],)).fetchone()
            self.assertEqual(tuple(attempt), ("delivery_unknown", 1))
            self.assertEqual(db.execute("SELECT used FROM roots WHERE id=?", (jobs[0]["root_id"],)).fetchone()[0], 1)
        self.assertIsNone(bus.store.claim(actor))

    def test_granted_canonical_reason_settles_after_closing_pause_and_expiry(self):
        # Reuse only an existing isolated synthetic-process fixture; its engine,
        # executable and bindings are Temp files and never run a real provider.
        from test_canonical_reason import CanonicalReasonTests
        from adapters.reason_engine import ReasonEngineAdapter
        from native_dispatch import complete
        fixture = CanonicalReasonTests("test_ready_grant_is_durable_immutable_and_pause_does_not_block_slow_receipt_cleanup")
        fixture.setUp()
        entered, release = threading.Event(), threading.Event()
        original, calls = ReasonEngineAdapter._remember, []

        def remember(adapter, *args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                entered.set()
                self.assertTrue(release.wait(8))
            return original(adapter, *args, **kwargs)
        try:
            claim = fixture.reserve()
            with fixture.runtime._work(), patch.object(ReasonEngineAdapter, "_remember", remember):
                delivery, results, errors = self.spawn(lambda: fixture.dispatcher.deliver(claim["dispatch_id"]))
                try:
                    self.assertTrue(entered.wait(5))
                    fixture.runtime.begin_closing()
                    self.assertIsNone(fixture.runtime.try_acquire_work())
                    self.assertFalse(fixture.runtime.wait_idle(.01))
                    self.assertTrue(fixture.runtime.control("pause")["paused"])
                finally:
                    release.set()
                    delivery.join(8)
            self.assertEqual(errors, [])
            self.assertTrue(results[0]["commit_granted"])
            with fixture.runtime.store._connection(write=True) as db:
                db.execute("UPDATE goals SET deadline=0,used=budget")
            with fixture.bus.store.connection() as db:
                db.execute("UPDATE roots SET deadline=0")
            result = complete(fixture.workspace, actor=fixture.actor["id"], native_session_id=fixture.binding.native_id,
                job_id=claim["job_id"], event_id=claim["event_id"], text="Selected synthetic final after closing")
            self.assertTrue(result["completed"])
            self.assertTrue(fixture.dispatcher.deliver(claim["dispatch_id"])["duplicate_suppressed"])
            self.assertEqual(len(fixture.starts), 1)
            self.assertTrue(fixture.runtime.wait_idle(.1))
        finally:
            release.set()
            fixture.runtime.hosted.close()
            fixture.tearDown()


class RuntimeAdmissionTests(_Fixture):
    def test_transferable_release_is_idempotent_and_closing_wins_future_admission(self):
        release = self.permit()
        self.runtime.begin_closing()
        self.assertIsNone(self.runtime.try_acquire_work())
        self.assertFalse(self.runtime.wait_idle(.01))
        thread, _, errors = self.spawn(lambda: (release(), release()))
        thread.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(self.runtime.wait_idle(.1))
        self.assertEqual(self.runtime._active_work, 0)

    def test_close_refuses_prompt_config_approval_peer_and_drain_before_guard_or_io(self):
        self.runtime.begin_closing()
        with patch.object(self.runtime, "_guard_workspace", side_effect=AssertionError("New work touched storage")), \
                patch.object(self.runtime.hosted, "configure") as configure, patch.object(self.runtime.hosted, "reply") as reply:
            for action in (lambda: self.runtime.prompt({}), lambda: self.runtime.configure_hosted({"providers": []}),
                           lambda: self.runtime.reply_hosted({}), lambda: self.runtime.peer("synthetic", {"operation": "send-keys"})):
                with self.assertRaises(BridgeError) as refused:
                    action()
                self.assertEqual(refused.exception.status, 409)
            self.assertEqual(self.runtime.drain_native_peers(), [])
            configure.assert_not_called()
            reply.assert_not_called()

    def test_before_prompt_is_admitted_without_runtime_lock_and_saved_request_can_finish_after_close(self):
        entered, release, controlled = threading.Event(), threading.Event(), threading.Event()

        def prepare():
            entered.set()
            self.assertTrue(release.wait(5))
        with patch.object(self.runtime.hosted, "before_prompt", prepare):
            thread, results, errors = self.spawn(lambda: self.runtime.prompt({"text": "Synthetic already admitted input", "idempotency_key": "before-close"}))
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.runtime._active_work, 1)
                control, _, control_errors = self.spawn(lambda: (self.runtime.control("pause"), controlled.set()))
                self.assertTrue(controlled.wait(3))
                self.runtime.begin_closing()
                self.assertFalse(self.runtime.wait_idle(.01))
            finally:
                release.set()
                thread.join(6)
            control.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(control_errors, [])
        self.assertTrue(results[0]["dispatch"]["durable_accepted"])
        self.assertTrue(self.runtime.wait_idle(.1))

    def test_prompt_ipc_and_reason_deliver_outside_runtime_lock_under_one_permit(self):
        actor = {"id": "synthetic-codex", "provider": "codex", "mode": "native_attached", "state": "idle",
                 "native_session_id": "synthetic-native", "role": "peer", "capabilities": []}
        observed = {"connection": {"state": "connected"}, "state": {"agents": [actor], "paused": False}}
        for transport in ("ipc", "reason"):
            entered, release, controlled = threading.Event(), threading.Event(), threading.Event()

            def deliver(dispatch_id):
                entered.set()
                self.assertTrue(release.wait(5))
                return {"state": "delivery_unknown", "automatic_retry": False}
            dispatcher = SimpleNamespace(available_for=lambda *args: {"state": "metadata_verified"}, deliver=deliver,
                reserve=Mock(return_value={"state": "claimed", "dispatch_id": "synthetic-dispatch", "transport": transport}))
            self.runtime.native_dispatcher = dispatcher
            with patch.object(self.runtime.bridge, "observe", return_value=observed), \
                    patch.object(self.runtime.bridge, "dispatch", return_value={"state": "queued", "native_ipc_ids": [actor["id"]], "recipient_ids": [actor["id"]]}), \
                    patch.object(self.runtime.bridge, "dispatches", return_value={}), \
                    patch.object(self.runtime.bridge, "reconcile_dispatches", return_value={}):
                thread, results, errors = self.spawn(lambda: self.runtime.prompt({"text": "Synthetic transport work",
                    "idempotency_key": "transport-" + transport, "recipient_ids": [actor["id"]]}))
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertEqual(self.runtime._active_work, 1)
                    control, _, control_errors = self.spawn(lambda: (self.runtime.control("pause"), controlled.set()))
                    self.assertTrue(controlled.wait(3))
                finally:
                    release.set()
                    thread.join(6)
                control.join(3)
            self.assertEqual(errors + control_errors, [])
            self.assertFalse(results[0]["native_delivery"][0]["automatic_retry"])
            self.assertTrue(self.runtime.wait_idle(.1))
            self.runtime.control("resume")

    def test_peer_delivery_is_admitted_outside_runtime_lock_and_new_nested_peer_is_refused(self):
        entered, release, controlled = threading.Event(), threading.Event(), threading.Event()

        def deliver(dispatch_id):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"state": "delivery_unknown", "automatic_retry": False}
        self.runtime.native_dispatcher = SimpleNamespace(deliver=deliver)
        with patch.object(self.runtime, "_peer", return_value={"state": "claimed", "dispatch_id": "synthetic", "duplicate_suppressed": False}):
            thread, results, errors = self.spawn(lambda: self.runtime.peer("synthetic", {"operation": "send-keys"}))
            try:
                self.assertTrue(entered.wait(3))
                control, _, control_errors = self.spawn(lambda: (self.runtime.control("pause"), controlled.set()))
                self.assertTrue(controlled.wait(3))
                self.runtime.begin_closing()
                with self.assertRaises(BridgeError):
                    self.runtime.peer("synthetic", {"operation": "send-keys"})
                self.assertEqual(self.runtime._active_work, 1)
            finally:
                release.set()
                thread.join(6)
            control.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(control_errors, [])
        self.assertFalse(results[0]["native_delivery"]["automatic_retry"])

    def test_malformed_peer_operation_is_rejected_before_authority_or_delivery(self):
        deliver = Mock(side_effect=AssertionError("Malformed operation reached native delivery"))
        self.runtime.native_dispatcher = SimpleNamespace(deliver=deliver)
        with patch.object(self.runtime.bridge, "paused", side_effect=AssertionError("Malformed operation reached authority")), \
                patch.object(self.runtime.bridge, "authenticate_peer") as authenticate:
            for operation in (None, {}, [], 1, True, "unknown"):
                with self.subTest(operation=operation):
                    with self.assertRaises(BridgeError) as refused:
                        self.runtime.peer("synthetic", {"operation": operation})
                    self.assertEqual(refused.exception.status, 400)
            authenticate.assert_not_called()
        deliver.assert_not_called()
        self.assertEqual(self.runtime._active_work, 0)

    def test_claimed_peer_without_dispatcher_refuses_before_input_and_releases_permit(self):
        self.runtime.native_dispatcher = None
        value = {"state": "claimed", "dispatch_id": "synthetic", "duplicate_suppressed": False}
        with patch.object(self.runtime, "_peer", return_value=value) as peer:
            with self.assertRaises(BridgeError) as refused:
                self.runtime.peer("synthetic", {"operation": "send-keys"})
        self.assertEqual(refused.exception.status, 409)
        peer.assert_called_once_with("synthetic", {"operation": "send-keys"})
        self.assertEqual(value["state"], "claimed")
        self.assertNotIn("native_delivery", value)
        self.assertEqual(self.runtime._active_work, 0)

    def test_drain_is_one_admission_nonblocking_serialized_and_outside_runtime_lock(self):
        entered, release, controlled, duplicate = (threading.Event() for _ in range(4))
        limits = []

        def drain(*, limit):
            limits.append(limit)
            entered.set()
            self.assertTrue(release.wait(5))
            return [{"state": "synthetic_unknown", "automatic_retry": False}]
        self.runtime.native_dispatcher = SimpleNamespace(drain=drain)
        thread, results, errors = self.spawn(self.runtime.drain_native_peers)
        try:
            self.assertTrue(entered.wait(3))
            other, other_results, other_errors = self.spawn(lambda: (self.runtime.drain_native_peers(), duplicate.set()))
            self.assertTrue(duplicate.wait(2))
            control, _, control_errors = self.spawn(lambda: (self.runtime.control("pause"), controlled.set()))
            self.assertTrue(controlled.wait(3))
            self.runtime.begin_closing()
            self.assertFalse(self.runtime.wait_idle(.01))
        finally:
            release.set()
            thread.join(6)
        other.join(2)
        control.join(3)
        self.assertEqual(limits, [1])
        self.assertEqual(other_results[0][0], [])
        self.assertEqual(errors + other_errors + control_errors, [])
        self.assertFalse(results[0][0]["automatic_retry"])

    def test_owned_bus_callback_is_present_before_dispatch_start_and_intake_rejects_after_close(self):
        original = Supervisor.start
        present = []

        def start(supervisor):
            present.append(supervisor.acquire_work is not None)
            return original(supervisor)
        with patch.object(Supervisor, "start", start):
            self.runtime.hosted._ensure_bus()
        bus = self.runtime.hosted.server
        self.assertEqual(present, [True])
        self.assertTrue(bus.serving.wait(2))
        with self.runtime._work_condition:
            active = self.runtime._active_work
            release = bus.acquire_work()
            self.assertTrue(callable(release))
            self.permits.append(release)
            self.assertEqual(self.runtime._active_work, active + 1)
            release()
            release()
            self.assertEqual(self.runtime._active_work, active)
        self.runtime.begin_closing()
        self.assertIsNone(bus.acquire_work())
        for path, payload in (("/api/hosted", {"providers": []}),
                              ("/api/hosted/approval", {"agent_id": "synthetic", "approval_id": "synthetic", "result": {}})):
            self.assertEqual(self.http(bus, path, payload, bus=True)[0], 409)
        actor, token = bus.store.register("codex", "Synthetic external", [], "external_attached", native_session_id="synthetic-session")
        # Settlement keeps its original authentication path; closing does not
        # transform this bad correlation into a new-work refusal.
        with patch.object(bus.store, "native_result", return_value={"settlement_checked": True}) as settle:
            status, body = self.http(bus, "/api/native/result", {"native_session_id": "synthetic-session", "last_assistant_message": "Selected result"}, bus=True, token=token)
        self.assertEqual(status, 200)
        self.assertTrue(body["settlement_checked"])
        self.assertEqual(settle.call_args.args[0], actor["id"])

    def test_runtime_to_bus_close_race_reports_partial_configuration_without_starting_worker(self):
        self.runtime.hosted._ensure_bus()
        original = self.runtime.bridge.request

        def request(path, payload=None):
            if path == "/api/hosted" and payload is not None:
                self.runtime.begin_closing()
            return original(path, payload)
        with patch.object(self.runtime.bridge, "request", request):
            result = self.runtime.configure_hosted({"providers": ["codex"]})
        self.assertEqual(self.runtime.hosted.providers(), ["codex"])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(self.runtime.hosted.server.supervisor.hosted_state()["sessions"], [])
        self.assertEqual(FakeNative.instances, [])

    def test_waiting_recovery_cannot_start_another_prompt_after_closing(self):
        goal = self.runtime.store.new_goal("Synthetic saved request", idempotency_key="saved-before-close")
        receipt = {"state": "waiting", "request_budget": 1, "request_ttl": 30}
        self.runtime.begin_closing()
        with patch.object(self.runtime.bridge, "observe", return_value={"connection": {"state": "connected"}}), \
                patch.object(self.runtime.bridge, "dispatches", return_value={goal["id"]: receipt}), \
                patch.object(self.runtime.store, "new_goal", side_effect=AssertionError("Closing recovery created work")) as create:
            self.runtime._recover_waiting()
        create.assert_not_called()
        self.assertEqual(self.runtime._active_work, 0)


class SupervisorAdmissionTests(_Fixture):
    def supervisor(self):
        store = BusStore(self.workspace)
        actor, _ = store.register("codex", "Synthetic managed", [], "managed_new")
        supervisor = Supervisor(store, [], adapter_factory=FakeNative, acquire_work=self.runtime.try_acquire_work)
        self.supervisors.append(supervisor)
        return store, actor, supervisor

    def test_no_job_and_all_claim_errors_return_the_permit_once(self):
        store, actor, _ = self.supervisor()
        for error in (None, BusError("Synthetic refusal", 503), sqlite3.OperationalError("locked"), OSError("storage"), ValueError("unexpected")):
            supervisor = Supervisor(store, [], acquire_work=self.runtime.try_acquire_work)
            self.supervisors.append(supervisor)

            def claim(actor_id):
                supervisor._stop.set()
                if error is not None:
                    raise error
                return None
            with patch.object(store, "claim", claim):
                supervisor.wake()
                if type(error) is ValueError:
                    with self.assertRaises(ValueError) as refused:
                        supervisor._dispatch()
                    self.assertIs(refused.exception, error)
                else:
                    supervisor._dispatch()
            self.assertEqual(self.runtime._active_work, 0)
        self.assertEqual(supervisor._workers, {})

    def test_postclaim_stop_preserves_one_reservation_and_starts_no_worker(self):
        store, actor, supervisor = self.supervisor()
        sent = store.send("user", actor["id"], "Synthetic reserved input", "task")
        original = store.claim

        def claim(actor_id):
            result = original(actor_id)
            supervisor._stop.set()
            return result
        with patch.object(store, "claim", claim):
            supervisor.wake()
            supervisor._dispatch()
        jobs = store.jobs()
        self.assertEqual([(job["id"], job["state"]) for job in jobs], [(sent["jobs"][0]["id"], "running")])
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT used FROM roots WHERE id=?", (jobs[0]["root_id"],)).fetchone()[0], 1)
        self.assertEqual(supervisor._workers, {})
        self.assertEqual(FakeNative.instances, [])
        self.assertEqual(self.runtime._active_work, 0)
        self.assertIsNone(store.claim(actor["id"]))

    def test_thread_start_failure_returns_permit_without_refund_requeue_or_native_attempt(self):
        store, actor, supervisor = self.supervisor()
        store.send("user", actor["id"], "Synthetic reserved input", "task")

        def fail_start(thread):
            supervisor._stop.set()
            raise RuntimeError("Injected worker start failure")
        with patch.object(threading.Thread, "start", fail_start):
            supervisor.wake()
            with self.assertRaises(RuntimeError):
                supervisor._dispatch()
        self.assertEqual(self.runtime._active_work, 0)
        self.assertEqual(supervisor._workers, {})
        self.assertEqual(supervisor._busy, set())
        self.assertEqual(store.jobs()[0]["state"], "running")
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT used FROM roots").fetchone()[0], 1)
        self.assertEqual(FakeNative.instances, [])
        self.assertIsNone(store.claim(actor["id"]))

    def test_admission_precedes_physical_worker_start_and_release_follows_cleanup(self):
        store, actor, supervisor = self.supervisor()
        store.send("user", actor["id"], "Synthetic admitted input", "task")
        original = threading.Thread.start
        ordered = []

        def start(thread):
            if thread.name == "sharebus-codex":
                ordered.append(("before_close", self.runtime._active_work))
                self.runtime.begin_closing()
                ordered.append(("physical_start_after_close", self.runtime._active_work))
            return original(thread)
        with patch.object(threading.Thread, "start", start):
            supervisor.start()
            deadline = time.monotonic() + 4
            while not FakeNative.instances and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(FakeNative.instances)
            adapter = FakeNative.instances[0]
            self.assertTrue(adapter.entered.wait(3))
            self.assertEqual(self.runtime._active_work, 1)
            self.assertIsNone(self.runtime.try_acquire_work())
            adapter.release.set()
            self.assertTrue(self.runtime.wait_idle(5))
        self.assertEqual(ordered, [("before_close", 1), ("physical_start_after_close", 1)])
        self.assertEqual((adapter.starts, adapter.inputs), (1, 1))
        self.assertEqual(supervisor._workers, {})
        self.assertEqual(supervisor._busy, set())
        self.assertEqual(store.jobs()[0]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
