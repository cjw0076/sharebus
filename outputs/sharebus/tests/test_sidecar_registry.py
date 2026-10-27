"""Scoped metadata fixtures only; no broker, native client or model is launched."""
from __future__ import annotations

import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
PRODUCT = HERE.parent / "avalokita"
sys.path.insert(0, str(HERE))

import sidecar_registry as registry


@pytest.fixture
def live_runtime(tmp_path):
    workspace = tmp_path / "workspace"
    private = workspace / ".sharebus/private"
    private.mkdir(parents=True)
    runtime = {"workspace": str(workspace.resolve()), "workspace_id": "fixture-workspace",
               "nonce": "fixture-broker-instance", "pid": 123}
    state = {"requests": [], "runtime": runtime, "registry": tmp_path / "registry"}

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append({"path": self.path, "headers": dict(self.headers),
                                      "body": self.rfile.read(int(self.headers.get("Content-Length", 0)))})
            self.send_response(state.get("status", 200))
            if "location" in state:
                self.send_header("Location", state["location"])
            self.end_headers()
            health = state.get("health", {"ok": True, **{key: runtime[key] for key in ("workspace_id", "nonce", "pid")}})
            self.wfile.write(state.get("raw", json.dumps(health).encode()))

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    runtime["url"] = f"http://127.0.0.1:{server.server_port}"
    path = private / "runtime.json"
    path.write_text(json.dumps(runtime), encoding="utf-8")
    state.update(workspace=workspace.resolve(), path=path, url=runtime["url"])
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def save_runtime(fixture, value):
    fixture["path"].write_text(json.dumps(value), encoding="utf-8")


def assert_no_registry(fixture):
    assert not fixture["registry"].exists()


def test_live_registration_reads_only_scoped_runtime_and_sends_no_authority(live_runtime, monkeypatch):
    fixture = live_runtime
    original_open = Path.open

    def scoped_open(path, mode="r", *args, **kwargs):
        if "r" in mode:
            assert path == fixture["path"], "Metadata verification read an unrelated private file"
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", scoped_open)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: pytest.fail("No home discovery is allowed")))
    assert registry.verified_runtime(fixture["workspace"]) == fixture["runtime"]
    path = registry.register_workspace(fixture["workspace"], dict(fixture["runtime"]), fixture["registry"])
    monkeypatch.setattr(Path, "open", original_open)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert set(record) == {"version", "workspace", "workspace_id", "runtime_nonce", "registered_at"}
    assert record["workspace"] == str(fixture["workspace"])
    assert record["workspace_id"] == "fixture-workspace"  # Non-UUID metadata remains supported.
    assert record["runtime_nonce"] == fixture["runtime"]["nonce"]
    assert len(fixture["requests"]) == 2
    for request in fixture["requests"]:
        assert request["path"] == "/health" and request["body"] == b""
        assert not any(key.lower() in {"authorization", "cookie", "x-sharebus-request"} for key in request["headers"])
    assert set(path.name for path in (fixture["workspace"] / ".sharebus/private").iterdir()) == {"runtime.json"}


