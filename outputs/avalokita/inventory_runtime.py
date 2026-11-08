"""Owned background collection and local posterior checkpoints.

No provider is launched and no native/model input is created here. Snapshots are
cached projections, so opening a summary never scans files or fits a model.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import stat
import threading
import time

from judgment import JudgmentRouter
from preference_learning import PreferenceModel


_CHECKPOINT = "preference_model"
_SCHEMA = "avalokita.inventory-runtime/1"
_SOURCE_STATUSES = {"ready", "empty", "missing", "unsupported", "unavailable", "partial",
                    "error", "stale", "storage_full", "closed"}


class InventoryRuntime:
    def __init__(self, workspace, *, home=None, data_root=None, device_probe=None,
                 enabled=True, interval=60.0, max_records=500, close_timeout=1.0,
                 vault_factory=None, collector_factory=None, model_factory=None):
        original = Path(workspace).absolute()
        self.workspace = original.resolve(strict=True)
        self._identity = self._directory_identity(original)
        if self._directory_identity(self.workspace) != self._identity:
            raise ValueError("inventory_workspace_identity")
        self.home = Path(home) if home is not None else None
        self.data_root = Path(data_root) if data_root is not None else None
        self.device_probe = device_probe
        if (type(interval) not in {int, float} or not math.isfinite(interval) or interval <= 0
                or type(max_records) is not int or not 1 <= max_records <= 500
                or type(close_timeout) not in {int, float} or not math.isfinite(close_timeout)
                or not 0 <= close_timeout <= 5):
            raise ValueError("inventory_worker_limits")
        self.enabled = bool(enabled)
        self.interval = float(interval)
        self.max_records = max_records
        self.close_timeout = float(close_timeout)
        self._vault_factory = vault_factory
        self._collector_factory = collector_factory
        self._model_factory = model_factory or PreferenceModel
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False
        self._vault = None
        self._collector = None
        self._model = None
        self._evidence_digest = None
        self._cache = {"schema": _SCHEMA, "state": "idle" if enabled else "disabled", "bounded": True,
                       "observed_at": None, "refreshes": 0, "complete": False, "records": 0,
                       "evidence_samples": 0, "sources": [], "learning": {"state": "untrained", "revision": 0},
                       "error_code": None, "worker_alive": False,
                       "native_input_created": False, "model_api_calls": 0}

    @staticmethod
    def _directory_identity(path):
        for current in (path, *path.parents):
            info = current.lstat()
            if (not stat.S_ISDIR(info.st_mode) or current.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise ValueError("inventory_linked_workspace")
        info = path.stat()
        return info.st_dev, info.st_ino

    def _guard(self):
        if self._directory_identity(self.workspace) != self._identity:
            raise ValueError("inventory_workspace_identity")

    def start(self):
        with self._lock:
            if not self.enabled or self._closed or self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._run, name="avalokita-inventory", daemon=True)
            self._cache.update(state="starting", worker_alive=True)
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._cache.update(state="error", worker_alive=False, error_code="worker_start_failed")
                return False
        return True

    def refresh(self):
        with self._lock:
            if not self.enabled or self._closed:
                return False
            self._wake.set()
        return True

    def snapshot(self):
        with self._lock:
            return deepcopy(self._cache)

    @property
    def model(self):
        """Private controller access returns a copy, never the fitting instance."""
        with self._lock:
            return self._model_factory.from_dict(self._model.to_dict()) if self._model is not None else None

    @property
    def router(self):
        model = self.model
        return JudgmentRouter(model) if model is not None else None

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            self._wake.set()
            worker = self._thread
            if worker is None:
                self._cache.update(state="closed", worker_alive=False)
                return True
            if not worker.is_alive():
                return True
            self._cache["state"] = "closing"
            collector = self._collector
        if collector is not None:
            # Collector.close only signals cooperative cancellation. The worker
            # retains exclusive ownership of vault operations and its DB close.
            try:
                collector.close()
            except Exception:
                with self._lock:
                    self._cache["error_code"] = "inventory_cancel_failed"
        if worker is not threading.current_thread():
            worker.join(self.close_timeout)
        # The worker exclusively owns and closes storage; never close its DB
        # underneath an unfinished collection or claim it has already stopped.
        return not worker.is_alive()

    def _initialize(self):
        if self._collector is not None:
            return
        self._guard()
        vault_factory = self._vault_factory
        collector_factory = self._collector_factory
        if vault_factory is None:
            from inventory_vault import PrivateVault
            vault_factory = PrivateVault
        if collector_factory is None:
            from inventory_sources import InventoryCollector
            collector_factory = InventoryCollector
        if self._stop.is_set():
            return
        if self._vault is None:
            self._vault = vault_factory(self.workspace, home=self.home, data_root=self.data_root)
        if self._stop.is_set():
            return
        self._collector = collector_factory(self._vault, home=self.home, device_probe=self.device_probe)

    @staticmethod
    def _count(value):
        return value if type(value) is int and 0 <= value <= 1000000 else 0

    @classmethod
    def _coverage(cls, sources):
        if not isinstance(sources, list):
            return []
        # Source identifiers are internal adapter names, never connector labels,
        # filesystem locators, error strings or historical/native identifiers.
        allowed = {"device", "workspace_config"}
        for provider in ("claude", "codex", "antigravity"):
            allowed.update({provider, "config:" + provider, "history:" + provider,
                            provider + ":config", provider + ":history",
                            provider + "_config", provider + "_history"})
        rows = []
        for row in sources[:32]:
            if not isinstance(row, dict):
                continue
            source = row.get("source")
            status = row.get("status")
            rows.append({"source": source if isinstance(source, str) and source in allowed else "other_adapter",
                         "status": status if isinstance(status, str) and status in _SOURCE_STATUSES else "unavailable",
                         "records": cls._count(row.get("records")), "complete": row.get("complete") is True})
        return rows

    def _cycle(self):
        self._guard()
        self._initialize()
        if self._stop.is_set():
            return
        report = self._collector.collect_once(max_records=self.max_records)
        self._guard()
        if self._stop.is_set():
            return
        samples = sorted(self._vault.preference_samples(), key=lambda sample: sample["id"])
        digest = hashlib.sha256(json.dumps(samples, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode("utf-8")).hexdigest()
        learning_action = "unchanged"
        recovery = None
        model = self._model
        if digest != self._evidence_digest or model is None:
            try:
                checkpoint = self._vault.get_checkpoint(_CHECKPOINT)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # A malformed document is recoverable from typed evidence. DB,
                # filesystem, identity and storage failures must still surface.
                checkpoint = None
                recovery = "malformed_checkpoint"
            if (isinstance(checkpoint, dict) and checkpoint.get("schema") == "avalokita.inventory-model/1"
                    and checkpoint.get("evidence_digest") == digest):
                try:
                    model = self._model_factory.from_dict(checkpoint["model"])
                    learning_action = "reloaded"
                except (KeyError, TypeError, ValueError):
                    model = None
                    recovery = "invalid_model_checkpoint"
            else:
                model = None
            if model is None:
                previous = self._model
                model = self._model_factory.from_dict(previous.to_dict()) if previous is not None else self._model_factory()
                model.fit(samples)
                self._guard()
                if self._stop.is_set():
                    return
                body = model.to_dict()
                self._vault.put_checkpoint(_CHECKPOINT, {"schema": "avalokita.inventory-model/1",
                    "revision": self._count(body.get("revision")), "evidence_digest": digest, "model": body})
                learning_action = "fit"
        self._guard()
        summary = self._vault.summary()
        if not isinstance(report, dict) or not isinstance(summary, dict):
            raise ValueError("inventory_invalid_report")
        body = model.to_dict()
        state = report.get("state")
        coverage = self._coverage(report.get("sources", []))
        if not coverage:
            state = "partial"
        if state not in {"ready", "partial", "error", "empty", "unavailable"}:
            state = "partial"
        heads = body.get("heads", {})
        trained = "inferred" if heads else "untrained"
        if any(head.get("source_counts", {}).get(source, 0) for head in heads.values()
               for source in ("human_explicit", "verified_outcome")):
            trained = "feedback_trained"
        with self._lock:
            if self._stop.is_set():
                return
            self._model = model
            self._evidence_digest = digest
            self._cache.update(state=state, observed_at=time.time(), complete=report.get("complete") is True and bool(coverage),
                refreshes=self._cache["refreshes"] + 1, records=self._count(summary.get("observations")),
                evidence_samples=self._count(summary.get("evidence_samples")), sources=coverage,
                learning={"state": trained, "heads": len(heads),
                          "revision": self._count(body.get("revision")), "checkpoint": learning_action,
                          "recovery": recovery}, error_code=None)

    def _run(self):
        terminal_error = None
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    with self._lock:
                        self._cache["state"] = "collecting"
                    self._cycle()
                except Exception:
                    # Paths, raw provider errors and history text stay private.
                    with self._lock:
                        self._cache.update(state="error", error_code="inventory_refresh_failed", complete=False)
                if self._stop.is_set():
                    break
                self._wake.wait(self.interval)
        finally:
            for owned in (self._collector, self._vault):
                if owned is not None:
                    try:
                        owned.close()
                    except Exception:
                        terminal_error = "inventory_close_failed"
            with self._lock:
                self._cache.update(state="closed" if self._closed else "error", worker_alive=False)
                if terminal_error:
                    self._cache["error_code"] = terminal_error
