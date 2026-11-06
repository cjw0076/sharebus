"""Private store tests use only owned temporary workspaces/account fixtures."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from inventory_vault import InventoryError, PrivateVault, opaque
from preference_learning import PreferenceModel, featureize


class PrivateVaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="avalokita-vault-test-")
        self.base = Path(self.temporary.name)
        self.workspace, self.home = self.base / "workspace", self.base / "home"
        self.workspace.mkdir()
        self.home.mkdir()
        self.root = self.base / "data"
        self.vault = PrivateVault(self.workspace, home=self.home, data_root=self.root)

    def tearDown(self):
        self.vault.close()
        self.temporary.cleanup()

    def observation(self, *, label="fixture", expires=None):
        now = time.time()
        return {"id": opaque(label), "kind": "mcp_server", "provider": "codex", "label": label,
                "device_key": self.vault.device_key, "workspace_key": self.vault.workspace_key,
                "status": "configured", "observed_at": now if expires is None else expires - 100,
                "expires_at": now + 3600 if expires is None else expires, "verified": False,
                "provenance": {"source_kind": "local_metadata", "source_ref": opaque("source"), "source_hash": opaque("bytes")}}

    def sample(self, **updates):
        result = {"id": opaque("sample"), "source_kind": "observed_usage", "source_ref": opaque("source"),
                  "event_ref": opaque("event"), "observed_at": time.time(), "scope": self.vault.workspace_key,
                  "provider": "codex", "purpose": "frontend", "capability": None,
                  "features": featureize("frontend layout", "frontend"), "target": 1, "weight": .1}
        result.update(updates)
        return result

    def test_restart_deduplicates_evidence_and_reloads_actual_model(self):
        evidence = self.sample()
        self.assertTrue(self.vault.put_evidence(evidence))
        self.assertFalse(self.vault.put_evidence(evidence))
        model = PreferenceModel()
        model.fit(self.vault.preference_samples())
        checkpoint = {"schema": "avalokita.inventory-model/1", "revision": 1,
                      "evidence_digest": opaque(json.dumps(evidence, sort_keys=True)), "model": model.to_dict()}
        self.vault.put_checkpoint("preference_model", checkpoint)
        self.vault.set_cursor("fixture", {"offset": 15, "prefix_hash": opaque("prefix")})
        self.vault.close()
        self.vault = PrivateVault(self.workspace, home=self.home, data_root=self.root)
        self.assertFalse(self.vault.put_evidence(evidence))
        self.assertEqual(self.vault.get_cursor("fixture")["offset"], 15)
        self.assertEqual(self.vault.get_checkpoint("preference_model"), checkpoint)
        restored = PreferenceModel.from_dict(self.vault.get_checkpoint("preference_model")["model"])
        self.assertEqual(restored.to_dict(), model.to_dict())
        self.assertEqual(self.vault.summary()["model_revisions"], {"preference_model": 1})
        self.vault.put_checkpoint("preference_model", checkpoint)
        self.assertEqual(self.vault.summary()["model_revisions"]["preference_model"], 1)

    def test_failures_preserve_last_observation_empty_and_expired_are_distinct(self):
        self.vault.put_inventory("device", [self.observation(expires=time.time() - 1)], status="ready", observed_at=time.time())
        self.assertEqual(self.vault.summary()["stale_observations"], 1)
        self.vault.put_inventory("device", [], status="unavailable", observed_at=time.time())
        summary = self.vault.summary()
        self.assertEqual(summary["observations"], 1)
        self.assertEqual(summary["sources"][0]["status"], "unavailable")
        self.vault.put_inventory("device", [], status="empty", observed_at=time.time())
        self.assertEqual(self.vault.summary()["observations"], 0)

    def test_inventory_validation_is_atomic_and_scope_is_local(self):
        valid = self.observation()
        wrong = self.observation(label="other")
        wrong["workspace_key"] = opaque("different")
        with self.assertRaises(InventoryError):
            self.vault.put_inventory("device", [valid, wrong], status="ready", observed_at=time.time())
        self.assertEqual(self.vault.summary()["observations"], 0)
        with self.assertRaises(InventoryError):
            self.vault.put_evidence(self.sample(scope=opaque("other_workspace")))
        self.assertTrue(self.vault.put_evidence(self.sample(scope="global_style")))

    def test_credential_fields_raw_urls_and_nonfinite_checkpoints_are_rejected(self):
        for payload in ({"env": {"a": "b"}}, {"headers": {}}, {"command": "anything"},
                        {"token": "value"}, {"safe": "https://private.example/?key=secret"},
                        {"safe": "person@example.com"}, {"weight": float("nan")},
                        {"native_session_id": "fixture"}):
            with self.subTest(payload=tuple(payload)):
                with self.assertRaises(InventoryError):
                    self.vault.put_checkpoint("fixture", payload)
        row = self.observation()
        row["env"] = {"SECRET": "DO_NOT_RETAIN"}
        with self.assertRaises(InventoryError):
            self.vault.put_inventory("codex_config", [row], status="ready", observed_at=time.time())
        self.assertNotIn(b"DO_NOT_RETAIN", self.vault.db_path.read_bytes())

    def test_untrusted_evidence_and_absent_negative_are_rejected(self):
        for changes in ({"source_kind": "peer"}, {"source_kind": "system_recommendation"},
                        {"target": 0}, {"weight": .9}, {"features": [1] * 64}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(InventoryError):
                    self.vault.put_evidence(self.sample(**changes))
        self.assertEqual(self.vault.preference_samples(), [])

    def test_same_evidence_id_cannot_change_feedback(self):
        row = self.sample(source_kind="human_explicit", weight=1)
        self.vault.put_evidence(row)
        changed = deepcopy(row)
        changed["target"] = 0
        with self.assertRaisesRegex(InventoryError, "evidence_conflict"):
            self.vault.put_evidence(changed)

    def test_future_evidence_admission_matches_learning_clock_skew(self):
        now = time.time()
        with patch("inventory_vault.time.time", return_value=now):
            self.assertTrue(self.vault.put_evidence(self.sample(observed_at=now + 300)))
            with self.assertRaisesRegex(InventoryError, "future_evidence"):
                self.vault.put_evidence(self.sample(id=opaque("future"), observed_at=now + 300.01))
        self.vault.close()
        self.vault = PrivateVault(self.workspace, home=self.home, data_root=self.root)
        self.assertEqual(len(self.vault.preference_samples()), 1)

    def test_workspace_replacement_refuses_active_vault(self):
        self.workspace.rename(self.base / "old_workspace")
        self.workspace.mkdir()
        with self.assertRaisesRegex(InventoryError, "workspace_changed"):
            self.vault.set_cursor("fixture", {"offset": 1})

    def test_database_replacement_and_hardlink_are_rejected(self):
        original = self.vault.db_path
        displaced = original.with_suffix(".old")
        original.rename(displaced)
        shutil.copyfile(displaced, original)
        with self.assertRaisesRegex(InventoryError, "storage_changed"):
            self.vault.summary()
        self.vault.close()
        self.vault = PrivateVault(self.workspace, home=self.home, data_root=self.root)
        alias = self.base / "linked.sqlite3"
        os.link(original, alias)
        with self.assertRaisesRegex(InventoryError, "linked_file"):
            self.vault.summary()
        alias.unlink()

    def test_other_workspace_does_not_receive_features_or_checkpoint(self):
        self.vault.put_evidence(self.sample())
        self.vault.put_checkpoint("fixture", {"revision": 1})
        second = self.base / "second"
        second.mkdir()
        with_vault = PrivateVault(second, home=self.home, data_root=self.root)
        try:
            self.assertEqual(with_vault.preference_samples(), [])
            self.assertIsNone(with_vault.get_checkpoint("fixture"))
            self.assertNotEqual(with_vault.workspace_key, self.vault.workspace_key)
        finally:
            with_vault.close()

    def test_summary_is_sanitized_and_closed_vault_refuses_io(self):
        self.vault.put_evidence(self.sample())
        self.vault.set_cursor("fixture", {"offset": 5, "prefix_hash": opaque("private")})
        encoded = json.dumps(self.vault.summary())
        for forbidden in (str(self.home), str(self.workspace), "features", "prefix_hash", "event_ref"):
            self.assertNotIn(forbidden, encoded)
        self.vault.close()
        with self.assertRaisesRegex(InventoryError, "closed"):
            self.vault.summary()


if __name__ == "__main__":
    unittest.main()