def test_supplied_dictionary_cannot_replace_live_runtime_proof(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "registry"
    with pytest.raises(registry.RuntimeVerificationError):
        registry.register_workspace(workspace, {"workspace_id": "fixture", "nonce": "fixture-instance", "pid": 123}, target)
    assert not target.exists() and list(workspace.iterdir()) == []


@pytest.mark.parametrize("key,value", [
    ("workspace_id", "another-workspace"), ("nonce", "another-broker-instance"), ("pid", 124),
    ("pid", True), ("workspace_id", None),
])
def test_expected_generation_mismatch_never_writes_registry(live_runtime, key, value):
    fixture = live_runtime
    if key == "pid" and value is True:
        fixture["runtime"]["pid"] = 1
        save_runtime(fixture, fixture["runtime"])
    expected = dict(fixture["runtime"], **{key: value})
    with pytest.raises(registry.RuntimeVerificationError, match="does not match"):
        registry.register_workspace(fixture["workspace"], expected, fixture["registry"])
    assert_no_registry(fixture)


def test_registration_replaces_only_same_workspace_record_atomically(live_runtime, monkeypatch):
    fixture = live_runtime
    path = registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    previous = path.read_bytes()
    fixture["runtime"]["nonce"] = "next-fixture-broker-instance"
    save_runtime(fixture, fixture["runtime"])
    real_replace = os.replace
    calls = []

    def replace(source, target):
        assert source.parent == target.parent == fixture["registry"]
        assert target.read_bytes() == previous
        assert json.loads(source.read_text(encoding="utf-8"))["runtime_nonce"] == fixture["runtime"]["nonce"]
        calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(registry.os, "replace", replace)
    assert registry.register_workspace(fixture["workspace"], fixture["runtime"], fixture["registry"]) == path
    assert len(calls) == 1 and list(fixture["registry"].iterdir()) == [path]
    assert path.read_bytes() != previous


def test_failed_atomic_replace_preserves_record_and_cleans_temporary(live_runtime, monkeypatch):
    fixture = live_runtime
    path = registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    previous = path.read_bytes()

    def fail(*args):
        raise OSError("private-fixture-value-must-not-appear")

    monkeypatch.setattr(registry.os, "replace", fail)
    with pytest.raises(registry.RuntimeVerificationError) as error:
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    assert "private-fixture-value" not in str(error.value)
    assert path.read_bytes() == previous and list(fixture["registry"].iterdir()) == [path]


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:12345", "http://example.invalid:12345", "http://localhost:12345",
    "http://[::1]:12345", "http://127.0.0.2:12345", "http://127.0.0.1", "http://127.0.0.1:0",
    "http://user:private-fixture-value@127.0.0.1:12345", "http://@127.0.0.1:12345",
    "http://127.0.0.1:12345/path", "http://127.0.0.1:12345?query=1", "http://127.0.0.1:12345#fragment",
    "http://127.0.0.1:bad", None, [], {}, 123,
])
def test_invalid_runtime_address_is_rejected_before_network_or_registry(live_runtime, monkeypatch, url):
    fixture = live_runtime
    save_runtime(fixture, dict(fixture["runtime"], url=url))
    monkeypatch.setattr(registry, "build_opener", lambda *args: pytest.fail("Invalid URL reached networking"))
    with pytest.raises(registry.RuntimeVerificationError) as error:
        registry.register_workspace(fixture["workspace"], fixture["runtime"], fixture["registry"])
    assert "private-fixture-value" not in str(error.value)
    assert not fixture["requests"]
    assert_no_registry(fixture)


@pytest.mark.parametrize("key,value", [
    ("workspace", "/another-fixture-workspace"), ("workspace", None), ("workspace", []),
    ("pid", True), ("pid", 0), ("pid", "123"), ("nonce", "short"), ("nonce", None),
    ("workspace_id", ""), ("workspace_id", None), ("workspace_id", 123),
])
def test_invalid_runtime_identity_is_rejected_without_registration(live_runtime, key, value):
    fixture = live_runtime
    save_runtime(fixture, dict(fixture["runtime"], **{key: value}))
    with pytest.raises(registry.RuntimeVerificationError):
        registry.register_workspace(fixture["workspace"], fixture["runtime"], fixture["registry"])
    assert not fixture["requests"]
    assert_no_registry(fixture)


@pytest.mark.parametrize("raw", [b"[]", b"null", b"invalid fixture JSON", b"\xff", b" " * 65537],
                         ids=["array", "null", "invalid-json", "invalid-utf8", "oversized"])
def test_invalid_or_oversized_runtime_never_writes_registry(live_runtime, raw):
    fixture = live_runtime
    fixture["path"].write_bytes(raw)
    with pytest.raises(registry.RuntimeVerificationError):
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    assert not fixture["requests"]
    assert_no_registry(fixture)


