"""Actual model-free business broker signed diagnostics and legacy refusal."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from urllib.request import build_opener, ProxyHandler

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daemon import BusHTTPServer
from support_auth import ProtocolError, request_headers, signed_exchange, token_key
from support_controller import DiagnosticClient
from support_diagnostics import business_runtime
from support_store import SupportPaths


@pytest.fixture
def business(tmp_path, monkeypatch):
    workspace = tmp_path / "own-synthetic-business-room"
    workspace.mkdir()
    server = BusHTTPServer(workspace, providers="none", enable_native=False, discovery=[])
    server.store.set_paused(True)
    agent, _ = server.store.register("claude", "PRIVATE_DISPLAY_NAME", [], "native_attached",
                                    native_session_id="PRIVATE_NATIVE_ID")
    server.store.update_agent(agent["id"], state="idle")
    (server.store.private / "runtime.json").write_text(json.dumps(server.runtime()), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield SimpleNamespace(server=server, workspace=workspace, root=tmp_path / "own-help-root")
    server.shutdown()
    thread.join(timeout=2)
    server.close()


def snapshot(server):
    with server.store.connection() as db:
        return {name: [tuple(row) for row in db.execute("SELECT * FROM " + name)]
                for name in ("config", "agents", "roots", "events", "jobs")}


def test_business_diagnostic_is_actual_signed_paused_projection_with_zero_queue_writes(business):
    server = business.server
    before = snapshot(server)
    client = DiagnosticClient(SupportPaths(business.workspace, root=business.root))
    seen = []
    real = client.opener
    def observed(request, timeout):
        seen.append({"path": request.full_url, "headers": dict(request.header_items()),
                     "body": request.data.decode() if request.data else ""})
        return real.open(request, timeout=timeout)
    client.opener = SimpleNamespace(open=observed)
    result = client.request("/api/support/diagnose")
    assert result["connection"] == "paused"
    assert result["control"] == {"paused": True, "stopped": None}
    assert result["apps"]["registered"] == 1 and result["apps"]["connected"] == 0
    assert snapshot(server) == before
    wire = json.dumps(seen)
    assert all("Authorization" not in record["headers"] for record in seen)
    assert server.store.operator_token not in wire
    for private in (str(business.workspace), "PRIVATE_DISPLAY_NAME", "PRIVATE_NATIVE_ID", server.store.operator_token):
        assert private not in json.dumps(result)
    assert seen[-1]["path"].endswith("/api/support/diagnose")
    assert "/api/state" not in wire


def test_business_signed_projection_replay_and_other_routes_do_not_grant_operator(business):
    server = business.server
    runtime = business_runtime(server.runtime())
    body = b"{}"
    headers = request_headers(token_key(server.store.operator_token), runtime, "business-diagnostic",
                              "POST", "/api/support/diagnose", body)
    headers.update({"Content-Type": "application/json", "Origin": server.url, "X-Sharebus-Request": "1"})
    def send(path, values):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", path, body, values)
        response = connection.getresponse()
        code, raw = response.status, response.read()
        connection.close()
        return code, raw
    before = snapshot(server)
    assert send("/api/support/diagnose", headers)[0] == 200
    assert send("/api/support/diagnose", headers)[0] == 403
    assert send("/api/control", headers)[0] in {401, 403}
    assert send("/api/support/diagnose", {"Authorization": "Bearer " + server.store.operator_token,
        "Origin": server.url, "Content-Type": "application/json", "X-Sharebus-Request": "1"})[0] == 403
    assert snapshot(server) == before


def test_legacy_or_impostor_business_health_echo_gets_no_raw_secret(business):
    captured = []
    runtime = business.server.runtime()
    class Legacy(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            payload = json.dumps({"ok": True, **runtime}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def do_POST(self):
            captured.append({"path": self.path, "headers": dict(self.headers),
                             "body": self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()})
            raw = b'{"error":"not supported"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    legacy = ThreadingHTTPServer(("127.0.0.1", 0), Legacy)
    thread = threading.Thread(target=legacy.serve_forever, daemon=True)
    thread.start()
    runtime["url"] = "http://127.0.0.1:" + str(legacy.server_port)
    (business.server.store.private / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    try:
        client = DiagnosticClient(SupportPaths(business.workspace, root=business.root))
        with pytest.raises(ProtocolError):
            client.request("/api/support/diagnose")
        assert captured and captured[0]["path"] == "/api/support/diagnose"
        assert business.server.store.operator_token not in json.dumps(captured)
        assert all("Authorization" not in value["headers"] for value in captured)
    finally:
        legacy.shutdown()
        thread.join(timeout=2)
        legacy.server_close()
