"""Dependency-free broker identity checks and explicit sidecar registration.

Only the selected workspace's runtime, public health endpoint and registry are
used here. No agent credentials, provider discovery or native adapters are read.
"""
from __future__ import annotations

import hashlib
import errno
import json
import os
from pathlib import Path
import stat
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


REGISTRY = Path.home() / ".gemini/config/sidecars/sharebus/workspaces"
_IDENTITY = ("workspace_id", "nonce", "pid")


class RuntimeVerificationError(RuntimeError):
    """A credential-free runtime or registry error."""

    def __init__(self, message, reason_code="uncertain", *, runtime=None):
        super().__init__(message)
        self.reason_code = reason_code
        self.runtime = runtime


class _Guard:
    """Preserve the first refusal across nested helpers and temporary cleanup."""

    def __init__(self, callback):
        self.callback = callback
        self.error = None

    def __call__(self):
        if self.error is not None:
            raise self.error
        if self.callback is not None:
            try:
                self.callback()
            except Exception as error:
                self.error = error
                raise


def _guard_for(callback):
    return callback if isinstance(callback, _Guard) else _Guard(callback)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeVerificationError("Sharebus refused an HTTP redirect.")


def _safe(root: Path, target: Path, *, directory: bool = False, workspace_guard=None) -> Path:
    guard = _guard_for(workspace_guard)
    current = root
    for part in target.relative_to(root).parts:
        current /= part
        guard()
        try:
            info = current.lstat()
        except FileNotFoundError:
            guard()
            continue
        guard()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeVerificationError("Sharebus metadata paths must not contain links or reparse points.", "invalid_metadata")
    if directory:
        guard()
        target.mkdir(parents=True, exist_ok=True)
        guard()
    return target


def _read_private(path: Path, root: Path, *, json_file: bool = False, workspace_guard=None):
    guard = _guard_for(workspace_guard)
    try:
        _safe(root, path, workspace_guard=guard)
        guard()
        info = path.stat()
        guard()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise RuntimeVerificationError("Sharebus private file is invalid.", "invalid_metadata")
        guard()
        with path.open("rb") as stream:
            guard()
            raw = stream.read(65537)
            guard()
        if len(raw) > 65536:
            raise RuntimeVerificationError("Sharebus private file is invalid.", "invalid_metadata")
        content = raw.decode("utf-8")
        return json.loads(content) if json_file else content.strip()
    except (OSError, ValueError) as error:
        if guard.error is not None:
            raise guard.error
        reason = ("runtime_missing" if isinstance(error, FileNotFoundError) and path == root / ".sharebus/private/runtime.json"
                  else "denied" if isinstance(error, PermissionError)
                  else "invalid_metadata" if isinstance(error, (ValueError, IsADirectoryError, NotADirectoryError)) else "uncertain")
        raise RuntimeVerificationError("Sharebus runtime could not be read in this workspace.", reason) from None


def _same_generation(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    return all(type(expected.get(key)) is type(current.get(key)) and expected.get(key) == current.get(key)
               for key in _IDENTITY)


def _verified_runtime(root: Path, *, read_private=_read_private, opener=None, timeout: float = 5,
                      workspace_guard=None) -> dict[str, Any]:
    """Internal seam for the MCP client's existing private reader and opener."""
    guard = _guard_for(workspace_guard)
    guard()
    if read_private is _read_private:
        value = read_private(root / ".sharebus/private/runtime.json", root, json_file=True, workspace_guard=guard)
    else:
        # Legacy readers keep their contract; only the default reader guards its internal I/O.
        value = read_private(root / ".sharebus/private/runtime.json", root, json_file=True)
    guard()
    if not isinstance(value, dict):
        raise RuntimeVerificationError("Sharebus runtime is invalid.", "invalid_metadata")
    try:
        parsed = urlsplit(value.get("url", ""))
        port = parsed.port
        workspace = value.get("workspace")
        guard()
        matches_workspace = isinstance(workspace, str) and Path(workspace).resolve() == root
        guard()
    except (OSError, AttributeError, TypeError, ValueError):
        if guard.error is not None:
            raise guard.error
        raise RuntimeVerificationError("Sharebus runtime URL or workspace is invalid.", "invalid_metadata") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not port
            or parsed.username is not None or parsed.password is not None or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or not matches_workspace):
        raise RuntimeVerificationError("Sharebus runtime must point to this workspace's loopback daemon.", "invalid_metadata")
    if (type(value.get("pid")) is not int or value["pid"] <= 0
            or not isinstance(value.get("nonce"), str) or len(value["nonce"]) < 16
            or not isinstance(value.get("workspace_id"), str) or not value["workspace_id"]):
        raise RuntimeVerificationError("Sharebus runtime process identity is invalid.", "invalid_metadata")
    probe = Request(value["url"].rstrip("/") + "/health")
    try:
        client = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
        guard()
        with client.open(probe, timeout=timeout) as response:
            guard()
            raw = response.read(65537)
            guard()
        if len(raw) > 65536:
            raise RuntimeVerificationError("Sharebus health response is invalid.", "invalid_metadata")
        health = json.loads(raw)
    except (OSError, ValueError) as error:
        if guard.error is not None:
            raise guard.error
        cause = error.reason if isinstance(error, URLError) else error
        reason = ("denied" if isinstance(error, HTTPError) and error.code in {401, 403} or isinstance(error, PermissionError)
                  else "connect_refused" if isinstance(cause, ConnectionRefusedError)
                  or isinstance(cause, OSError) and (cause.errno == errno.ECONNREFUSED or getattr(cause, "winerror", None) == 10061)
                  else "invalid_metadata" if isinstance(error, ValueError) else "uncertain")
        identity = {key: value[key] for key in ("workspace", "url", *_IDENTITY)} if reason == "connect_refused" else None
        raise RuntimeVerificationError("Sharebus daemon identity could not be verified; start or reconnect this workspace.",
                                       reason, runtime=identity) from None
    if not isinstance(health, dict):
        raise RuntimeVerificationError("Sharebus health response is invalid.", "invalid_metadata")
    if health.get("ok") is not True or not _same_generation(health, value):
        raise RuntimeVerificationError("Sharebus runtime is stale; no credential or message was sent.", "generation_mismatch")
    guard()
    return value