@pytest.mark.parametrize("key,value", [
    ("workspace_id", "another-workspace"), ("nonce", "another-broker-instance"), ("pid", 124),
    ("pid", True), ("ok", False), ("ok", 1),
])
def test_wrong_health_identity_cannot_be_overridden_by_supplied_runtime(live_runtime, key, value):
    fixture = live_runtime
    if key == "pid" and value is True:
        fixture["runtime"]["pid"] = 1
        save_runtime(fixture, fixture["runtime"])
    fixture["health"] = {"ok": True, **{key: fixture["runtime"][key] for key in ("workspace_id", "nonce", "pid")}, key: value}
    with pytest.raises(registry.RuntimeVerificationError, match="stale"):
        registry.register_workspace(fixture["workspace"], fixture["runtime"], fixture["registry"])
    assert len(fixture["requests"]) == 1
    assert_no_registry(fixture)


@pytest.mark.parametrize("raw", [b"[]", b"invalid fixture JSON", b"\xff", b" " * 65537],
                         ids=["array", "invalid-json", "invalid-utf8", "oversized"])
def test_invalid_or_oversized_health_never_writes_registry(live_runtime, raw):
    fixture = live_runtime
    fixture["raw"] = raw
    with pytest.raises(registry.RuntimeVerificationError):
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    assert_no_registry(fixture)


def test_exact_private_and_health_size_limits_remain_supported(live_runtime):
    fixture = live_runtime
    fixture["path"].write_bytes(fixture["path"].read_bytes().ljust(65536, b" "))
    fixture["raw"] = json.dumps({"ok": True, **fixture["runtime"]}).encode().ljust(65536, b" ")
    assert registry.verified_runtime(fixture["workspace"]) == fixture["runtime"]


@pytest.mark.parametrize("value", [123, [], "bad\0path"])
def test_invalid_registry_location_is_rejected_without_writing(live_runtime, value):
    fixture = live_runtime
    with pytest.raises(registry.RuntimeVerificationError):
        registry.register_workspace(fixture["workspace"], registry_path=value)
    assert_no_registry(fixture)


def test_registry_location_must_be_a_directory(live_runtime):
    fixture = live_runtime
    fixture["registry"].write_text("preserve fixture bytes")
    with pytest.raises(registry.RuntimeVerificationError):
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    assert fixture["registry"].read_text() == "preserve fixture bytes"


def test_health_ignores_proxy_environment(live_runtime, monkeypatch):
    for key in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(key, "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    assert registry.verified_runtime(live_runtime["workspace"]) == live_runtime["runtime"]
    assert len(live_runtime["requests"]) == 1


@pytest.mark.parametrize("location", ["/must-not-follow", "http://example.invalid/must-not-follow"])
def test_health_never_follows_redirects(live_runtime, location):
    fixture = live_runtime
    fixture.update(status=302, location=location)
    with pytest.raises(registry.RuntimeVerificationError, match="redirect"):
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    assert [request["path"] for request in fixture["requests"]] == ["/health"]
    assert_no_registry(fixture)


@pytest.mark.parametrize("part", ["file", "ancestor"])
def test_runtime_links_are_rejected(live_runtime, tmp_path, part):
    fixture = live_runtime
    if part == "file":
        outside = tmp_path / "outside-runtime.json"
        outside.write_bytes(fixture["path"].read_bytes())
        fixture["path"].unlink()
        link = fixture["path"]
    else:
        outside = tmp_path / "outside-private"
        fixture["path"].parent.rename(outside)
        link = fixture["path"].parent
    try:
        link.symlink_to(outside, target_is_directory=part == "ancestor")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(registry.RuntimeVerificationError, match="links or reparse"):
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    assert_no_registry(fixture)


@pytest.mark.parametrize("part", ["file", "ancestor"])
def test_registry_links_are_rejected_without_changing_target(live_runtime, tmp_path, part):
    fixture = live_runtime
    if part == "file":
        link = registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
        link.unlink()
        outside = tmp_path / "outside-record.json"
        outside.write_text("preserve fixture bytes")
    else:
        link = fixture["registry"]
        outside = tmp_path / "outside-registry"
        outside.mkdir()
    try:
        link.symlink_to(outside, target_is_directory=part == "ancestor")
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(registry.RuntimeVerificationError, match="links or reparse"):
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    if part == "file":
        assert outside.read_text() == "preserve fixture bytes"
    else:
        assert list(outside.iterdir()) == []


@pytest.mark.parametrize("target", ["runtime", "registry"])
@pytest.mark.parametrize("kind", ["symlink", "reparse"])
def test_link_or_reparse_metadata_is_rejected(live_runtime, monkeypatch, target, kind):
    fixture = live_runtime
    path = fixture["path"] if target == "runtime" else registry.register_workspace(
        fixture["workspace"], registry_path=fixture["registry"])
    original = Path.lstat

    def lstat(current, *args, **kwargs):
        info = original(current, *args, **kwargs)
        return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else info.st_mode,
                               st_file_attributes=0x400 if kind == "reparse" else 0) if current == path else info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(registry.RuntimeVerificationError, match="links or reparse"):
        registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])


