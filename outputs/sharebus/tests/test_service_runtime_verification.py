"""Increment 3 metadata boundaries, using Temp files and fake HTTP only."""
from contextlib import nullcontext
import ast
import errno
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from urllib.error import HTTPError, URLError

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))
import sidecar_registry as registry
from store import BusError


@pytest.fixture
def metadata(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    private = root / ".sharebus/private"
    private.mkdir(parents=True)
    runtime = {"workspace": str(root.resolve()), "url": "http://127.0.0.1:12345", "workspace_id": "fixture-id",
               "pid": 123, "nonce": "fixture-original-generation"}
    path = private / "runtime.json"
    path.write_text(json.dumps(runtime), encoding="utf-8")
    target = tmp_path / "registry"
    monkeypatch.setattr(registry, "REGISTRY", target)
    state = {"root": root, "path": path, "runtime": runtime, "registry": target, "requests": []}

    def opened(request, **kwargs):
        state["requests"].append((request, kwargs))
        if "error" in state:
            raise state["error"]
        raw = json.dumps(state.get("health", {"ok": True, **runtime})).encode()
        return nullcontext(SimpleNamespace(read=lambda maximum: raw))

    monkeypatch.setattr(registry, "build_opener", lambda *args: SimpleNamespace(open=opened))
    return state


@pytest.mark.parametrize("kind,reason", [
    ("missing", "runtime_missing"), ("directory", "invalid_metadata"), ("json", "invalid_metadata"),
    ("denied_file", "denied"), ("http401", "denied"), ("http403", "denied"),
    ("refused", "connect_refused"), ("direct_refused", "connect_refused"),
    ("timeout", "uncertain"), ("reset", "uncertain"), ("string_refusal", "uncertain"),
    ("http500", "uncertain"), ("redirect", "uncertain"), ("generation", "generation_mismatch"),
    ("bad_health", "invalid_metadata"),
])
def test_typed_reasons_do_not_confuse_absence_and_uncertainty(metadata, monkeypatch, kind, reason):
    item = metadata
    if kind == "missing":
        item["path"].unlink()
    elif kind == "directory":
        item["path"].unlink()
        item["path"].mkdir()
    elif kind == "json":
        item["path"].write_text("invalid-json", encoding="utf-8")
    elif kind == "denied_file":
        original = Path.open

        def opened(path, *args, **kwargs):
            if path == item["path"]:
                raise PermissionError("fixture-private-value")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", opened)
    elif kind.startswith("http"):
        item["error"] = HTTPError(item["runtime"]["url"], int(kind[4:]), "fixture-private-value", {}, None)
    elif kind == "refused":
        item["error"] = URLError(ConnectionRefusedError(errno.ECONNREFUSED, "fixture-private-value"))
    elif kind == "direct_refused":
        item["error"] = ConnectionRefusedError(errno.ECONNREFUSED, "fixture-private-value")
    elif kind == "timeout":
        item["error"] = URLError(TimeoutError("fixture-private-value"))
    elif kind == "reset":
        item["error"] = URLError(ConnectionResetError("fixture-private-value"))
    elif kind == "string_refusal":
        item["error"] = URLError("Connection refused")
    elif kind == "redirect":
        item["error"] = registry.RuntimeVerificationError("Sharebus refused an HTTP redirect.")
    elif kind == "generation":
        item["health"] = {"ok": True, **item["runtime"], "nonce": "fixture-next-generation"}
    elif kind == "bad_health":
        item["health"] = "invalid shape"
    with pytest.raises(registry.RuntimeVerificationError) as caught:
        registry.verified_runtime(item["root"], workspace_guard=lambda: None)
    assert caught.value.reason_code == reason
    assert "fixture-private-value" not in str(caught.value)
    assert caught.value.runtime == (item["runtime"] if reason == "connect_refused" else None)
    assert not item["registry"].exists()


def test_missing_workspace_is_uncertain_and_wrong_workspace_never_reaches_http(metadata):
    item = metadata
    with pytest.raises(registry.RuntimeVerificationError) as caught:
        registry.verified_runtime(item["root"] / "absent")
    assert caught.value.reason_code == "uncertain"
    item["path"].write_text(json.dumps({**item["runtime"], "workspace": str(item["root"].parent)}), encoding="utf-8")
    with pytest.raises(registry.RuntimeVerificationError) as caught:
        registry.verified_runtime(item["root"])
    assert caught.value.reason_code == "invalid_metadata" and not item["requests"]


@pytest.mark.parametrize("operation", ["verify", "register"])
@pytest.mark.parametrize("error_type", [ValueError, BusError])
def test_guard_refuses_before_any_resolve_or_lexical_io(metadata, monkeypatch, operation, error_type):
    item = metadata
    original = error_type("fixture-private-value")
    calls = []

    def guard():
        calls.append("guard")
        raise original

    def forbidden(*args, **kwargs):
        pytest.fail("Lexical I/O followed the first guard refusal")

    with monkeypatch.context() as scoped:
        for name in ("resolve", "lstat", "stat", "open", "exists", "mkdir", "unlink"):
            scoped.setattr(Path, name, forbidden)
        scoped.setattr(registry.os, "open", forbidden)
        scoped.setattr(registry.os, "replace", forbidden)
        with pytest.raises(error_type) as caught:
            if operation == "verify":
                registry.verified_runtime(item["root"], workspace_guard=guard)
            else:
                registry.register_workspace(item["root"], runtime=item["runtime"],
                                            registry_path=item["registry"], workspace_guard=guard)
    assert caught.value is original and calls == ["guard"]


@pytest.mark.parametrize("boundary", ["read_open", "http_open", "temporary_open", "replace"])
def test_one_shot_guard_refusal_stays_first_and_prevents_cleanup_io(metadata, monkeypatch, boundary):
    item = metadata
    original = BusError("fixture-private-value", 503)
    state = {"armed": False, "refused": False, "post_refusal_io": []}

    def guard():
        if state["armed"]:
            state["refused"] = True
            state["armed"] = False  # A later call would recover; the operation must latch this one.
            raise original

    def instrument(owner, name, arm=False):
        old = getattr(owner, name)

        def wrapped(*args, **kwargs):
            if state["refused"]:
                state["post_refusal_io"].append(name)
                raise AssertionError("I/O after observed refusal")
            value = old(*args, **kwargs)
            if arm:
                state["armed"] = True
            return value
        monkeypatch.setattr(owner, name, wrapped)

    with monkeypatch.context() as scoped:
        # Use scoped patching so test cleanup is outside the observed operation.
        saved = monkeypatch
        monkeypatch = scoped
        for name in ("resolve", "lstat", "stat", "open", "exists", "mkdir", "unlink"):
            instrument(Path, name, boundary == "read_open" and name == "open")
        instrument(registry.os, "open", boundary == "temporary_open")
        instrument(registry.os, "replace", boundary == "replace")
        if boundary == "http_open":
            old = registry.build_opener

            def opener(*args):
                client = old(*args)
                opened = client.open

                def request(*args, **kwargs):
                    response = opened(*args, **kwargs)
                    state["armed"] = True
                    return response
                return SimpleNamespace(open=request)
            monkeypatch.setattr(registry, "build_opener", opener)
        with pytest.raises(BusError) as caught:
            registry.register_workspace(item["root"], runtime=item["runtime"],
                                        registry_path=item["registry"], workspace_guard=guard)
        monkeypatch = saved
    assert caught.value is original and state["refused"] and state["post_refusal_io"] == []


def test_legacy_reader_and_verifier_keep_their_default_signature(metadata):
    item = metadata
    reads, checks = [], []

    def reader(path, root, *, json_file=False):
        reads.append((path, root, json_file))
        return item["runtime"]

    def verify(root):
        checks.append(root)
        return item["runtime"]

    assert registry._verified_runtime(item["root"], read_private=reader, workspace_guard=lambda: None) == item["runtime"]
    path = registry._register_workspace(item["root"], item["runtime"], item["registry"],
                                        _verify_runtime=verify, workspace_guard=lambda: None)
    assert reads == [(item["path"], item["root"], True)]
    assert checks == [item["root"], item["root"]]
    assert json.loads(path.read_text(encoding="utf-8"))["runtime_nonce"] == item["runtime"]["nonce"]


def test_old_error_constructor_and_stdlib_only_imports_remain_supported():
    assert registry.RuntimeVerificationError("old-caller").reason_code == "uncertain"
    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    imports = {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert imports <= sys.stdlib_module_names | {"__future__"}