def verified_runtime(workspace: str | Path, *, workspace_guard=None) -> dict[str, Any]:
    """Verify the scoped private runtime and its public loopback process identity."""
    guard = _guard_for(workspace_guard)
    try:
        guard()
        lexical = Path(workspace).absolute()
        _safe(Path(lexical.anchor), lexical, workspace_guard=guard)
        guard()
        root = Path(workspace).resolve(strict=True)
        guard()
        if not root.is_dir():
            raise RuntimeVerificationError("Sharebus workspace does not exist.")
        guard()
        return _verified_runtime(root, workspace_guard=guard)
    except (OSError, TypeError, ValueError) as error:
        if guard.error is not None:
            raise guard.error
        raise RuntimeVerificationError("Sharebus workspace or runtime is invalid.",
                                       "denied" if isinstance(error, PermissionError) else "uncertain") from None


def _register_workspace(workspace: Path, runtime: dict[str, Any] | None = None,
                        registry_path: Path | None = None, *, _verify_runtime=verified_runtime,
                        workspace_guard=None) -> Path:
    """Internal seam for AGY's existing call-time runtime verification wrapper."""
    guard = _guard_for(workspace_guard)

    def verify(root):
        guard()
        value = (_verify_runtime(root, workspace_guard=guard) if _verify_runtime is verified_runtime
                 else _verify_runtime(root))
        guard()
        return value

    try:
        guard()
        root = Path(workspace).resolve(strict=True)
        guard()
        current = verify(root)
        if runtime is not None and (not isinstance(runtime, dict) or not _same_generation(runtime, current)):
            raise RuntimeVerificationError("Workspace registration does not match its live broker.", "generation_mismatch")
        registry = Path(REGISTRY if registry_path is None else registry_path).absolute()
        anchor = Path(registry.anchor)
        _safe(anchor, registry, directory=True, workspace_guard=guard)
        digest = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()
        path = _safe(anchor, registry / (digest + ".json"), workspace_guard=guard)
        value = {"version": 1, "workspace": str(root), "workspace_id": current["workspace_id"],
                 "runtime_nonce": current["nonce"], "registered_at": time.time()}
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            guard()
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                guard()
                json.dump(value, stream, ensure_ascii=False)
                guard()
                stream.flush()
                guard()
                os.fsync(stream.fileno())
                guard()
            if workspace_guard is not None and not _same_generation(current, verify(root)):
                raise RuntimeVerificationError("Workspace registration does not match its live broker.", "generation_mismatch")
            guard()
            os.replace(temporary, path)
            guard()
        finally:
            if guard.error is None:
                guard()
                if temporary.exists():
                    guard()
                    temporary.unlink()
                    guard()
        return path
    except (OSError, TypeError, ValueError):
        if guard.error is not None:
            raise guard.error
        raise RuntimeVerificationError("Sharebus workspace could not be registered safely.") from None


def register_workspace(workspace: Path, runtime: dict[str, Any] | None = None,
                       registry_path: Path | None = None, *, workspace_guard=None) -> Path:
    """Register this explicit workspace after live verification, never by a supplied dict."""
    return _register_workspace(workspace, runtime, registry_path, workspace_guard=workspace_guard)