def test_existing_hardlink_read_and_atomic_replacement_semantics_are_preserved(live_runtime, tmp_path):
    fixture = live_runtime
    runtime_link = tmp_path / "runtime-hardlink.json"
    os.link(fixture["path"], runtime_link)
    assert registry.verified_runtime(fixture["workspace"]) == fixture["runtime"]
    path = registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    previous_link = tmp_path / "registry-hardlink.json"
    os.link(path, previous_link)
    previous = previous_link.read_bytes()
    fixture["runtime"]["nonce"] = "next-fixture-broker-instance"
    save_runtime(fixture, fixture["runtime"])
    registry.register_workspace(fixture["workspace"], registry_path=fixture["registry"])
    assert previous_link.read_bytes() == previous and path.read_bytes() != previous


@pytest.mark.parametrize("request_timeout,expected_timeout", [(2, 2), (15, 3)])
def test_mcp_wrapper_keeps_reader_opener_timeout_and_error_type(live_runtime, monkeypatch, request_timeout, expected_timeout):
    import mcp_server

    fixture = live_runtime
    client = mcp_server.BusClient("antigravity", fixture["workspace"], request_timeout=request_timeout)
    assert client.runtime(fixture["workspace"]) == fixture["runtime"]
    captured = []

    def verify(root, **kwargs):
        captured.append((root, kwargs))
        raise registry.RuntimeVerificationError("Fixture identity refusal")

    monkeypatch.setattr(mcp_server, "_verified_runtime", verify)
    with pytest.raises(mcp_server.BusError, match="Fixture identity refusal"):
        client.runtime(fixture["workspace"])
    assert captured == [(fixture["workspace"], {"read_private": mcp_server._read_private,
                                               "opener": client.opener, "timeout": expected_timeout})]


