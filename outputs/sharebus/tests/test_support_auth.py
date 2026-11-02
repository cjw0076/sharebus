"""Adversarial local protocol tests: no reusable native/operator secret on wire."""
from email.message import Message
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from urllib.request import build_opener, ProxyHandler

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support_auth import (ProtocolError, request_headers, request_signature, signed_exchange,
                          token_key, verify_request)
from support_controller import SupportClient, DiagnosticClient
from support_store import SupportError, read_private, write_private
from test_support_controller import service, request


def raw_call(service, path, body, headers):
    connection = http.client.HTTPConnection("127.0.0.1", service.server.server_port, timeout=10)
    values = {"Content-Type": "application/json", "Origin": service.server.url, "X-Sharebus-Request": "1", **headers}
    connection.request("POST", path, body, values)
    response = connection.getresponse()
    status, raw = response.status, response.read()
    connection.close()
    return status, raw


@pytest.mark.parametrize("change", ["path", "body", "actor", "expiry", "duplicate", "runtime"])
def test_signed_mutation_cannot_change_path_body_actor_expiry_or_runtime(service, change):
    body = b'{"provider":"claude"}'
    path = "/api/support/open"
    headers = request_headers(token_key(service.controller.operator), service.server.runtime(), "operator", "POST", path, body)
    if change == "path":
        path = "/api/support/ticket"
    elif change == "body":
        body = b'{"provider":"codex"}'
    elif change == "actor":
        headers["X-Sharebus-Actor"] = "diagnostic:" + "1" * 32
    elif change == "expiry":
        headers["X-Sharebus-Expires"] = str(int(time.time()) - 5)
    elif change == "runtime":
        headers = request_headers(token_key(service.controller.operator),
            {**service.server.runtime(), "nonce": "previous-service-instance"}, "operator", "POST", path, body)
    else:
        message = Message()
        for key, value in headers.items():
            message[key] = value
        message["X-Sharebus-Nonce"] = headers["X-Sharebus-Nonce"]
        with pytest.raises(ProtocolError):
            verify_request(token_key(service.controller.operator), service.server.runtime(), "operator", "POST", path, body, message)
        return
    assert raw_call(service, path, body, headers)[0] in {403, 409}
    assert service.controller.store.public("offline")["messages"] == []


def test_signed_request_nonce_cannot_replay_and_raw_bearer_is_rejected(service):
    body = b'{"provider":"claude"}'
    headers = request_headers(token_key(service.controller.operator), service.server.runtime(), "operator",
                              "POST", "/api/support/open", body)
    assert raw_call(service, "/api/support/open", body, headers)[0] == 200
    assert raw_call(service, "/api/support/open", body, headers)[0] == 403
    assert raw_call(service, "/api/support/open", body, {"Authorization": "Bearer " + service.controller.operator})[0] == 403


def test_forged_public_health_listener_cannot_capture_operator_or_open_browser(service, monkeypatch):
    captured = []
    runtime = service.server.runtime()
    class Impostor(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            captured.append({"headers": dict(self.headers), "path": self.path})
            payload = json.dumps({"ok": True, **runtime}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    impostor = ThreadingHTTPServer(("127.0.0.1", 0), Impostor)
    thread = threading.Thread(target=impostor.serve_forever, daemon=True)
    thread.start()
    runtime["url"] = "http://127.0.0.1:" + str(impostor.server_port)
    write_private(service.paths.file("runtime.json"), json.dumps(runtime))
    opened = []
    monkeypatch.setattr("support_controller.webbrowser.open", opened.append)
    client = SupportClient(service.workspace, root=service.paths.root)
    try:
        for action in (client.state, client.open):
            with pytest.raises(SupportError):
                action()
        assert captured and not opened
        wire = json.dumps(captured)
        assert service.controller.operator not in wire
        assert all("Authorization" not in value["headers"] for value in captured)
    finally:
        impostor.shutdown()
        thread.join(timeout=2)
        impostor.server_close()


def test_port_flip_after_valid_health_cannot_receive_master_or_forge_ticket(service, monkeypatch):
    real = build_opener(ProxyHandler({}))
    captured, opened = [], []
    class ForgedResponse:
        code = 200
        headers = Message()
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def read(self, _maximum):
            return b'{"ticket":"attacker-fabricated-ticket-with-enough-length","expires_in":60}'
    def opened_request(request, timeout):
        if request.full_url.endswith("/health"):
            return real.open(request, timeout=timeout)
        captured.append({"headers": dict(request.header_items()), "body": request.data.decode()})
        return ForgedResponse()
    client = SupportClient(service.workspace, root=service.paths.root)
    client.opener = SimpleNamespace(open=opened_request)
    monkeypatch.setattr("support_controller.webbrowser.open", opened.append)
    with pytest.raises(SupportError):
        client.open()
    assert captured and not opened
    assert service.controller.operator not in json.dumps(captured)
    assert all("Authorization" not in item["headers"] for item in captured)


def test_ticket_is_short_lived_one_use_not_an_operator_capability(service, monkeypatch):
    code, receipt, _ = request(service, "/api/support/ticket", {})
    assert code == 200 and 0 < receipt["expires_in"] <= 60
    ticket = receipt["ticket"]
    assert ticket != service.controller.operator
    assert request(service, "/api/support/state", token=ticket)[0] == 403
    with service.server._auth_lock:
        service.server._tickets[token_key(ticket)] = time.time() - 1
    assert request(service, "/api/auth", {"ticket": ticket}, token=False)[0] == 403
    assert request(service, "/api/auth", {"ticket": ticket}, token=False)[0] == 403


def test_master_never_in_supported_request_or_browser_cookie(service):
    seen = []
    real = build_opener(ProxyHandler({}))
    def inspect_request(request, timeout):
        seen.append({"url": request.full_url, "headers": dict(request.header_items()),
                     "body": request.data.decode() if request.data else ""})
        return real.open(request, timeout=timeout)
    client = SupportClient(service.workspace, root=service.paths.root)
    client.opener = SimpleNamespace(open=inspect_request)
    assert client.state()["guide"]["state"] == "ready"
    ticket = client._request("/api/support/ticket", {})["ticket"]
    code, value, response = request(service, "/api/auth", {"ticket": ticket}, token=False)
    assert code == 200
    assert service.controller.operator not in json.dumps(seen)
    assert service.controller.operator not in response["Set-Cookie"]
    assert request(service, "/api/support/state", token=False,
                   headers={"Cookie": response["Set-Cookie"].split(";")[0]})[0] == 403
