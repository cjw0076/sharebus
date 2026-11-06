"""Owned HTTP and retained-worker fixtures; no native process or model input."""
from contextlib import ExitStack, closing, contextmanager
from http.client import HTTPConnection
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch
from urllib.parse import urlsplit
from urllib.request import url2pathname

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from bridge import BridgeError, BusError, BusStore, SharebusBridge, WorkspaceRetired, private_read
import daemon
from harmony import HarmonyError, HarmonyStore
from hosted_runtime import HostedLedger
from native_dispatch import NativeDispatcher
from server import Runtime, _cleanup_ready


class BarrierNative:
    instances = []

    def __init__(self, provider, workspace, actor, token_file, *, on_event, on_approval, **options):
        self.provider, self.actor = provider, actor
        self.on_event, self.on_approval = on_event, on_approval
        self.pid = 12345
        self.entered, self.release = threading.Event(), threading.Event()
        self.closed = False
        self.session_id = "fixture-owned-session"
        self.instances.append(self)

    def start(self, resume_id=None):
        self.on_event({"type": "session", "session_id": self.session_id})
        return self.session_id

    def turn(self, prompt, timeout=240):
        self.on_event({"type": "input_attempt", "session_id": self.session_id})
        self.on_event({"type": "turn", "status": "accepted", "session_id": self.session_id, "turn_id": "fixture-turn"})
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError("Fixture barrier was not released")
        self.pid = None
        return SimpleNamespace(text="selected fixture result", success=True, session_id=self.session_id,
                               status="completed", delivery_only=False)

    def interrupt(self):
        self.release.set()

    def close(self):
        self.closed = True
        self.pid = None
        self.release.set()


class OwnedRetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="avalokita-owned-retirement-")
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.old = self.workspace.with_name("original-workspace")
        self.buses = []
        self.handlers = set()
        self.handler_lock = threading.Lock()
        original_init = daemon.BusHTTPServer.__init__
        original_request = daemon.BusHTTPServer.process_request_thread

        def constructed(server, *args, **kwargs):
            original_init(server, *args, **kwargs)
            self.buses.append(server)

        def request(server, *args, **kwargs):
            thread = threading.current_thread()
            with self.handler_lock:
                self.handlers.add(thread)
            try:
                return original_request(server, *args, **kwargs)
            finally:
                with self.handler_lock:
                    self.handlers.discard(thread)

        self.enterContext(patch.object(daemon.BusHTTPServer, "__init__", constructed))
        self.enterContext(patch.object(daemon.BusHTTPServer, "process_request_thread", request))
        BarrierNative.instances = []
        self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False,
            hosted_factory=BarrierNative,
            hosted_discovery=[{"provider": "codex", "installed": True, "managed_supported": True}])
        self.runtimes = [self.runtime]

    def tearDown(self):
        for adapter in BarrierNative.instances:
            adapter.release.set()
        for runtime in self.runtimes:
            runtime.hosted.close()
        with self.handler_lock:
            handlers = list(self.handlers)
        for handler in handlers:
            handler.join(timeout=2)
        self.assertFalse(self.handlers)
        for bus in self.buses:
            self.assertFalse(bus.serving.is_set())
            self.assertEqual(bus.socket.fileno(), -1)
            self.assertFalse(bus.supervisor._dispatcher.is_alive())
            self.assertEqual(bus.supervisor._workers, {})
        self.resource_cleanup = {"buses_closed": len(self.buses), "open_listener_sockets": 0,
                                 "live_http_handlers": 0, "live_dispatchers": 0, "live_workers": 0}
        self.temp.cleanup()
        self.assertFalse(Path(self.temp.name).exists())

    @contextmanager
    def fixture_lease(self, store):
        # Windows' real open lease prevents rename. Root-swap cases explicitly
        # replace only that lease; HTTP, identity guards, callbacks and DB are real.
        yield

    def bus(self, *, fixture_lease=False):
        if fixture_lease:
            with patch("daemon.workspace_guard", self.fixture_lease):
                self.runtime.hosted._ensure_bus()
        else:
            self.runtime.hosted._ensure_bus()
        bus = self.runtime.hosted.server
        self.assertTrue(bus.serving.wait(2))
        self.assertIs(bus.store, bus.supervisor.store)
        return bus

    def replace(self, bus):
        # The stat/open gap is not an OS-atomic guarantee. Quiesce the idle
        # dispatcher before this deliberate path swap; an in-flight worker and
        # actual HTTP remain retained for post-observation fencing assertions.
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        self.assertFalse(bus.supervisor._dispatcher.is_alive())
        self.workspace.rename(self.old)
        self.workspace.mkdir()
        replacement = BusStore(self.workspace)
        with replacement.connection() as db:
            db.execute("UPDATE config SET value=? WHERE key='workspace_id'", (bus.store.workspace_id,))
        sentinel = self.workspace / "replacement.txt"
        sentinel.write_text("replacement root; do not open its state", encoding="utf-8")
        return replacement

    def request(self, bus, path, payload=None):
        connection = HTTPConnection("127.0.0.1", bus.server_port, timeout=2)
        try:
            headers = {"Authorization": "Bearer " + bus.store.operator_token, "X-Sharebus-Request": "1"}
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request("POST" if payload is not None else "GET", path,
                               body=json.dumps(payload) if payload is not None else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    @contextmanager
    def forbid_workspace_io(self, *, after=None, include_metadata=False):
        """Count attempts, even if an exception would have hidden the mutation."""
        attempts = []
        root = self.workspace

        def check(operation, value):
            if after is not None and not after():
                return
            if isinstance(value, int):
                return
            try:
                if isinstance(value, str) and value.startswith("file:"):
                    value = url2pathname(urlsplit(value).path)
                path = Path(value).absolute()
            except (TypeError, ValueError):
                return
            if path == root or path.is_relative_to(root):
                attempts.append((operation, str(path.relative_to(root))))
                raise AssertionError("Retired workspace I/O attempted: " + operation)

        with ExitStack() as stack:
            operations = [(Path, "open"), (Path, "unlink"), (Path, "mkdir"),
                          (os, "open"), (os, "chmod"), (sqlite3, "connect")]
            if include_metadata:
                operations.extend((Path, name) for name in ("stat", "lstat", "exists", "is_file", "is_dir", "is_symlink", "resolve", "read_bytes", "read_text"))
            for target, name in operations:
                original = getattr(target, name)

                def checked(value, *args, _original=original, _name=name, **kwargs):
                    check(_name, value)
                    return _original(value, *args, **kwargs)

                stack.enter_context(patch.object(target, name, checked))
            original_replace = os.replace

            def replace(source, target, *args, **kwargs):
                check("replace-source", source)
                check("replace-target", target)
                return original_replace(source, target, *args, **kwargs)

            stack.enter_context(patch.object(os, "replace", replace))
            yield attempts
        self.assertEqual(attempts, [])

    def test_constructor_guard_runs_before_creating_any_bus_state(self):
        untouched = Path(self.temp.name) / "untouched"
        untouched.mkdir()

        def retired():
            raise WorkspaceRetired()

        with self.assertRaisesRegex(BusError, "runtime is retired") as error:
            BusStore(untouched, workspace_guard=retired)
        self.assertNotIn("private failure", str(error.exception))
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(list(untouched.iterdir()), [])

    def test_captured_component_identity_mismatch_denies_before_owned_store_creation(self):
        device, inode = self.runtime.bridge._workspace_identity
        self.runtime.bridge._workspace_identity = (device, inode + 1)
        with self.assertRaises(WorkspaceRetired):
            self.runtime.hosted._ensure_bus()
        self.assertFalse((self.workspace / ".sharebus").exists())
        self.assertTrue(self.runtime._workspace_retired)
        self.assertTrue(self.runtime.store._workspace_retired)
        self.assertTrue(self.runtime.bridge._workspace_retired)

    def test_owned_http_and_files_refuse_same_uuid_replacement_and_close(self):
        bus = self.bus(fixture_lease=True)
        replacement = self.replace(bus)
        before = replacement.db_path.read_bytes()
        with self.assertRaises((BridgeError, HarmonyError)):
            self.runtime._guard_workspace()
        with self.forbid_workspace_io():
            for path, payload in (("/health", None), ("/api/state", None), ("/api/policy", None),
                                  ("/api/artifacts/missing", None), ("/api/avalokita/peer", {}),
                                  ("/api/native/result", {}), ("/api/policy", {}), ("/api/auth", {})):
                self.assertEqual(self.request(bus, path, payload)[0], 403, path)
            operations = [bus.store.agents, lambda: bus.store.authenticate(bus.store.operator_token),
                lambda: bus.store._write_auto_mirror(), lambda: bus.store._manifest("missing"),
                lambda: bus.store.write_auto_private(bus.store.private / "auto/test.json", {}),
                lambda: bus.store.safe_source("replacement.txt"),
                lambda: daemon._daemon_path(bus.store, "runtime.json"),
                lambda: daemon._archive_daemon_metadata(bus.store, "runtime.json"),
                lambda: daemon._write_daemon_metadata(bus.store, "runtime.json", {}),
                lambda: daemon._startup_log(self.workspace, "must-not-log", store=bus.store),
                lambda: self.runtime.peer("fixture-token", {})]
            for operation in operations:
                with self.assertRaises((BusError, BridgeError, HarmonyError)):
                    operation()
            bus._discover("none")
            self.runtime.hosted.close()
            self.runtime.hosted.close()
        self.assertEqual(replacement.db_path.read_bytes(), before)
        self.assertFalse(bus.serving.is_set())
        self.assertFalse(bus.supervisor._dispatcher.is_alive())
        self.assertEqual(self.runtime.hosted.phase, "retired")

    def test_harmony_bridge_or_ledger_latch_fences_restored_original_and_callbacks(self):
        for component in ("harmony", "bridge", "ledger"):
            with self.subTest(component=component):
                if component != "harmony":
                    self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False, hosted_discovery=[])
                    self.runtimes.append(self.runtime)
                bus = self.bus(fixture_lease=True)
                bus.supervisor._stop.set()
                bus.supervisor.wake()
                bus.supervisor._dispatcher.join(timeout=2)
                self.workspace.rename(self.old)
                self.workspace.mkdir()
                with self.assertRaises((BridgeError, HarmonyError, BusError)):
                    if component == "harmony":
                        self.runtime.store._check_database_files()
                    elif component == "bridge":
                        self.runtime.bridge._guard_workspace()
                    else:
                        bus.supervisor.hosted.guard()
                self.workspace.rmdir()
                self.old.rename(self.workspace)
                with self.forbid_workspace_io():
                    self.assertEqual(self.request(bus, "/health")[0], 403)
                    self.assertEqual(self.request(bus, "/api/avalokita/peer", {})[0], 403)
                    with self.assertRaises(BusError):
                        bus.supervisor.hosted.process("fixture-actor", None)
                    self.runtime.hosted.close()
                    with self.assertRaises((BridgeError, BusError)):
                        self.runtime.hosted.configure({"providers": []})
                    for context in (self.runtime.bridge._db, self.runtime.store._connection):
                        with self.assertRaises((BridgeError, BusError)):
                            with context():
                                pass
                self.assertEqual(self.runtime.hosted.phase, "retired")

    def test_commit_guard_rolls_back_and_retirement_is_permanent(self):
        root = Path(self.temp.name) / "transaction"
        root.mkdir()
        changed = False

        def guard():
            if changed:
                raise WorkspaceRetired()

        store = BusStore(root, workspace_guard=guard)
        with self.assertRaises(BusError):
            with store.connection() as db:
                db.execute("INSERT INTO config VALUES ('must_rollback','one')")
                changed = True
        changed = False
        with patch("store.sqlite3.connect", side_effect=AssertionError("must not reopen")):
            with self.assertRaises(BusError):
                store.agents()
        with closing(sqlite3.connect(store.db_path)) as db:
            self.assertIsNone(db.execute("SELECT value FROM config WHERE key='must_rollback'").fetchone())

    def test_root_permission_and_unexpected_callback_errors_fail_without_retiring(self):
        bus = self.bus()
        original = Path.lstat

        def denied(path):
            if path == self.workspace:
                raise PermissionError("fixture permission failure")
            return original(path)

        with patch.object(Path, "lstat", denied):
            with self.assertRaises(BusError) as error:
                bus.store.agents()
            self.assertEqual(error.exception.status, 503)
        self.assertFalse(bus.store.workspace_retired)
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse(self.runtime.store._workspace_retired)
        self.assertFalse(self.runtime.bridge._workspace_retired)
        self.assertNotEqual(self.runtime.hosted.phase, "retired")
        self.assertEqual(bus.store.agents(), [])
        original_guard = bus.store._workspace_guard
        with patch.object(bus.store, "_workspace_guard", side_effect=RuntimeError("private callback detail")):
            with self.assertRaises(BusError) as error:
                bus.store.agents()
            self.assertEqual(error.exception.status, 503)
            self.assertNotIn("private callback", str(error.exception))
        self.assertIs(bus.store._workspace_guard, original_guard)
        self.assertFalse(bus.store.workspace_retired)
        self.assertEqual(bus.store.agents(), [])

    def test_draft_one_shot_pre_replace_refusal_preserves_error_without_cleanup_io(self):
        refusal = ValueError("fixture draft guard refusal")
        original_guard = self.runtime._guard_workspace_identity
        original_fsync = os.fsync
        armed, refused = False, False

        def fsync(descriptor):
            nonlocal armed
            original_fsync(descriptor)
            armed = True

        def guard():
            nonlocal refused
            if armed and not refused:
                refused = True
                raise refusal
            return original_guard()

        value = {"text": "unsent fixture text", "reply": None, "pending": None}
        with self.forbid_workspace_io(after=lambda: refused, include_metadata=True):
            with patch.object(self.runtime, "_guard_workspace_identity", guard), patch("os.fsync", fsync):
                with self.assertRaises(ValueError) as error:
                    self.runtime.draft(value)
                self.assertIs(error.exception, refusal)
        self.assertTrue(refused)
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse((self.runtime.bridge.receipts.parent / "draft.json").exists())
        self.assertEqual(len(list(self.runtime.bridge.receipts.parent.glob(".draft-*.tmp"))), 1)

    def test_draft_read_stops_before_stat_when_shared_retirement_is_observed(self):
        value = {"text": "unsent fixture text", "reply": None, "pending": None}
        self.runtime.draft(value)
        target = self.runtime.bridge.receipts.parent / "draft.json"
        original_exists = Path.exists
        observed = False

        def exists(path):
            nonlocal observed
            result = original_exists(path)
            if path == target:
                self.runtime._workspace_retired = True
                observed = True
            return result

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(Path, "exists", exists):
                with self.assertRaises(WorkspaceRetired):
                    self.runtime.draft()
        self.assertTrue(observed)

    def test_harmony_nested_path_refusal_keeps_temp_and_preserves_original_error(self):
        store = self.runtime.store
        store.register_pane("fixture-pane", "codex", "fixture role", "fixture-native", [], verified=True)
        root = store.new_goal("fixture cooperating commit", idempotency_key="nested-commit")["root_id"]
        lease = store.claim("fixture-pane", ["fixture.txt"], root_id=root)
        original = store._path
        refusal = ValueError("fixture nested path guard refusal")
        calls, refused = 0, False

        def path(*args, **kwargs):
            nonlocal calls, refused
            calls += 1
            if calls == 3:
                refused = True
                raise refusal
            return original(*args, **kwargs)

        with self.forbid_workspace_io(after=lambda: refused, include_metadata=True):
            with patch.object(store, "_path", path):
                with self.assertRaises(ValueError) as error:
                    store.commit_if_current("fixture-pane", lease["lease_id"], lease["fence"], "fixture.txt", None,
                                            "fixture bounded content", root_id=root)
                self.assertIs(error.exception, refusal)
        self.assertTrue(refused)
        self.assertFalse((self.workspace / "fixture.txt").exists())
        self.assertEqual(len(list(self.workspace.glob(".avalokita-write-*.tmp"))), 1)
        self.assertFalse(self.runtime._workspace_retired)

    def test_ready_publication_guard_failure_closes_retained_server_without_cleanup_io(self):
        import server as service
        server = service.Server(self.workspace, runtime=self.runtime)
        self.addCleanup(server.server_close)
        target = self.workspace / "ready-publication.json"
        original_guard = self.runtime._guard_workspace_identity
        original_open = Path.open
        refusal = ValueError("fixture publication guard refusal")
        armed, refused = False, False

        def opened(path, *args, **kwargs):
            nonlocal armed
            stream = original_open(path, *args, **kwargs)
            if path == target:
                armed = True
            return stream

        def guard():
            nonlocal refused
            if armed and not refused:
                refused = True
                raise refusal
            return original_guard()

        arguments = ["server.py", "--workspace", str(self.workspace), "--ready-file", str(target)]
        with self.forbid_workspace_io(after=lambda: refused, include_metadata=True):
            with patch.object(service, "Server", return_value=server), patch.object(sys, "argv", arguments), \
                    patch.object(Path, "open", opened), patch.object(self.runtime, "_guard_workspace_identity", guard):
                with self.assertRaises(ValueError) as error:
                    service.main()
                self.assertIs(error.exception, refusal)
        self.assertTrue(refused)
        self.assertEqual(server.socket.fileno(), -1)
        self.assertEqual(target.stat().st_size, 0)
        self.assertEqual(self.runtime.hosted.phase, "closed")

    def test_harmony_nested_shared_retirement_stops_temp_cleanup(self):
        store = self.runtime.store
        store.register_pane("fixture-pane", "codex", "fixture role", "fixture-native", [], verified=True)
        root = store.new_goal("fixture retired commit", idempotency_key="retired-commit")["root_id"]
        lease = store.claim("fixture-pane", ["retired.txt"], root_id=root)
        original = store._path
        calls, observed = 0, False

        def path(*args, **kwargs):
            nonlocal calls, observed
            calls += 1
            if calls == 3:
                self.runtime._workspace_retired = True
                observed = True
            return original(*args, **kwargs)

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(store, "_path", path):
                with self.assertRaises(WorkspaceRetired):
                    store.commit_if_current("fixture-pane", lease["lease_id"], lease["fence"], "retired.txt", None,
                                            "fixture bounded content", root_id=root)
        self.assertTrue(observed)
        self.assertFalse((self.workspace / "retired.txt").exists())
        self.assertEqual(len(list(self.workspace.glob(".avalokita-write-*.tmp"))), 1)

    def test_optional_sidecar_disappearance_is_allowed_but_permission_error_is_not(self):
        bus = self.bus()
        optional = {Path(str(path) + suffix) for path in (self.runtime.store.db_path,
                    self.runtime.bridge.receipts, bus.store.db_path) for suffix in ("-wal", "-shm")}
        original_lstat, original_exists = Path.lstat, Path.exists

        def absent(path):
            if path in optional:
                raise FileNotFoundError("fixture sidecar vanished")
            return original_lstat(path)

        def exists(path):
            if path in optional:
                raise AssertionError("Optional sidecars must use one lstat, not exists then stat")
            return original_exists(path)

        checks = (self.runtime.store._check_database_files, self.runtime.bridge._guard_workspace,
                  self.runtime.bridge._guard_pinned_database, bus.store._check_database_files)
        with patch.object(Path, "lstat", absent), patch.object(Path, "exists", exists):
            for check in checks:
                check()

        def denied(path):
            if path in optional:
                raise PermissionError("fixture sidecar permission")
            return original_lstat(path)

        with patch.object(Path, "lstat", denied):
            for check in checks:
                with self.assertRaises((HarmonyError, BridgeError, BusError)) as error:
                    check()
                self.assertEqual(error.exception.status, 503)
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse(bus.store.workspace_retired)
        for check in checks:
            check()

    def test_operational_main_databases_cannot_be_recreated_when_missing(self):
        bus = self.bus()
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        paths = ((self.runtime.store.db_path, self.runtime.store._connection),
                 (self.runtime.bridge.receipts, self.runtime.bridge._db),
                 (bus.store.db_path, bus.store.connection))
        for path, connection in paths:
            with self.subTest(database=path.name):
                saved = path.with_suffix(".preserved")
                path.rename(saved)
                try:
                    with patch("sqlite3.connect", side_effect=AssertionError("Missing operational DB must not be opened")) as connect:
                        with self.assertRaises((HarmonyError, BridgeError, BusError)):
                            with connection():
                                pass
                        connect.assert_not_called()
                    self.assertFalse(path.exists())
                    self.assertFalse(self.runtime._workspace_retired)
                    self.assertFalse(bus.store.workspace_retired)
                finally:
                    saved.rename(path)
        self.assertEqual(bus.store.agents(), [])

    def test_storage_busy_and_hardlink_errors_do_not_retire_original_workspace(self):
        bus = self.bus()
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        with patch("sqlite3.connect", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                bus.store.agents()
        self.assertFalse(bus.store.workspace_retired)
        self.assertFalse(self.runtime._workspace_retired)
        alias = self.workspace / "fixture-hardlink"
        os.link(bus.store.db_path, alias)
        try:
            with self.assertRaises(BusError) as error:
                bus.store.agents()
            self.assertEqual(error.exception.status, 403)
        finally:
            alias.unlink()
        self.assertFalse(bus.store.workspace_retired)
        self.assertFalse(self.runtime._workspace_retired)
        self.assertEqual(bus.store.agents(), [])

    def test_pinned_bridge_post_open_failure_closes_retained_connection(self):
        self.bus()
        original = sqlite3.connect
        retained = []

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            retained.append(connection)
            return connection

        calls = 0
        original_guard = self.runtime.bridge._guard_workspace

        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise BridgeError("fixture post-open storage failure", 503)
            return original_guard()

        with patch("sqlite3.connect", connect), patch.object(self.runtime.bridge, "_guard_workspace", guard):
            with self.assertRaises(BridgeError):
                self.runtime.bridge._pinned_db(read_only=True)
        self.assertEqual(len(retained), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            retained[0].execute("SELECT 1")
        self.assertFalse(self.runtime._workspace_retired)

    def test_bridge_own_storage_ignores_bus_sidecars_until_pinned_access(self):
        bus = self.bus()
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        invalid = Path(str(bus.store.db_path) + "-wal")
        original = Path.lstat

        def lstat(path):
            if path == invalid:
                return SimpleNamespace(st_mode=0o100600, st_nlink=2, st_file_attributes=0)
            return original(path)

        with patch.object(Path, "lstat", lstat):
            self.assertFalse(self.runtime.bridge.paused())
            with patch("sqlite3.connect", side_effect=AssertionError("Invalid pinned files must not be opened")) as connect:
                with self.assertRaisesRegex(BridgeError, "regular nonlinked"):
                    self.runtime.bridge._pinned_db()
                connect.assert_not_called()
        self.assertFalse(self.runtime._workspace_retired)

    def test_pinned_transaction_early_return_checks_then_commits(self):
        self.bus()
        bridge = self.runtime.bridge
        with closing(bridge._pinned_db()) as db:
            with patch.object(bridge, "_guard_pinned_database", wraps=bridge._guard_pinned_database) as guard:
                def early_return():
                    with bridge._pinned_transaction(db):
                        db.execute("INSERT INTO config VALUES ('fixture_early_return','committed')")
                        return "returned"

                self.assertEqual(early_return(), "returned")
                self.assertEqual(guard.call_count, 2)
            self.assertFalse(db.in_transaction)
        with closing(bridge._pinned_db(read_only=True)) as db:
            self.assertEqual(db.execute("SELECT value FROM config WHERE key='fixture_early_return'").fetchone()[0], "committed")

    def test_pinned_transaction_one_shot_precommit_refusal_rolls_back(self):
        self.bus()
        bridge = self.runtime.bridge
        refusal = ValueError("fixture one-shot storage refusal")
        original = bridge._guard_pinned_database
        calls = 0

        def guard():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise refusal
            original()

        with closing(bridge._pinned_db()) as db:
            with patch.object(bridge, "_guard_pinned_database", guard):
                with self.assertRaises(ValueError) as error:
                    with bridge._pinned_transaction(db):
                        db.execute("INSERT INTO config VALUES ('fixture_precommit','must rollback')")
                self.assertIs(error.exception, refusal)
                self.assertEqual(calls, 2)
            self.assertFalse(db.in_transaction)
            self.assertIsNone(db.execute("SELECT value FROM config WHERE key='fixture_precommit'").fetchone())
        self.assertFalse(self.runtime._workspace_retired)

    def test_pinned_transaction_body_error_is_preserved_without_exit_guard(self):
        self.bus()
        bridge = self.runtime.bridge
        original = OSError("fixture body failure")
        with closing(bridge._pinned_db()) as db:
            with patch.object(bridge, "_guard_pinned_database", wraps=bridge._guard_pinned_database) as guard:
                with self.assertRaises(OSError) as error:
                    with bridge._pinned_transaction(db):
                        db.execute("INSERT INTO config VALUES ('fixture_body_failure','must rollback')")
                        raise original
                self.assertIs(error.exception, original)
                self.assertEqual(guard.call_count, 1)
            self.assertFalse(db.in_transaction)
            self.assertIsNone(db.execute("SELECT value FROM config WHERE key='fixture_body_failure'").fetchone())

    def test_late_worker_callbacks_finally_interrupt_and_close_use_retained_resources(self):
        with patch("daemon.workspace_guard", self.fixture_lease):
            configured = self.runtime.hosted.configure({"providers": ["codex"]})
        actor = configured["sessions"][0]["agent_id"]
        self.runtime.prompt({"text": "fixture selected task", "idempotency_key": "retirement-one", "recipient_ids": [actor]})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not BarrierNative.instances:
            time.sleep(0.01)
        self.assertEqual(len(BarrierNative.instances), 1)
        adapter = BarrierNative.instances[0]
        self.assertTrue(adapter.entered.wait(3))
        bus = self.runtime.hosted.server
        job = next(item for item in bus.store.jobs() if item["agent_id"] == actor)
        generation = bus.store.agent(actor)["generation"]
        ledger = bus.supervisor.hosted
        self.replace(bus)
        with self.assertRaises((BridgeError, HarmonyError)):
            self.runtime._guard_workspace()
        failures = []
        with self.forbid_workspace_io(), patch("threading.excepthook", lambda error: failures.append(error.exc_type.__name__)):
            for event in ({"type": "session", "session_id": adapter.session_id},
                          {"type": "turn", "status": "accepted", "turn_id": "late-turn"},
                          {"type": "input_attempt"}):
                with self.assertRaises(BusError):
                    adapter.on_event(event)
            with self.assertRaises(BusError):
                adapter.on_approval({"approval_id": "late-approval"})
            for operation in (lambda: ledger.complete(job, generation, SimpleNamespace(text="late", session_id=adapter.session_id)),
                              lambda: ledger.attention(job, generation, "late attention"),
                              lambda: ledger.process(actor, None), lambda: bus.supervisor.select("all"),
                              lambda: bus.supervisor._native_event(actor, {"type": "session", "session_id": adapter.session_id})):
                with self.assertRaises(BusError):
                    operation()
            bus.supervisor.interrupt()
            self.runtime.hosted.close()
        self.assertTrue(adapter.closed)
        self.assertEqual(bus.supervisor._adapters, {})
        self.assertEqual(bus.supervisor._workers, {})
        self.assertEqual(failures, [])

    def test_constructor_failure_and_http_thread_start_failure_release_lease(self):
        with patch("daemon.BusHTTPServer", side_effect=RuntimeError("fixture constructor failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture constructor"):
                self.runtime.hosted._ensure_bus()
        self.assertEqual(self.runtime.hosted.phase, "idle")
        store = BusStore(self.workspace)
        with daemon.workspace_guard(store):
            pass
        original_start = threading.Thread.start

        def start(thread):
            if thread.name == "avalokita-owned-bus":
                raise RuntimeError("fixture thread start failure")
            return original_start(thread)

        with patch.object(threading.Thread, "start", start), patch.object(daemon.BusHTTPServer, "shutdown") as shutdown:
            with self.assertRaisesRegex(RuntimeError, "fixture thread start"):
                self.runtime.hosted._ensure_bus()
            shutdown.assert_not_called()
        self.assertEqual(self.runtime.hosted.phase, "idle")
        self.assertIsNone(self.runtime.hosted.server)
        self.assertFalse((store.private / "runtime.json").exists())
        with daemon.workspace_guard(store):
            pass

    def test_concurrent_close_latches_before_construction_can_publish_or_recreate(self):
        entered, release = threading.Event(), threading.Event()
        errors = []

        def legacy(store):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Fixture release")
            return False

        def construct():
            try:
                self.runtime.hosted.configure({"providers": ["codex"]})
            except BridgeError as exc:
                errors.append(exc.status)

        with patch("daemon._legacy_owner_alive", legacy), patch("daemon.BusHTTPServer") as factory:
            constructing = threading.Thread(target=construct)
            constructing.start()
            self.assertTrue(entered.wait(2))
            closing = threading.Thread(target=self.runtime.hosted.close)
            closing.start()
            self.assertTrue(self.runtime.hosted._closing.wait(2))
            release.set()
            constructing.join(4)
            closing.join(4)
            self.assertFalse(constructing.is_alive())
            self.assertFalse(closing.is_alive())
            factory.assert_not_called()
        self.assertEqual(errors, [409])
        self.assertEqual(self.runtime.hosted.phase, "closed")
        with self.assertRaises(BridgeError):
            self.runtime.hosted.before_prompt()
        with self.assertRaises(BridgeError):
            self.runtime.hosted.configure({"providers": ["codex"]})
        self.assertIsNone(self.runtime.hosted.server)

    def test_close_during_claim_does_not_create_a_worker_after_cleanup_snapshot(self):
        configured = self.runtime.hosted.configure({"providers": ["codex"]})
        actor = configured["sessions"][0]["agent_id"]
        bus = self.runtime.hosted.server
        reserved, release = threading.Event(), threading.Event()
        original_claim = bus.store.claim

        def claim(agent_id):
            job = original_claim(agent_id)
            if job is not None:
                reserved.set()
                if not release.wait(3):
                    raise TimeoutError("Fixture claim barrier")
            return job

        with patch.object(bus.store, "claim", claim):
            sent = bus.store.send("user", actor, "Fixture claim to close", "task")
            bus.supervisor.wake()
            self.assertTrue(reserved.wait(2))
            closing = threading.Thread(target=self.runtime.hosted.close)
            closing.start()
            self.assertTrue(bus.supervisor._stop.wait(2))
            release.set()
            closing.join(4)
            self.assertFalse(closing.is_alive())
        self.assertEqual(BarrierNative.instances, [])
        self.assertEqual(bus.supervisor._workers, {})
        self.assertEqual(bus.supervisor._busy, set())
        self.assertEqual(bus.store.jobs()[0]["id"], sent["jobs"][0]["id"])
        self.assertEqual(bus.store.jobs()[0]["state"], "running")

    def test_normal_close_preserves_guard_inode_relocks_and_preserves_foreign_runtime(self):
        bus = self.bus()
        guard_path = bus.store.private / "daemon.guard"
        identity = guard_path.stat().st_dev, guard_path.stat().st_ino
        runtime_path = bus.store.private / "runtime.json"
        foreign = {**bus.runtime(), "nonce": "foreign-instance-nonce"}
        runtime_path.write_text(json.dumps(foreign), encoding="utf-8")
        before = runtime_path.read_bytes()
        self.runtime.hosted.close()
        self.runtime.hosted.close()
        self.assertEqual(runtime_path.read_bytes(), before)
        self.assertEqual((guard_path.stat().st_dev, guard_path.stat().st_ino), identity)
        with daemon.workspace_guard(bus.store):
            pass
        self.assertEqual(self.runtime.hosted.phase, "closed")

    def test_normal_close_archives_only_owned_generation(self):
        bus = self.bus()
        runtime_path = bus.store.private / "runtime.json"
        original = runtime_path.read_bytes()
        self.runtime.hosted.close()
        self.assertFalse(runtime_path.exists())
        archived = list((bus.store.private / "daemon-history").glob("*-runtime.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), original)
        with daemon.workspace_guard(bus.store):
            pass

    @unittest.skipUnless(os.name == "nt", "Observed Windows open-lease rename behavior")
    def test_actual_windows_lease_blocks_root_rename_and_still_releases(self):
        bus = self.bus()
        try:
            with self.assertRaises(PermissionError):
                self.workspace.rename(self.old)
        finally:
            self.runtime.hosted.close()
            if self.old.exists() and not self.workspace.exists():
                self.old.rename(self.workspace)
        guard = bus.store.private / "daemon.guard"
        self.assertTrue(guard.is_file())
        with daemon.workspace_guard(bus.store):
            pass

    def test_reparse_detection_latches_shared_guard_after_link_is_removed(self):
        bus = self.bus(fixture_lease=True)
        original = Path.lstat

        def reparse_snapshot(path):
            info = original(path)
            if path == self.workspace:
                values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                values["st_file_attributes"] = getattr(info, "st_file_attributes", 0) | 0x400
                return SimpleNamespace(**values)
            return info

        with patch.object(Path, "lstat", reparse_snapshot):
            with self.assertRaises((BridgeError, HarmonyError)):
                self.runtime._guard_workspace()
        with self.forbid_workspace_io():
            self.assertEqual(self.request(bus, "/health")[0], 403)
            self.runtime.hosted.close()

    @unittest.skipUnless(os.name == "nt", "NTFS junction fixture")
    def test_actual_junction_replacement_is_rejected_and_restoration_stays_retired(self):
        bus = self.bus(fixture_lease=True)
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        self.workspace.rename(self.old)
        replacement = Path(self.temp.name) / "junction-replacement"
        replacement.mkdir()
        sentinel = replacement / "replacement.txt"
        sentinel.write_text("junction target remains untouched", encoding="utf-8")
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(self.workspace), str(replacement)],
                                capture_output=True, timeout=10)
        try:
            if result.returncode:
                self.skipTest("NTFS junction creation unavailable; mocked reparse case is separate")
            with self.assertRaises((BridgeError, HarmonyError)):
                self.runtime._guard_workspace()
            with self.forbid_workspace_io():
                self.assertEqual(self.request(bus, "/health")[0], 403)
                self.runtime.hosted.close()
            self.assertEqual(list(replacement.iterdir()), [sentinel])
        finally:
            if result.returncode == 0:
                os.rmdir(self.workspace)
            self.old.rename(self.workspace)
        with self.forbid_workspace_io():
            with self.assertRaises(BusError):
                bus.store.agents()

    def test_ready_cleanup_requires_shared_guard_and_exact_owner_fields(self):
        server = SimpleNamespace(runtime=self.runtime, instance_id="fixture-instance")
        target = self.workspace / "ready.json"
        value = {"instance_id": server.instance_id, "pid": os.getpid(), "workspace": str(self.workspace)}
        target.write_text(json.dumps({**value, "pid": os.getpid() + 1}), encoding="utf-8")
        _cleanup_ready(server, target)
        self.assertTrue(target.exists())
        target.write_text(json.dumps(value), encoding="utf-8")
        _cleanup_ready(server, target)
        self.assertFalse(target.exists())
        bus = self.bus(fixture_lease=True)
        self.replace(bus)
        with self.assertRaises((BridgeError, HarmonyError)):
            self.runtime._guard_workspace()
        with self.forbid_workspace_io():
            _cleanup_ready(server, target)
            self.runtime.hosted.close()

    @staticmethod
    def metadata_info(**changes):
        value = {"st_mode": stat.S_IFREG | 0o600, "st_nlink": 1, "st_file_attributes": 0,
                 "st_dev": 123, "st_ino": 456}
        return SimpleNamespace(**{**value, **changes})

    def metadata_boundaries(self):
        bus = self.bus()
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        self.assertFalse(bus.supervisor._dispatcher.is_alive())
        return bus, [
            ("harmony", self.runtime.store.db_path, self.runtime.store._check_database_files, self.runtime.store._connection),
            ("bridge", self.runtime.bridge.receipts, self.runtime.bridge._guard_workspace, self.runtime.bridge._db),
            ("bus", bus.store.db_path, bus.store._check_database_files, bus.store.connection),
            ("pinned", bus.store.db_path, self.runtime.bridge._guard_pinned_database,
             lambda: closing(self.runtime.bridge._pinned_db())),
        ]

    @unittest.skipUnless(os.name == "nt", "Windows optional metadata stabilization")
    def test_windows_optional_zero_requires_later_valid_or_missing_sample(self):
        bus, boundaries = self.metadata_boundaries()
        original = Path.lstat
        for name, main, check, _ in boundaries:
            target = Path(str(main) + "-wal")
            for later in (self.metadata_info(), FileNotFoundError("fixture absent sidecar")):
                with self.subTest(boundary=name, later=type(later).__name__):
                    samples = 0

                    def lstat(path):
                        nonlocal samples
                        if path == target:
                            samples += 1
                            if samples == 1:
                                return self.metadata_info(st_nlink=0)
                            self.assertEqual(samples, 2)
                            if isinstance(later, Exception):
                                raise later
                            return later
                        return original(path)

                    with patch.object(Path, "lstat", lstat), patch("time.sleep") as delay:
                        check()
                    self.assertEqual(samples, 2)
                    delay.assert_called_once_with(0.025)
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse(bus.store.workspace_retired)

    @unittest.skipUnless(os.name == "nt", "Windows optional metadata stabilization")
    def test_windows_optional_persistent_zero_is_bounded_503_without_retirement(self):
        bus, boundaries = self.metadata_boundaries()
        original = Path.lstat
        for name, main, check, _ in boundaries:
            with self.subTest(boundary=name):
                target, samples = Path(str(main) + "-shm"), 0

                def lstat(path):
                    nonlocal samples
                    if path == target:
                        samples += 1
                        return self.metadata_info(st_nlink=0)
                    return original(path)

                with patch.object(Path, "lstat", lstat), patch("time.sleep") as delay:
                    with self.assertRaises((BusError, BridgeError, HarmonyError)) as error:
                        check()
                self.assertEqual(error.exception.status, 503)
                self.assertEqual(samples, 3)
                self.assertEqual(delay.call_args_list, [call(0.025), call(0.025)])
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse(bus.store.workspace_retired)

    @unittest.skipUnless(os.name == "nt", "Windows optional metadata stabilization")
    def test_windows_invalid_second_metadata_sample_denies_immediately(self):
        bus, boundaries = self.metadata_boundaries()
        original = Path.lstat
        invalid = [self.metadata_info(st_nlink=2), self.metadata_info(st_nlink=0, st_file_attributes=0x400),
                   self.metadata_info(st_mode=stat.S_IFDIR | 0o700, st_nlink=0), PermissionError("fixture denied")]
        for name, main, check, _ in boundaries:
            target = Path(str(main) + "-wal")
            for later in invalid:
                with self.subTest(boundary=name, later=repr(later)):
                    samples = 0

                    def lstat(path):
                        nonlocal samples
                        if path == target:
                            samples += 1
                            if samples == 1:
                                return self.metadata_info(st_nlink=0)
                            self.assertEqual(samples, 2)
                            if isinstance(later, Exception):
                                raise later
                            return later
                        return original(path)

                    with patch.object(Path, "lstat", lstat), patch("time.sleep") as delay:
                        with self.assertRaises((BusError, BridgeError, HarmonyError)):
                            check()
                    self.assertEqual(samples, 2)
                    delay.assert_called_once_with(0.025)
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse(bus.store.workspace_retired)

    @unittest.skipUnless(os.name == "nt", "Windows optional metadata stabilization")
    def test_windows_main_zero_never_retries_or_opens_a_database(self):
        bus, boundaries = self.metadata_boundaries()
        original = Path.lstat
        for name, target, _, connection in boundaries:
            with self.subTest(boundary=name):
                def lstat(path):
                    if path == target:
                        return self.metadata_info(st_nlink=0)
                    return original(path)

                with patch.object(Path, "lstat", lstat), patch("time.sleep") as delay, \
                        patch("sqlite3.connect", side_effect=AssertionError("Unknown main metadata must not open")) as connect:
                    with self.assertRaises((BusError, BridgeError, HarmonyError)) as error:
                        with connection():
                            pass
                    self.assertEqual(error.exception.status, 503)
                    delay.assert_not_called()
                    connect.assert_not_called()
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse(bus.store.workspace_retired)

    @unittest.skipUnless(os.name == "nt", "Windows optional metadata stabilization")
    def test_windows_resample_checks_storage_parent_before_optional_file(self):
        bus, boundaries = self.metadata_boundaries()
        original = Path.lstat
        for name, main, check, connection in boundaries:
            with self.subTest(boundary=name):
                target, samples, changed = Path(str(main) + "-wal"), 0, False

                def lstat(path):
                    nonlocal samples
                    if changed and path == main.parent:
                        return self.metadata_info(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
                    if path == target:
                        samples += 1
                        return self.metadata_info(st_nlink=0)
                    return original(path)

                def delay(seconds):
                    nonlocal changed
                    self.assertEqual(seconds, 0.025)
                    changed = True

                with patch.object(Path, "lstat", lstat), patch("time.sleep", delay), \
                        patch("sqlite3.connect", side_effect=AssertionError("Reparsed parent must not open")) as connect:
                    with self.assertRaises((BusError, BridgeError, HarmonyError)):
                        with connection():
                            pass
                    connect.assert_not_called()
                self.assertEqual(samples, 1)
                self.assertFalse(self.runtime._workspace_retired)
                check()
        self.assertFalse(bus.store.workspace_retired)

    @unittest.skipUnless(os.name == "nt", "Windows optional metadata stabilization")
    def test_windows_resample_observed_retirement_prevents_next_file_sample_and_open(self):
        for index in range(4):
            self.workspace = Path(self.temp.name) / ("resample-" + str(index))
            self.workspace.mkdir()
            self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False, hosted_factory=BarrierNative,
                hosted_discovery=[{"provider": "codex", "installed": True, "managed_supported": True}])
            self.runtimes.append(self.runtime)
            _, boundaries = self.metadata_boundaries()
            name, main, check, connection = boundaries[index]
            target, samples = Path(str(main) + "-wal"), 0
            original = Path.lstat

            def lstat(path):
                nonlocal samples
                if path == target:
                    samples += 1
                    return self.metadata_info(st_nlink=0)
                return original(path)

            def delay(seconds):
                self.assertEqual(seconds, 0.025)
                self.runtime._workspace_retired = True

            with self.subTest(boundary=name):
                with patch.object(Path, "lstat", lstat), patch("time.sleep", delay), \
                        patch("sqlite3.connect", side_effect=AssertionError("Retirement must not open")) as connect:
                    with self.assertRaises((BusError, BridgeError, HarmonyError)):
                        with connection():
                            pass
                    connect.assert_not_called()
                self.assertEqual(samples, 1)
                with self.forbid_workspace_io(include_metadata=True):
                    with self.assertRaises((BusError, BridgeError, HarmonyError)):
                        check()
                self.assertTrue(self.runtime._workspace_retired)

    @unittest.skipUnless(os.name == "nt", "Windows unknown identity classification")
    def test_windows_unknown_root_capture_is_503_before_state_creation(self):
        constructors = [HarmonyStore, SharebusBridge, lambda root: Runtime(root, discovery=False, native_ipc=False),
                        lambda root: HostedLedger(SimpleNamespace(workspace=root, _workspace_guard=None))]
        original = Path.stat
        for index, construct in enumerate(constructors):
            root = Path(self.temp.name) / ("unknown-capture-" + str(index))
            root.mkdir()

            def unknown(path, *args, **kwargs):
                if path == root:
                    return self.metadata_info(st_mode=stat.S_IFDIR | 0o700, st_dev=0, st_ino=0)
                return original(path, *args, **kwargs)

            with self.subTest(constructor=index):
                with patch.object(Path, "stat", unknown), \
                        patch.object(Path, "mkdir", side_effect=AssertionError("Unknown identity must not create folders")), \
                        patch.object(Path, "open", side_effect=AssertionError("Unknown identity must not open files")), \
                        patch("sqlite3.connect", side_effect=AssertionError("Unknown identity must not create DB")):
                    with self.assertRaises((BusError, BridgeError, HarmonyError)) as error:
                        construct(root)
                    self.assertEqual(error.exception.status, 503)
                self.assertEqual(list(root.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows unknown identity classification")
    def test_windows_unknown_root_comparison_keeps_original_capture_and_latch(self):
        ledger = HostedLedger(SimpleNamespace(workspace=self.workspace, _workspace_guard=None, guard_workspace=lambda: None))
        original = Path.lstat
        states = [(self.runtime, self.runtime._guard_workspace_identity, None),
                  (self.runtime.store, self.runtime.store._check_workspace_identity, "_workspace_guard"),
                  (self.runtime.bridge, self.runtime.bridge._guard_workspace_identity, "_workspace_guard"),
                  (ledger, ledger.guard, None)]
        captured = self.runtime.store._workspace_identity, self.runtime.bridge._workspace_identity, ledger.identity
        for subject, check, callback in states:
            for mode in (stat.S_IFDIR | 0o700, 0):
                with self.subTest(subject=type(subject).__name__, mode=mode):
                    def unknown(path):
                        if path == self.workspace:
                            return self.metadata_info(st_mode=mode, st_dev=0, st_ino=0)
                        return original(path)

                    with ExitStack() as stack:
                        if callback:
                            stack.enter_context(patch.object(subject, callback, None))
                        stack.enter_context(patch.object(Path, "lstat", unknown))
                        with self.assertRaises((BusError, BridgeError, HarmonyError)) as error:
                            check()
                        self.assertEqual(error.exception.status, 503)
                    self.assertFalse(self.runtime._workspace_retired)
                    self.assertFalse(self.runtime.store._workspace_retired)
                    self.assertFalse(self.runtime.bridge._workspace_retired)
                    self.assertFalse(ledger.retired)
        self.assertEqual(captured, (self.runtime.store._workspace_identity, self.runtime.bridge._workspace_identity, ledger.identity))
        self.runtime._workspace_retired = True
        with patch.object(Path, "lstat", side_effect=AssertionError("Old retirement latch must win before I/O")):
            with self.assertRaises(WorkspaceRetired):
                self.runtime._guard_workspace_identity()

    def assert_preclaim_recovers(self, refusal):
        failed = threading.Event()
        original = BusStore.paused

        def paused(store):
            if threading.current_thread().name == "sharebus-dispatch" and not failed.is_set():
                failed.set()
                raise refusal
            return original(store)

        with patch.object(BusStore, "paused", paused):
            actor = self.runtime.hosted.configure({"providers": ["codex"]})["sessions"][0]["agent_id"]
            bus = self.runtime.hosted.server
            self.assertTrue(failed.wait(3))
            self.assertFalse(bus.supervisor._stop.is_set())
            self.runtime.prompt({"text": "fixture same valid original work", "idempotency_key": "transient-preclaim", "recipient_ids": [actor]})
            deadline = time.monotonic() + 5
            while not BarrierNative.instances and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(BarrierNative.instances), 1)
            adapter = BarrierNative.instances[0]
            self.assertTrue(adapter.entered.wait(3))
            adapter.release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                jobs = bus.store.jobs()
                if len(jobs) == 1 and jobs[0]["state"] == "completed":
                    break
                time.sleep(0.01)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "completed")
            with bus.store.connection() as db:
                self.assertEqual(db.execute("SELECT used FROM roots WHERE id=?", (jobs[0]["root_id"],)).fetchone()[0], 1)
            self.assertEqual(len(BarrierNative.instances), 1)

    def test_preclaim_transient_failure_skips_tick_and_same_job_runs_once(self):
        self.assert_preclaim_recovers(BusError("fixture preclaim unavailable", 503))

    def test_preclaim_sqlite_busy_skips_tick_and_same_job_runs_once(self):
        self.assert_preclaim_recovers(sqlite3.OperationalError("database is locked"))

    def test_preclaim_os_error_skips_tick_and_same_job_runs_once(self):
        self.assert_preclaim_recovers(OSError("fixture preclaim storage unavailable"))

    def assert_mailbox_recovers(self, refusal):
        first, second = threading.Event(), threading.Event()
        calls = 0

        def drain():
            nonlocal calls
            calls += 1
            if calls == 1:
                first.set()
                raise refusal
            second.set()

        try:
            with patch.object(self.runtime, "drain_native_peers", drain):
                self.runtime.start_peer_reconciler()
                self.runtime.peer_wakeup.set()
                self.assertTrue(first.wait(3))
                self.assertFalse(self.runtime.peer_stop.is_set())
                self.runtime.peer_wakeup.set()
                self.assertTrue(second.wait(3))
        finally:
            self.runtime.stop_peer_reconciler()
        self.assertTrue(self.runtime.peer_stop.is_set())
        self.runtime.start_peer_reconciler()
        self.assertTrue(self.runtime.peer_stop.is_set())
        self.assertFalse(self.runtime.peer_thread.is_alive())

    def test_peer_mailbox_transient_failure_keeps_loop_and_shutdown_latch(self):
        self.assert_mailbox_recovers(BusError("fixture mailbox unavailable", 503))

    def test_peer_mailbox_sqlite_busy_keeps_loop_and_shutdown_latch(self):
        self.assert_mailbox_recovers(sqlite3.OperationalError("database is locked"))

    def test_peer_mailbox_os_error_keeps_loop_and_shutdown_latch(self):
        self.assert_mailbox_recovers(OSError("fixture mailbox storage unavailable"))

    def context_fixture(self):
        store = self.runtime.store
        store.register_pane("fixture-pane", "codex", "fixture role", "fixture-native", [], verified=True)
        root = store.new_goal("fixture selected context", idempotency_key="selected-context")["root_id"]
        target = self.workspace / "selected.txt"
        target.write_text("selected fixture text", encoding="utf-8")
        return store, root, target

    def test_publish_context_retirement_during_read_prevents_after_stat_and_commit(self):
        store, root, target = self.context_fixture()
        original_open = Path.open
        runtime = self.runtime
        observed = False

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *error):
                return self.stream.__exit__(*error)

            def read(self, limit):
                nonlocal observed
                data = self.stream.read(limit)
                runtime._workspace_retired = True
                observed = True
                return data

        def opened(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            return Reader(stream) if path == target else stream

        with closing(sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True)) as retained:
            before = retained.execute("SELECT value FROM config WHERE key='context_generation'").fetchone()[0]
            with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
                with patch.object(Path, "open", opened):
                    with self.assertRaises(WorkspaceRetired):
                        store.publish_context("fixture-pane", ["selected.txt"], root_id=root)
            self.assertTrue(observed)
            self.assertEqual(retained.execute("SELECT COUNT(*) FROM contexts").fetchone()[0], 0)
            self.assertEqual(retained.execute("SELECT value FROM config WHERE key='context_generation'").fetchone()[0], before)

    def test_read_context_retirement_during_is_file_prevents_lexical_stat_and_open(self):
        store, root, target = self.context_fixture()
        saved = store.publish_context("fixture-pane", ["selected.txt"], root_id=root)
        original = Path.is_file
        observed = False

        def is_file(path):
            nonlocal observed
            value = original(path)
            if path == target and sys._getframe(1).f_code.co_name == "read_context":
                self.runtime._workspace_retired = True
                observed = True
            return value

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(Path, "is_file", is_file):
                with self.assertRaises(WorkspaceRetired):
                    store.read_context(saved["snapshot_id"], root_id=root)
        self.assertTrue(observed)

    def assert_private_swap_fenced(self, endpoint):
        bus = self.bus(fixture_lease=True)
        if endpoint == "hosted":
            target = bus.store.private / "runtime.json"
            invoke = self.runtime.hosted.close
        elif endpoint == "native-key":
            target = bus.store.private / "operator.token"
            dispatcher = object.__new__(NativeDispatcher)
            dispatcher.bridge = self.runtime.bridge
            invoke = dispatcher._routing_key
        elif endpoint == "native-routing":
            target = self.runtime.bridge.receipts.parent / "fixture-routing.json"
            target.write_text("{}", encoding="utf-8")
            dispatcher = object.__new__(NativeDispatcher)
            dispatcher.bridge = self.runtime.bridge
            dispatcher.routing_path = target
            dispatcher.harmony = self.runtime.store
            invoke = dispatcher._load_routing
        else:
            target = self.runtime.bridge.receipts.parent / "fixture-capability.txt"
            target.write_text("selected fixture capability", encoding="utf-8")
            invoke = lambda: private_read(target, workspace_guard=self.runtime.bridge._guard_workspace_identity)
        original = Path.stat
        swapped, observed = False, False
        replacement, before = None, None

        def metadata(path, *args, **kwargs):
            nonlocal swapped, observed, replacement, before
            info = original(path, *args, **kwargs)
            if path == target and not swapped:
                swapped = True
                replacement = self.replace(bus)
                before = hashlib.sha256(replacement.db_path.read_bytes()).hexdigest()
                with self.assertRaises(WorkspaceRetired):
                    self.runtime._guard_workspace_identity()
                observed = True
            return info

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(Path, "stat", metadata):
                if endpoint == "hosted":
                    invoke()
                else:
                    with self.assertRaises((BridgeError, BusError)):
                        invoke()
        self.assertTrue(swapped)
        self.assertTrue(observed)
        self.runtime.hosted.close()
        self.assertEqual(hashlib.sha256(replacement.db_path.read_bytes()).hexdigest(), before)
        self.assertTrue((self.workspace / "replacement.txt").exists())

    def test_private_read_owned_guard_fences_actual_root_swap_after_first_stat(self):
        self.assert_private_swap_fenced("bridge")

    def test_hosted_private_metadata_guard_fences_actual_root_swap_after_first_stat(self):
        self.assert_private_swap_fenced("hosted")

    def test_native_routing_key_guard_fences_actual_root_swap_after_first_stat(self):
        self.assert_private_swap_fenced("native-key")

    def test_native_routing_load_guard_fences_actual_root_swap_after_first_stat(self):
        self.assert_private_swap_fenced("native-routing")

    def test_native_routing_readers_reject_retired_entry_before_exists_or_read(self):
        dispatcher = object.__new__(NativeDispatcher)
        dispatcher.bridge = self.runtime.bridge
        dispatcher.routing_path = self.runtime.bridge.receipts.parent / "fixture-routing.json"
        self.runtime._workspace_retired = True
        with self.forbid_workspace_io(include_metadata=True):
            for invoke in (dispatcher._routing_key, dispatcher._load_routing):
                with self.assertRaises((BridgeError, BusError)):
                    invoke()

    def test_private_read_standalone_validation_and_strip_are_preserved(self):
        target = self.workspace / "fixture-private.txt"
        target.write_text("  selected 한국어 text\n", encoding="utf-8")
        self.assertEqual(private_read(target), "selected 한국어 text")
        with self.assertRaises(BridgeError):
            private_read(target, maximum=2)
        alias = self.workspace / "fixture-private-alias.txt"
        os.link(target, alias)
        try:
            with self.assertRaises(BridgeError):
                private_read(target)
        finally:
            alias.unlink()
        self.assertFalse(self.runtime._workspace_retired)

    def test_harmony_path_latch_after_linked_blocks_exists_and_resolve(self):
        import harmony
        target = self.workspace / "selected.txt"
        target.write_text("selected fixture text", encoding="utf-8")
        original = harmony._linked
        observed = False

        def linked(path):
            nonlocal observed
            value = original(path)
            if path == target:
                self.runtime._workspace_retired = True
                observed = True
            return value

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(harmony, "_linked", linked):
                with self.assertRaises(WorkspaceRetired):
                    self.runtime.store._path("selected.txt")
        self.assertTrue(observed)

    def test_harmony_path_latch_after_is_file_blocks_stat_and_resolve(self):
        target = self.workspace / "selected.txt"
        target.write_text("selected fixture text", encoding="utf-8")
        original = Path.is_file
        observed = False

        def is_file(path):
            nonlocal observed
            value = original(path)
            if path == target:
                self.runtime._workspace_retired = True
                observed = True
            return value

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(Path, "is_file", is_file):
                with self.assertRaises(WorkspaceRetired):
                    self.runtime.store._path("selected.txt")
        self.assertTrue(observed)

    def assert_daemon_exists_latch_fences_stat(self, operation):
        bus = self.bus()
        target = bus.store.private / "runtime.json"
        original = Path.exists
        observed = False

        def exists(path):
            nonlocal observed
            value = original(path)
            if path == target and sys._getframe(1).f_code.co_name == operation.__name__:
                self.runtime._workspace_retired = True
                observed = True
            return value

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(Path, "exists", exists):
                with self.assertRaises(WorkspaceRetired):
                    operation(bus.store, "runtime.json") if operation is daemon._archive_daemon_metadata else operation(bus.store)
        self.assertTrue(observed)

    def test_archive_metadata_latch_after_exists_blocks_stat(self):
        self.assert_daemon_exists_latch_fences_stat(daemon._archive_daemon_metadata)

    def test_legacy_owner_latch_after_exists_blocks_stat(self):
        self.assert_daemon_exists_latch_fences_stat(daemon._legacy_owner_alive)

    def assert_startup_log_latch_fences_next_io(self, boundary):
        bus = self.bus()
        target = bus.store.private / "startup.log"
        target.write_text("fixture startup marker\n", encoding="utf-8")
        original_linked = BusStore.reject_linked_path
        original_exists, original_file = Path.exists, Path.is_file
        observed = False

        def latch():
            nonlocal observed
            self.runtime._workspace_retired = True
            observed = True

        def linked(path):
            original_linked(path)
            if boundary == "linked-folder" and path == bus.store.directory:
                latch()
            if boundary == "linked-log" and path == target:
                latch()

        def exists(path):
            value = original_exists(path)
            if boundary == "exists-log" and path == target:
                latch()
            return value

        def is_file(path):
            value = original_file(path)
            if boundary == "is-file-log" and path == target:
                latch()
            return value

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(BusStore, "reject_linked_path", linked), \
                    patch.object(Path, "exists", exists), patch.object(Path, "is_file", is_file):
                with self.assertRaises(WorkspaceRetired):
                    daemon._startup_log(self.workspace, "selected-fixture-stage", store=bus.store)
        self.assertTrue(observed)

    def test_owned_startup_log_latch_after_folder_validation_blocks_mkdir(self):
        self.assert_startup_log_latch_fences_next_io("linked-folder")

    def test_owned_startup_log_latch_after_log_validation_blocks_exists(self):
        self.assert_startup_log_latch_fences_next_io("linked-log")

    def test_owned_startup_log_latch_after_exists_blocks_is_file(self):
        self.assert_startup_log_latch_fences_next_io("exists-log")

    def test_owned_startup_log_latch_after_is_file_blocks_stat(self):
        self.assert_startup_log_latch_fences_next_io("is-file-log")

    def test_ready_cleanup_latch_after_link_validation_blocks_read(self):
        server = SimpleNamespace(runtime=self.runtime, instance_id="fixture-nested-ready")
        target = self.workspace / "nested-ready.json"
        target.write_text(json.dumps({"instance_id": server.instance_id, "pid": os.getpid(),
                                      "workspace": str(self.workspace)}), encoding="utf-8")
        original = BusStore.reject_linked_path
        observed = False

        def linked(path):
            nonlocal observed
            original(path)
            if path == target:
                self.runtime._workspace_retired = True
                observed = True

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(BusStore, "reject_linked_path", linked):
                _cleanup_ready(server, target)
        self.assertTrue(observed)
        self.assertTrue(target.exists())

    def test_daemon_metadata_one_shot_guard_refusal_skips_cleanup_and_preserves_error(self):
        bus = self.bus()
        original_guard, original_chmod = bus.store.guard_workspace, os.chmod
        refusal = BusError("fixture metadata guard refusal", 503)
        armed, refused = False, False

        def chmod(path, *args, **kwargs):
            nonlocal armed
            value = original_chmod(path, *args, **kwargs)
            if Path(path).name.startswith(".daemon-"):
                armed = True
            return value

        def guard():
            nonlocal refused
            if armed and not refused:
                refused = True
                raise refusal
            return original_guard()

        with self.forbid_workspace_io(after=lambda: refused, include_metadata=True):
            with patch.object(bus.store, "guard_workspace", guard), patch("os.chmod", chmod):
                with self.assertRaises(BusError) as error:
                    daemon._write_daemon_metadata(bus.store, "fixture-write.json", {"stage": "selected fixture"})
                self.assertIs(error.exception, refusal)
        self.assertTrue(refused)
        self.assertFalse(self.runtime._workspace_retired)
        self.assertFalse((bus.store.private / "fixture-write.json").exists())
        self.assertEqual(len(list(bus.store.private.glob(".daemon-*.tmp"))), 1)

    def test_bridge_runtime_private_guard_refusal_preserves_original_bus_error(self):
        import bridge
        self.bus()
        original_read, original_guard = bridge.private_read, self.runtime.bridge._guard_workspace_identity
        for refusal in (BusError("fixture metadata unavailable", 503), WorkspaceRetired()):
            with self.subTest(status=refusal.status):
                armed = False

                def guarded_read(*args, **kwargs):
                    nonlocal armed
                    armed = True
                    return original_read(*args, **kwargs)

                def guard():
                    if armed:
                        raise refusal
                    return original_guard()

                with patch.object(bridge, "private_read", guarded_read), \
                        patch.object(self.runtime.bridge, "_guard_workspace_identity", guard), \
                        patch.object(self.runtime.bridge, "_fetch", side_effect=AssertionError("HTTP must not follow refusal")):
                    with self.assertRaises(BusError) as error:
                        self.runtime.bridge._runtime()
                    self.assertIs(error.exception, refusal)
                self.assertTrue(armed)

    def assert_store_delta_boundary(self, family, point):
        if getattr(self, "delta_case_count", 0):
            self.runtime = Runtime(self.workspace, discovery=False, native_ipc=False,
                hosted_factory=BarrierNative, hosted_discovery=[])
            self.runtimes.append(self.runtime)
        self.delta_case_count = getattr(self, "delta_case_count", 0) + 1
        self.old = self.workspace.with_name("delta-original-" + str(self.delta_case_count))
        synthetic = family in {"recovery", "prepare"}
        bus = self.bus(fixture_lease=not synthetic)
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        self.assertFalse(bus.supervisor._dispatcher.is_alive())
        store = bus.store
        ordinal = 1
        if family == "ctor":
            caller = "__init__"
            method, target = {
                "checked": ("checked_workspace", self.workspace),
                "folder-symlink": ("is_symlink", store.directory),
                "folder-reparse": ("_reparse", store.directory),
                "ignore-symlink": ("is_symlink", store.private / ".gitignore"),
                "db-symlink": ("is_symlink", store.db_path),
                "db-reparse": ("_reparse", store.db_path),
                "db-exists": ("exists", store.db_path),
                "db-is-file": ("is_file", store.db_path),
                "token-symlink": ("is_symlink", store.private / "operator.token"),
            }[point]
            invoke = lambda: BusStore(self.workspace, workspace_guard=self.runtime._guard_workspace_identity)
        elif family == "mirror":
            caller = "_write_auto_mirror"
            target = store.directory / "auto.json"
            target.write_text("{}", encoding="utf-8")
            method = {"reject": "reject_linked_path", "symlink": "is_symlink", "reparse": "_reparse", "exists": "exists"}[point]
            invoke = store._write_auto_mirror
        elif family in {"auto-path", "auto-write"}:
            target = store.private / "auto/contexts/delta.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{}", encoding="utf-8")
            if family == "auto-write":
                caller, method, target = "write_auto_private", "reject_linked_path", store.private / "auto"
                invoke = lambda: store.write_auto_private(store.private / "auto/contexts/delta.json", {"stage": "fixture"})
            else:
                caller = "checked_auto_path"
                method = {"reject": "reject_linked_path", "candidate-resolve": "resolve", "private-resolve": "resolve",
                          "exists": "exists", "is-file": "is_file", "nlink-stat": "stat", "size-stat": "stat"}[point]
                invoke = lambda: store.checked_auto_path(store.private / "auto/contexts/delta.json", must_exist=True)
                if point == "private-resolve":
                    target = store.private / "auto"
                if point == "size-stat":
                    ordinal = 2
        elif family in {"credential", "recovery", "manifest", "artifact"}:
            agent, _ = store.register("codex", "Delta fixture", [], "external_attached", "fixture", native_session_id="delta-native")
            target = store.credentials / (agent["id"] + ".token")
            if family == "recovery":
                caller, method = "redeem_recovery", "is_symlink"
                code = store.create_recovery(agent["id"])["recovery_code"]
                invoke = lambda: store.redeem_recovery("codex", "delta-native", code)
            elif family == "manifest":
                caller, method, target = "_manifest", {"symlink": "is_symlink", "reparse": "_reparse"}[point], store.directory / "codex"
                invoke = lambda: store._manifest(agent["id"])
            elif family == "artifact":
                record = store.publish(agent["id"], text="Selected delta fixture", filename="delta.txt")
                caller, method, target = "artifact_file", {"symlink": "is_symlink", "reparse": "_reparse"}[point], self.workspace / record["relative_path"]
                invoke = lambda: store.artifact_file(record["id"])
            else:
                with store.connection() as db:
                    owner = dict(db.execute("SELECT * FROM agents WHERE id=?", (agent["id"],)).fetchone())
                caller = "_auto_credential"
                method = {"reject": "reject_linked_path", "symlink": "is_symlink", "reparse": "_reparse", "is-file": "is_file"}[point]
                invoke = lambda: store._auto_credential(owner)
        elif family == "prepare":
            store.configure_auto(enabled=True)
            target = store.prepare_auto_context("codex", "delta-native", "codex:SessionStart")
            caller, method = "prepare_auto_context", "checked_auto_path"
            invoke = lambda: store.prepare_auto_context("codex", "delta-native", "codex:SessionStart")
        elif family == "source":
            folder = self.workspace / "selected"
            folder.mkdir()
            target = folder / "delta.txt"
            target.write_text("Selected delta source", encoding="utf-8")
            caller, method = "safe_source", {"symlink": "is_symlink", "ancestor-reparse": "_reparse", "leaf-reparse": "_reparse", "resolve": "resolve"}[point]
            if point in {"symlink", "ancestor-reparse"}:
                target = folder
            invoke = lambda: store.safe_source("selected/delta.txt")
        elif family == "pinned":
            import bridge
            caller, method, target = "_pinned_db", "read_bytes", bridge.SHAREBUS / "store.py"
            invoke = self.runtime.bridge._pinned_db
        else:
            self.fail("Unknown delta fixture family")
        observed, triggered, matches = False, False, 0
        replacement, before = None, None

        def after_result(path, current_caller, **kwargs):
            nonlocal observed, triggered, matches, replacement, before
            if triggered or path != target or current_caller != caller:
                return
            if family == "prepare" and kwargs.get("must_exist") is not True:
                return
            matches += 1
            if matches != ordinal:
                return
            triggered = True
            if synthetic:
                with self.assertRaises(WorkspaceRetired):
                    self.runtime._retire_workspace()
            else:
                replacement = self.replace(bus)
                before = hashlib.sha256(replacement.db_path.read_bytes()).hexdigest()
                with self.assertRaises(WorkspaceRetired):
                    self.runtime._guard_workspace_identity()
            observed = True

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with ExitStack() as stack:
                if method in {"checked_workspace", "reject_linked_path"}:
                    original = getattr(BusStore, method).__func__

                    def wrapped(cls, path, *args, **kwargs):
                        value = original(cls, path, *args, **kwargs)
                        after_result(Path(path), sys._getframe(1).f_code.co_name, **kwargs)
                        return value

                    stack.enter_context(patch.object(BusStore, method, classmethod(wrapped)))
                elif method == "_reparse":
                    original = BusStore._reparse

                    def wrapped(path):
                        value = original(path)
                        after_result(path, sys._getframe(1).f_code.co_name)
                        return value

                    stack.enter_context(patch.object(BusStore, method, staticmethod(wrapped)))
                elif method == "checked_auto_path":
                    original = BusStore.checked_auto_path

                    def wrapped(instance, path, *args, **kwargs):
                        value = original(instance, path, *args, **kwargs)
                        after_result(Path(path), sys._getframe(1).f_code.co_name, **kwargs)
                        return value

                    stack.enter_context(patch.object(BusStore, method, wrapped))
                else:
                    original = getattr(Path, method)

                    def wrapped(path, *args, **kwargs):
                        value = original(path, *args, **kwargs)
                        after_result(path, sys._getframe(1).f_code.co_name, **kwargs)
                        return value

                    stack.enter_context(patch.object(Path, method, wrapped))
                try:
                    expected_error = BridgeError if family == "pinned" else WorkspaceRetired
                    with self.assertRaises(expected_error) as error:
                        invoke()
                    self.assertIs(type(error.exception), expected_error)
                    self.assertEqual(error.exception.status, 403)
                finally:
                    self.runtime.hosted.close()
        self.assertTrue(triggered)
        self.assertTrue(observed)
        if replacement is not None:
            self.assertEqual(hashlib.sha256(replacement.db_path.read_bytes()).hexdigest(), before)
            self.assertTrue((self.workspace / "replacement.txt").exists())
        if not hasattr(self, "delta_observations"):
            self.delta_observations = []
        self.delta_observations.append({"family": family, "point": point, "real_result_before_injection": True,
            "physical_root_swap": not synthetic, "synthetic_shared_latch_with_real_transaction": synthetic,
            "post_observed_io_attempts": 0, "exception": type(error.exception).__name__, "status": 403})

    def test_store_delta_constructor_nested_metadata(self):
        for point in ("checked", "folder-symlink", "folder-reparse", "ignore-symlink", "db-symlink", "db-reparse", "db-exists", "db-is-file", "token-symlink"):
            with self.subTest(point=point):
                self.assert_store_delta_boundary("ctor", point)

    def test_store_delta_recovery_credential_metadata(self):
        self.assert_store_delta_boundary("recovery", "symlink")

    def test_store_delta_auto_mirror_metadata(self):
        for point in ("reject", "symlink", "reparse", "exists"):
            with self.subTest(point=point):
                self.assert_store_delta_boundary("mirror", point)

    def test_store_delta_auto_path_metadata(self):
        for point in ("reject", "candidate-resolve", "private-resolve", "exists", "is-file", "nlink-stat", "size-stat"):
            with self.subTest(point=point):
                self.assert_store_delta_boundary("auto-path", point)

    def test_store_delta_auto_write_reject_to_mkdir(self):
        self.assert_store_delta_boundary("auto-write", "reject")

    def test_store_delta_auto_credential_metadata(self):
        for point in ("reject", "symlink", "reparse", "is-file"):
            with self.subTest(point=point):
                self.assert_store_delta_boundary("credential", point)

    def test_store_delta_prepare_checked_path_to_read(self):
        self.assert_store_delta_boundary("prepare", "checked")

    def test_store_delta_manifest_metadata(self):
        for point in ("symlink", "reparse"):
            with self.subTest(point=point):
                self.assert_store_delta_boundary("manifest", point)

    def test_store_delta_safe_source_metadata_and_resolve(self):
        for point in ("symlink", "ancestor-reparse", "leaf-reparse", "resolve"):
            with self.subTest(point=point):
                self.assert_store_delta_boundary("source", point)

    def test_store_delta_artifact_ancestor_metadata(self):
        for point in ("symlink", "reparse"):
            with self.subTest(point=point):
                self.assert_store_delta_boundary("artifact", point)

    def test_store_delta_pinned_source_read_to_connect(self):
        self.assert_store_delta_boundary("pinned", "source-read")

    def test_store_delta_safe_source_preserves_first_transient_bus_error(self):
        bus = self.bus()
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        target = self.workspace / "delta-source.txt"
        target.write_text("Selected fixture source", encoding="utf-8")
        original, original_guard = Path.resolve, bus.store.guard_workspace
        refusal = BusError("fixture source metadata unavailable", 503)
        armed, refused = False, False

        def resolve(path, *args, **kwargs):
            nonlocal armed
            value = original(path, *args, **kwargs)
            if path == target:
                armed = True
            return value

        def guard():
            nonlocal refused
            if armed and not refused:
                refused = True
                raise refusal
            return original_guard()

        with self.forbid_workspace_io(after=lambda: refused, include_metadata=True):
            with patch.object(Path, "resolve", resolve), patch.object(bus.store, "guard_workspace", guard):
                with self.assertRaises(BusError) as error:
                    bus.store.safe_source("delta-source.txt")
                self.assertIs(error.exception, refusal)
        self.assertTrue(refused)
        self.assertFalse(self.runtime._workspace_retired)

    def test_store_delta_import_source_pin_and_native_source_are_unchanged(self):
        import bridge
        import store
        self.assertIs(bridge.BusStore, store.BusStore)
        self.assertEqual(hashlib.sha256((bridge.SHAREBUS / "store.py").read_bytes()).hexdigest(), bridge.STORE_SHA256)
        self.assertEqual(hashlib.sha256((HERE / "native_dispatch.py").read_bytes()).hexdigest(),
                         "f3a305dfa6ca37d10d6a1db7a410556183291e8a1e12e1bccf2fd55868a3a108")

    def assert_database_boundary_retirement(self, family, *, commit=False):
        bus = self.bus(fixture_lease=not commit)
        bus.supervisor._stop.set()
        bus.supervisor.wake()
        bus.supervisor._dispatcher.join(timeout=2)
        self.assertFalse(bus.supervisor._dispatcher.is_alive())
        if family == "bus":
            main, factory, caller, table, expected = bus.store.db_path, bus.store.connection, "_check_database_files", "config", WorkspaceRetired
        elif family == "harmony":
            main, factory, caller, table, expected = self.runtime.store.db_path, lambda: self.runtime.store._connection(write=True), "_check_database_files", "config", WorkspaceRetired
        elif family == "bridge":
            main, factory, caller, table, expected = self.runtime.bridge.receipts, self.runtime.bridge._db, "_check_database_path", "settings", BridgeError
        elif family == "pinned":
            main, caller, table, expected = bus.store.db_path, "_check_database_path", "config", BridgeError

            @contextmanager
            def factory():
                with closing(self.runtime.bridge._pinned_db()) as db:
                    with self.runtime.bridge._pinned_transaction(db):
                        yield db
        else:
            self.fail("Unknown DB boundary fixture")
        target = Path(str(main) + "-shm")
        key = "delta_precommit_marker"
        if commit:
            with factory() as db:
                db.execute("INSERT INTO " + table + " (key,value) VALUES (?,?)", (key, "before"))
        else:
            # A real regular-file metadata sentinel. SQLite never reads its
            # contents because the new identity gate must reject before open.
            target.touch(exist_ok=True)
        original = Path.lstat
        armed, observed, triggered, entered = False, False, False, False
        replacement, before, retained = None, None, None

        def lstat(path):
            nonlocal observed, triggered, replacement, before
            info = original(path)
            if (not triggered and path == target and sys._getframe(1).f_code.co_name == caller
                    and (armed or not commit) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and not getattr(info, "st_file_attributes", 0) & 0x400):
                triggered = True
                if commit:
                    with self.assertRaises(WorkspaceRetired):
                        self.runtime._retire_workspace()
                else:
                    replacement = self.replace(bus)
                    before = hashlib.sha256(replacement.db_path.read_bytes()).hexdigest()
                    with self.assertRaises(WorkspaceRetired):
                        self.runtime._guard_workspace_identity()
                observed = True
            return info

        with self.forbid_workspace_io(after=lambda: observed, include_metadata=True):
            with patch.object(Path, "lstat", lstat):
                try:
                    with self.assertRaises(expected) as error:
                        with factory() as db:
                            entered = True
                            retained = db
                            if commit:
                                db.execute("UPDATE " + table + " SET value=? WHERE key=?", ("after", key))
                                armed = True
                    self.assertIs(type(error.exception), expected)
                    self.assertEqual(error.exception.status, 403)
                finally:
                    self.runtime.hosted.close()
        self.assertTrue(triggered)
        self.assertTrue(observed)
        if commit:
            self.assertTrue(entered)
            with self.assertRaises(sqlite3.ProgrammingError):
                retained.execute("SELECT 1")
            with closing(sqlite3.connect(main.as_uri() + "?mode=ro", uri=True)) as check:
                self.assertEqual(check.execute("SELECT value FROM " + table + " WHERE key=?", (key,)).fetchone()[0], "before")
        else:
            self.assertFalse(entered)
            self.assertEqual(hashlib.sha256(replacement.db_path.read_bytes()).hexdigest(), before)
            self.assertTrue((self.workspace / "replacement.txt").exists())
        self.db_boundary_observations = [{"family": family, "phase": "precommit" if commit else "preopen",
            "real_last_regular_nonreparse_nlink1_shm_metadata": True, "physical_root_swap": not commit,
            "preopen_metadata_sentinel_not_sqlite_shm_layout_claim": not commit,
            "synthetic_shared_latch_with_real_transaction": commit, "post_observed_lexical_io_attempts": 0,
            "connect_after_latch": 0, "retained_connection_closed": bool(commit),
            "prior_row_preserved_by_rollback": bool(commit), "exception": type(error.exception).__name__, "status": 403}]

    def test_db_boundary_store_metadata_return_to_connect(self):
        self.assert_database_boundary_retirement("bus")

    def test_db_boundary_harmony_metadata_return_to_connect(self):
        self.assert_database_boundary_retirement("harmony")

    def test_db_boundary_bridge_metadata_return_to_connect(self):
        self.assert_database_boundary_retirement("bridge")

    def test_db_boundary_store_metadata_return_to_commit(self):
        self.assert_database_boundary_retirement("bus", commit=True)

    def test_db_boundary_harmony_metadata_return_to_commit(self):
        self.assert_database_boundary_retirement("harmony", commit=True)

    def test_db_boundary_bridge_metadata_return_to_commit(self):
        self.assert_database_boundary_retirement("bridge", commit=True)

    def test_db_boundary_pinned_metadata_return_to_commit(self):
        self.assert_database_boundary_retirement("pinned", commit=True)


if __name__ == "__main__":
    unittest.main()