IMPORT_PROOF = r'''
import json, pathlib, subprocess, sys
product, shared, workspace, target = map(pathlib.Path, sys.argv[1:5])
order, mode = sys.argv[5:7]
sys.path.insert(0, str(product))
if mode == "package":
    sys.path.insert(1, str(shared.parent))
sys.path.append(str(shared))
def forbidden(*args, **kwargs):
    raise AssertionError("Import/metadata registration attempted a process launch")
subprocess.run = subprocess.Popen = forbidden
def product_import():
    import server, mcp_server, adapters
    assert pathlib.Path(server.__file__).resolve() == product / "server.py"
    assert pathlib.Path(mcp_server.__file__).resolve() == product / "mcp_server.py"
    assert pathlib.Path(adapters.__file__).resolve() == product / "adapters/__init__.py"
    return {name: sys.modules.get(name) for name in ("mcp_server", "adapters", "native_bridge")}
def metadata_import():
    before = {name: sys.modules.get(name) for name in ("mcp_server", "adapters", "native_bridge")}
    import sidecar_registry, sharebus
    assert before == {name: sys.modules.get(name) for name in before}
    expected = shared / ("__init__.py" if mode == "package" else "sharebus.py")
    assert pathlib.Path(sharebus.__file__).resolve() == expected, (sharebus.__file__, str(expected))
    return sidecar_registry, sharebus.Client
if order == "mcp-first":
    import mcp_server
    original_mcp = mcp_server
    assert pathlib.Path(mcp_server.__file__).resolve() == product / "mcp_server.py"
    metadata, Client = metadata_import()
    bound = product_import()
    assert sys.modules["mcp_server"] is original_mcp
elif order == "metadata-first":
    metadata, Client = metadata_import()
    assert all(name not in sys.modules for name in ("mcp_server", "adapters", "native_bridge", "antigravity_bridge"))
    bound = product_import()
else:
    bound = product_import()
    metadata, Client = metadata_import()
metadata.REGISTRY = target
pathlib.Path.home = classmethod(lambda cls: forbidden())
runtime = json.loads((workspace / ".sharebus/private/runtime.json").read_text(encoding="utf-8"))
Client(workspace)._enable(runtime)
records = list(target.glob("*.json"))
assert len(records) == 1
assert json.loads(records[0].read_text(encoding="utf-8"))["runtime_nonce"] == runtime["nonce"]
assert (workspace / ".sharebus/enabled").is_file()
assert {name: sys.modules.get(name) for name in bound} == bound
assert "antigravity_bridge" not in sys.modules and "native_bridge" not in sys.modules
print(json.dumps({"ok": True, "order": order, "mode": mode, "registered": True}))
'''


@pytest.mark.parametrize("order", ["metadata-first", "product-first", "mcp-first"])
@pytest.mark.parametrize("mode", ["module", "package"])
def test_actual_enable_and_product_import_orders_do_not_collide(live_runtime, order, mode):
    fixture = live_runtime
    result = subprocess.run([sys.executable, "-B", "-I", "-c", IMPORT_PROOF, str(PRODUCT), str(HERE),
                             str(fixture["workspace"]), str(fixture["registry"]), order, mode],
                            cwd=fixture["workspace"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["registered"]
    assert len(fixture["requests"]) == 1


def test_development_and_public_allowlists_include_shared_module():
    development = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    sharebus_filter = next(item["filter"] for item in development["build"]["extraResources"] if item["to"] == "sharebus")
    assert "sidecar_registry.py" in sharebus_filter
    manifest = json.loads((ROOT / "work/avalokita-release/source-allowlist.json").read_text(encoding="utf-8"))
    assert {"source": "outputs/sharebus/sidecar_registry.py"} in manifest["files"]


@pytest.mark.parametrize("order", ["metadata-first", "product-first"])
def test_public_packaging_filters_support_isolated_import_and_enable(live_runtime, tmp_path, order):
    fixture = live_runtime
    config = (ROOT / "work/avalokita-release/templates/electron-builder.cjs").read_text(encoding="utf-8")
    resources = tmp_path / "resources"
    for source, destination in ((PRODUCT, "avalokita"), (HERE, "sharebus")):
        match = re.search(r"from: 'outputs/" + destination + r"', to: '" + destination + r"', filter: (\[[^\]]*\])", config)
        assert match
        filters = ast.literal_eval(match[1])
        if destination == "sharebus":
            assert "sidecar_registry.py" in filters
        for name in filters:
            if name.endswith(".py"):
                target = resources / destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, target)
    result = subprocess.run([sys.executable, "-B", "-I", "-c", IMPORT_PROOF,
                             str(resources / "avalokita"), str(resources / "sharebus"), str(fixture["workspace"]),
                             str(fixture["registry"]), order, "module"], cwd=fixture["workspace"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["registered"]
    assert list(resources.rglob("__pycache__")) == []
