"""Disposable home/workspace fixtures; no real provider/MCP/model is invoked."""
from functools import partial
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from inventory_runtime import InventoryRuntime
from inventory_vault import PrivateVault
from preference_learning import PreferenceModel, featureize
from server import Runtime, Server


def opaque(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CountingModel(PreferenceModel):
    fits = 0
    loads = 0

    def fit(self, samples):
        type(self).fits += 1
        return super().fit(samples)

    @classmethod
    def from_dict(cls, checkpoint):
        cls.loads += 1
        return super().from_dict(checkpoint)


class FixtureCollector:
    def __init__(self, vault, *, home=None, device_probe=None, fail=False):
        self.vault = vault
        self.closed = threading.Event()
        self.calls = 0
        self.fail = fail
        self.home = home

    def collect_once(self, *, max_records):
        self.calls += 1
        if self.fail:
            raise RuntimeError("RAW_PRIVATE_TEXT token=never-publish C:/Users/private")
        self.vault.put_evidence({"id": opaque("human-fixture"), "source_kind": "human_explicit",
            "source_ref": opaque("source-fixture"), "event_ref": opaque("event-fixture"), "observed_at": 1700000000.0,
            "scope": self.vault.workspace_key, "provider": "codex", "purpose": "implementation", "capability": None,
            "features": featureize("Implement a bounded local collector", "implementation"), "target": 1, "weight": 1.0})
        return {"state": "partial", "complete": False, "records": 1, "evidence_added": 1,
                "sources": [{"source": "codex_history", "status": "ready", "records": 1, "complete": True},
                            {"source": "antigravity_history", "status": "unsupported", "records": 0, "complete": False},
                            {"source": "RAW_PRIVATE_TEXT", "status": "error", "records": 0, "complete": False}],
                "raw_text": "RAW_PRIVATE_TEXT", "native_session_id": "private-native-id"}

    def close(self):
        self.closed.set()


class InventoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.home = self.root / "home"
        self.workspace.mkdir()
        self.home.mkdir()
        self.data = self.root / "inventory"
        self.runtimes = []
        self.collectors = []
        CountingModel.fits = 0
        CountingModel.loads = 0

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.close()
            if runtime._thread is not None:
                runtime._thread.join(2)
        self.temp.cleanup()

    def collector_factory(self, *args, **kwargs):
        collector = FixtureCollector(*args, **kwargs)
        self.collectors.append(collector)
        return collector

    def runtime(self, **changes):
        options = {"home": self.home, "data_root": self.data, "interval": 60,
                   "collector_factory": self.collector_factory, "model_factory": CountingModel}
        options.update(changes)
        runtime = InventoryRuntime(self.workspace, **options)
        self.runtimes.append(runtime)
        return runtime

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Background fixture worker did not reach the required state: "
                  + json.dumps([runtime.snapshot() for runtime in self.runtimes], sort_keys=True))

    def test_constructor_and_snapshot_do_not_scan_write_train_or_touch_real_home(self):
        factory = Mock(side_effect=AssertionError("constructor must be lazy"))
        runtime = self.runtime(vault_factory=factory, collector_factory=factory)
        for _ in range(3):
            self.assertEqual(runtime.snapshot()["state"], "idle")
        self.assertFalse(self.data.exists())
        factory.assert_not_called()
        self.assertEqual(CountingModel.fits, 0)

    def test_first_launch_actual_fit_checkpoint_refresh_and_restart_without_retraining(self):
        runtime = self.runtime()
        self.assertTrue(runtime.start())
        self.assertFalse(runtime.start())
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        first = runtime.snapshot()
        self.assertEqual(first["evidence_samples"], 1)
        self.assertEqual(first["learning"]["checkpoint"], "fit")
        self.assertEqual(first["learning"]["state"], "feedback_trained")
        self.assertEqual(CountingModel.fits, 1)
        revision = first["learning"]["revision"]
        self.assertTrue(runtime.refresh())
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 2)
        self.assertEqual(runtime.snapshot()["learning"]["checkpoint"], "unchanged")
        self.assertEqual(CountingModel.fits, 1)
        self.assertTrue(runtime.close())
        self.assertEqual(runtime.snapshot()["state"], "closed")
        self.assertTrue(runtime.close())
        self.assertEqual(runtime.snapshot()["state"], "closed")
        self.assertFalse(runtime.refresh())
        self.assertFalse(runtime.start())
        restarted = self.runtime()
        self.assertTrue(restarted.start())
        self.wait_for(lambda: restarted.snapshot()["refreshes"] == 1)
        self.assertEqual(restarted.snapshot()["learning"]["checkpoint"], "reloaded")
        self.assertEqual(restarted.snapshot()["learning"]["revision"], revision)
        self.assertEqual(CountingModel.fits, 1)
        self.assertGreater(CountingModel.loads, 0)
        self.assertIsNotNone(restarted.router)

    def test_real_collector_automatically_reads_only_fixture_home_and_persists_history_features(self):
        history = self.home / ".codex" / "sessions" / "2026" / "11" / "02"
        history.mkdir(parents=True)
        records = [{"type": "session_meta", "payload": {"cwd": str(self.workspace)}},
                   {"timestamp": time.time() - 1, "type": "event_msg", "payload": {
                       "type": "user_message", "message": "Please design the frontend layout FIXTURE_PRIVATE_WORD"}}]
        (history / "rollout-fixture.jsonl").write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        runtime = self.runtime(collector_factory=None, device_probe=lambda: ([], {"state": "ready"}))
        failures = []
        completed = threading.Event()
        actual_cycle = runtime._cycle

        def checked_cycle():
            try:
                return actual_cycle()
            except Exception as error:
                failures.append(error)
                raise
            finally:
                completed.set()

        with patch.object(runtime, "_cycle", side_effect=checked_cycle):
            runtime.start()
            # Real Windows SQLite/file startup can exceed the small fake-worker
            # polling bound under concurrent QA. Assert actual cycle completion,
            # with a finite harness deadline, rather than a product latency SLA.
            self.assertTrue(completed.wait(15), runtime.snapshot())
            if failures:
                raise failures[0]
            self.assertEqual(runtime.snapshot()["refreshes"], 1)
        first = runtime.snapshot()
        self.assertEqual(first["evidence_samples"], 2, first)
        self.assertEqual(first["learning"]["state"], "inferred")
        self.assertTrue(any(row["source"] == "codex_history" for row in first["sources"]))
        runtime.close()
        vault = PrivateVault(self.workspace, home=self.home, data_root=self.data)
        samples = vault.preference_samples()
        vault.close()
        self.assertEqual(len(samples), 2)
        self.assertEqual({sample["purpose"] for sample in samples}, {"frontend", "design"})
        self.assertEqual(samples[0]["source_kind"], "observed_usage")
        self.assertEqual(samples[0]["target"], 1)
        self.assertNotIn("FIXTURE_PRIVATE_WORD", json.dumps(samples))
        self.assertNotIn("FIXTURE_PRIVATE_WORD", json.dumps(first))
        restarted = self.runtime(collector_factory=None, device_probe=lambda: ([], {"state": "ready"}))
        restarted.start()
        self.wait_for(lambda: restarted.snapshot()["refreshes"] == 1)
        self.assertEqual(restarted.snapshot()["evidence_samples"], 2)
        self.assertEqual(restarted.snapshot()["learning"]["checkpoint"], "reloaded")

    def test_sanitized_cached_summary_keeps_partial_and_unsupported_coverage(self):
        runtime = self.runtime()
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        snapshot = runtime.snapshot()
        self.assertEqual(snapshot["state"], "partial")
        self.assertFalse(snapshot["complete"])
        self.assertEqual(snapshot["sources"][1]["status"], "unsupported")
        self.assertEqual(snapshot["sources"][2]["source"], "other_adapter")
        for value in ("RAW_PRIVATE_TEXT", "private-native-id", str(self.home), "features", "source_ref"):
            self.assertNotIn(value, json.dumps(snapshot))
        self.assertFalse(snapshot["native_input_created"])
        self.assertEqual(snapshot["model_api_calls"], 0)
        snapshot["sources"].clear()
        with patch.object(runtime._collector, "collect_once", side_effect=AssertionError("snapshot cannot scan")), \
                patch.object(runtime._vault, "summary", side_effect=AssertionError("snapshot cannot read DB")):
            self.assertEqual(len(runtime.snapshot()["sources"]), 3)

    def test_failure_is_sanitized_and_refresh_can_recover(self):
        runtime = self.runtime(collector_factory=partial(FixtureCollector, fail=True))
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["state"] == "error")
        self.assertEqual(runtime.snapshot()["error_code"], "inventory_refresh_failed")
        self.assertNotIn("RAW_PRIVATE_TEXT", json.dumps(runtime.snapshot()))
        self.assertEqual(CountingModel.fits, 0)
        runtime._collector.fail = False
        runtime.refresh()
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        self.assertIsNone(runtime.snapshot()["error_code"])

    def test_cancellation_is_cooperative_and_storage_closes_after_worker(self):
        entered = threading.Event()

        class BlockingCollector(FixtureCollector):
            def collect_once(self, *, max_records):
                entered.set()
                self.closed.wait(5)
                return {"state": "partial", "complete": False, "sources": []}

        runtime = self.runtime(collector_factory=BlockingCollector)
        runtime.start()
        self.assertTrue(entered.wait(5))
        self.assertTrue(runtime.close())
        self.assertEqual(runtime.snapshot()["state"], "closed")
        self.assertFalse(runtime.snapshot()["worker_alive"])
        self.assertEqual(CountingModel.fits, 0)
        self.assertEqual(runtime.snapshot()["refreshes"], 0)

    def test_noncooperative_worker_is_not_falsely_reported_closed(self):
        entered = threading.Event()
        release = threading.Event()

        class SlowCollector(FixtureCollector):
            def collect_once(self, *, max_records):
                entered.set()
                release.wait(5)
                return {"state": "partial", "complete": False, "sources": []}

        runtime = self.runtime(collector_factory=SlowCollector, close_timeout=0.01)
        runtime.start()
        self.assertTrue(entered.wait(5))
        self.assertFalse(runtime.close())
        self.assertEqual(runtime.snapshot()["state"], "closing")
        self.assertTrue(runtime.snapshot()["worker_alive"])
        release.set()
        self.wait_for(lambda: not runtime.snapshot()["worker_alive"])
        self.assertEqual(runtime.snapshot()["state"], "closed")

    def test_workspace_replacement_is_rejected_before_any_new_collection(self):
        runtime = self.runtime()
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        calls = self.collectors[0].calls
        runtime.close()
        retired = self.root / "retired"
        self.workspace.rename(retired)
        self.workspace.mkdir()
        runtime = self.runtime()
        # Capture a valid identity, then replace it before worker initialization.
        self.workspace.rename(self.root / "replacement-retired")
        self.workspace.mkdir()
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["state"] == "error")
        self.assertEqual(self.collectors[0].calls, calls)
        self.assertEqual(len(self.collectors), 1)

    def test_invalid_checkpoint_is_refit_from_typed_evidence(self):
        runtime = self.runtime()
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        runtime.close()
        with_checkpoint = PrivateVault(self.workspace, home=self.home, data_root=self.data)
        checkpoint = with_checkpoint.get_checkpoint("preference_model")
        checkpoint["model"]["checksum"] = "0" * 64
        with_checkpoint.put_checkpoint("preference_model", checkpoint)
        with_checkpoint.close()
        restarted = self.runtime()
        restarted.start()
        self.wait_for(lambda: restarted.snapshot()["refreshes"] == 1)
        self.assertEqual(restarted.snapshot()["learning"]["checkpoint"], "fit")
        self.assertEqual(CountingModel.fits, 2)

    def test_malformed_checkpoint_json_recovers_after_actual_restart(self):
        runtime = self.runtime()
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        checkpoint_path = runtime._vault.db_path
        runtime.close()
        with closing(sqlite3.connect(checkpoint_path)) as db:
            db.execute("UPDATE checkpoints SET payload=? WHERE name=?", ("{", "preference_model"))
            db.commit()
        restarted = self.runtime()
        restarted.start()
        self.wait_for(lambda: restarted.snapshot()["refreshes"] == 1)
        self.assertEqual(restarted.snapshot()["learning"]["checkpoint"], "fit")
        self.assertEqual(restarted.snapshot()["learning"]["recovery"], "malformed_checkpoint")
        self.assertEqual(restarted.snapshot()["evidence_samples"], 1)
        self.assertEqual(CountingModel.fits, 2)
        restarted.close()
        vault = PrivateVault(self.workspace, home=self.home, data_root=self.data)
        repaired = vault.get_checkpoint("preference_model")
        vault.close()
        self.assertEqual(repaired["schema"], "avalokita.inventory-model/1")

    def test_storage_failure_is_not_misclassified_as_corrupt_checkpoint(self):
        class CheckpointStorageFailure(PrivateVault):
            def get_checkpoint(self, name):
                raise sqlite3.OperationalError("storage failure fixture")

        runtime = self.runtime(vault_factory=CheckpointStorageFailure)
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["state"] == "error")
        self.assertEqual(runtime.snapshot()["error_code"], "inventory_refresh_failed")
        self.assertIsNone(runtime.model)
        self.assertEqual(CountingModel.fits, 0)

    def test_application_owned_default_root_is_outside_workspace_under_fixture_home(self):
        runtime = self.runtime(data_root=None)
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        self.assertTrue(runtime._vault.directory.is_relative_to(self.home))
        self.assertFalse(runtime._vault.directory.is_relative_to(self.workspace))
        self.assertEqual(runtime.snapshot()["evidence_samples"], 1)

    def test_server_default_preserves_application_owned_data_root_and_explicit_override(self):
        factory = Mock()
        runtime = Runtime(self.workspace, discovery=False, inventory_factory=factory)
        self.assertIsNone(factory.call_args.kwargs["data_root"])
        self.assertIsNone(factory.call_args.kwargs["home"])
        self.assertFalse(factory.call_args.kwargs["enabled"])
        runtime.begin_closing()
        runtime = Runtime(self.workspace, discovery=False, inventory_home=self.home,
                          inventory_data_root=self.data, inventory_factory=factory)
        self.assertEqual(factory.call_args.kwargs["data_root"], self.data)
        self.assertEqual(factory.call_args.kwargs["home"], self.home)
        runtime.begin_closing()

    def test_unlabelled_evidence_does_not_become_a_trained_model(self):
        class UnknownCollector(FixtureCollector):
            def collect_once(self, *, max_records):
                sample = {"id": opaque("unknown"), "source_kind": "observed_usage", "source_ref": opaque("unknown-source"),
                          "event_ref": opaque("unknown-event"), "observed_at": 1700000000.0, "scope": self.vault.workspace_key,
                          "provider": "codex", "purpose": "implementation", "capability": None,
                          "features": featureize("A historical work request"), "target": None, "weight": 0.1}
                self.vault.put_evidence(sample)
                return {"state": "partial", "complete": False,
                        "sources": [{"source": "codex_history", "status": "partial", "records": 1, "complete": False}]}

        runtime = self.runtime(collector_factory=UnknownCollector)
        runtime.start()
        self.wait_for(lambda: runtime.snapshot()["refreshes"] == 1)
        self.assertEqual(runtime.snapshot()["evidence_samples"], 1)
        self.assertEqual(runtime.snapshot()["learning"]["state"], "untrained")
        self.assertEqual(runtime.snapshot()["learning"]["heads"], 0)

    def test_existing_disabled_server_constructor_cannot_scan_user_home(self):
        runtime = Runtime(self.workspace, discovery=False)
        self.runtimes.append(runtime.inventory)
        self.assertEqual(runtime.inventory.snapshot()["state"], "disabled")
        self.assertFalse((runtime.store.private / "inventory").exists())
        runtime.inventory.start()
        self.assertFalse((runtime.store.private / "inventory").exists())

    def test_shutdown_rejects_new_work_while_inventory_close_is_blocked(self):
        entered = threading.Event()
        release = threading.Event()
        runtime = Runtime(self.workspace, discovery=False)
        self.runtimes.append(runtime.inventory)

        def blocked_close():
            entered.set()
            release.wait(5)

        with patch.object(runtime.inventory, "close", side_effect=blocked_close):
            thread = threading.Thread(target=runtime.begin_closing, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertIsNone(runtime.try_acquire_work())
                self.assertTrue(runtime._closing)
                self.assertEqual(runtime._active_work, 0)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())

    def test_actual_server_first_launch_starts_own_worker_and_summary_is_cached(self):
        inventory_factory = partial(InventoryRuntime, collector_factory=self.collector_factory, model_factory=CountingModel)
        runtime = Runtime(self.workspace, discovery=False, inventory=True, inventory_home=self.home,
                          inventory_data_root=self.data, inventory_factory=inventory_factory)
        self.runtimes.append(runtime.inventory)
        server = Server(self.workspace, runtime=runtime)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        with patch.object(runtime, "start_peer_reconciler"), patch.object(runtime, "stop_peer_reconciler"), \
                patch.object(runtime.hosted, "start_service"), patch.object(runtime.hosted, "close"):
            thread.start()
            try:
                self.wait_for(lambda: runtime.inventory.snapshot()["refreshes"] == 1)
                observations = {"totals": {}, "participants": 0, "status": "ready", "error_code": None}
                with patch.object(runtime.bridge, "public_inspector", return_value=observations), \
                        patch.object(runtime.inventory, "refresh", side_effect=AssertionError("summary must be cached")), \
                        patch.object(runtime.inventory._collector, "collect_once", side_effect=AssertionError("summary cannot scan")):
                    self.assertEqual(runtime.summary()["inventory"]["evidence_samples"], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(runtime.inventory.snapshot()["state"], "closed")


if __name__ == "__main__":
    unittest.main()
