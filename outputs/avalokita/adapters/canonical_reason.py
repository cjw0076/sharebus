"""Trusted exact Reason transport configuration; no human authority or native I/O.

Only the application host supplies this object. Renderer/peer payloads cannot
select an executable, binding, engine pin or transport. Canonical dispatch rows
retain their own immutable selection; this configuration cannot change it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import os
from pathlib import Path

from adapters.reason_engine import NativeBinding, ReasonEngineError, _plain


class CanonicalReasonTransport:
    def __init__(self, *, engine_path: Path | str, engine_sha256: str,
                 bindings: Mapping[str, NativeBinding], process_factory=None):
        self.engine_path = Path(engine_path)
        self.engine_sha256 = engine_sha256
        if (not isinstance(engine_sha256, str) or len(engine_sha256) != 64
                or any(char not in "0123456789abcdef" for char in engine_sha256)
                or not isinstance(bindings, Mapping) or not bindings):
            raise ReasonEngineError("An explicit engine pin and exact native bindings are required")
        self.bindings = dict(bindings)
        self.process_factory = process_factory
        for actor, binding in self.bindings.items():
            if (not isinstance(actor, str) or not 1 <= len(actor) <= 128
                    or any(ord(char) < 32 for char in actor)
                    or not isinstance(binding, NativeBinding) or binding.provider != "codex"):
                raise ReasonEngineError("Only exact trusted Codex human-input bindings are supported")

    def selection(self, actor_id: str, native_id: str, workspace: Path, *, check_files=False) -> dict | None:
        """Freeze configuration without blocking a control lock on filesystem I/O.

        The deliver path checks files outside all control locks. The frozen
        engine validates the executable hash and exact native target itself.
        """
        binding = self.bindings.get(actor_id)
        if binding is None:
            return None
        if (binding.native_id != native_id or not Path(binding.workspace).is_absolute()
                or os.path.normcase(os.path.abspath(binding.workspace)) != os.path.normcase(os.path.abspath(workspace))
                or not binding.rollout_path or not binding.executable
                or not isinstance(binding.executable_sha256, str) or len(binding.executable_sha256) != 64
                or any(char not in "0123456789abcdef" for char in binding.executable_sha256)):
            raise ReasonEngineError("Trusted Reason binding does not match this exact workspace recipient")
        if check_files:
            for path in (self.engine_path, Path(binding.rollout_path), Path(binding.executable)):
                _plain(path, require_file=True)
        return {"transport": "reason", "engine_path": str(self.engine_path),
                "engine_sha256": self.engine_sha256, "binding": asdict(binding)}
