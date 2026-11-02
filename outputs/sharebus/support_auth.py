"""Runtime-bound local support request/response MACs; secrets never go on wire."""
from __future__ import annotations

import hashlib
import errno
import hmac
import json
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request


SERVICE = "sharebus-support-v1"
TTL = 30
RUNTIME_FIELDS = ("service", "key", "nonce", "pid", "url")
SIGNED_HEADERS = ("X-Sharebus-Actor", "X-Sharebus-Nonce", "X-Sharebus-Expires", "X-Sharebus-Signature")


class ProtocolError(RuntimeError):
    def __init__(self):
        super().__init__("The help service identity could not be verified.")


class ConnectionUnavailable(ProtocolError):
    """A refused TCP connection, not an observation timeout or a forged reply."""


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mac(key: str, value: dict) -> str:
    try:
        raw = json.dumps({"domain": "sharebus-support-auth-v1", **value}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return hmac.new(bytes.fromhex(key), raw, hashlib.sha256).hexdigest()
    except (ValueError, TypeError, KeyError):
        raise ProtocolError() from None


def runtime_tuple(runtime: dict) -> dict:
    try:
        return {name: runtime[name] for name in RUNTIME_FIELDS}
    except (KeyError, TypeError):
        raise ProtocolError() from None


def request_signature(key: str, runtime: dict, actor: str, method: str, path: str,
                      body: bytes, nonce: str, expires: int) -> str:
    return _mac(key, {"kind": "request", "runtime": runtime_tuple(runtime), "actor": actor,
                      "method": method, "path": path, "body_sha256": hashlib.sha256(body).hexdigest(),
                      "nonce": nonce, "expires": expires})


def request_headers(key: str, runtime: dict, actor: str, method: str, path: str,
                    body: bytes, *, now: float | None = None) -> dict:
    nonce = secrets.token_urlsafe(32)
    expires = int(time.time() if now is None else now) + TTL
    return {"X-Sharebus-Actor": actor, "X-Sharebus-Nonce": nonce,
            "X-Sharebus-Expires": str(expires), "X-Sharebus-Signature": request_signature(
                key, runtime, actor, method, path, body, nonce, expires)}


def verify_request(key: str, runtime: dict, actor: str, method: str, path: str, body: bytes,
                   headers, *, now: float | None = None) -> tuple[str, int]:
    try:
        values = {}
        for name in SIGNED_HEADERS:
            candidates = headers.get_all(name, [])
            if len(candidates) != 1:
                raise ProtocolError()
            values[name] = candidates[0]
        nonce, expiry = values["X-Sharebus-Nonce"], values["X-Sharebus-Expires"]
        if (values["X-Sharebus-Actor"] != actor or not re.fullmatch(r"[A-Za-z0-9_-]{32,96}", nonce)
                or not re.fullmatch(r"[0-9]{1,12}", expiry)):
            raise ProtocolError()
        expires = int(expiry)
        current = time.time() if now is None else now
        if not current <= expires <= current + TTL + 1:
            raise ProtocolError()
        signature = values["X-Sharebus-Signature"]
        if (not re.fullmatch(r"[0-9a-f]{64}", signature) or not hmac.compare_digest(signature,
                request_signature(key, runtime, actor, method, path, body, nonce, expires))):
            raise ProtocolError()
        return nonce, expires
    except (ValueError, TypeError, AttributeError):
        raise ProtocolError() from None


def response_signature(key: str, runtime: dict, actor: str, nonce: str, status: int, body: bytes) -> str:
    return _mac(key, {"kind": "response", "runtime": runtime_tuple(runtime), "actor": actor,
                      "request_nonce": nonce, "status": status,
                      "body_sha256": hashlib.sha256(body).hexdigest()})


def signed_exchange(opener, runtime: dict, key: str, actor: str, path: str,
                    payload: dict | None = None, *, timeout: float = 5, maximum: int = 4 * 1024 * 1024):
    body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    method = "GET" if payload is None else "POST"
    headers = request_headers(key, runtime, actor, method, path, body)
    if payload is not None:
        headers.update({"Origin": runtime["url"], "X-Sharebus-Request": "1", "Content-Type": "application/json"})
    request = Request(runtime["url"] + path, data=None if payload is None else body, headers=headers)
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read(maximum + 1)
            status = response.code
            signatures = response.headers.get_all("X-Sharebus-Response-Signature", [])
            nonces = response.headers.get_all("X-Sharebus-Request-Nonce", [])
        if (len(raw) > maximum or len(signatures) != 1 or nonces != [headers["X-Sharebus-Nonce"]]
                or not isinstance(signatures[0], str) or not re.fullmatch(r"[0-9a-f]{64}", signatures[0])
                or not hmac.compare_digest(signatures[0], response_signature(
                    key, runtime, actor, headers["X-Sharebus-Nonce"], status, raw))):
            raise ProtocolError()
        return status, raw
    except OSError as error:
        cause = error.reason if isinstance(error, URLError) else error
        if (isinstance(cause, ConnectionRefusedError) or isinstance(cause, OSError)
                and (cause.errno == errno.ECONNREFUSED or getattr(cause, "winerror", None) == 10061)):
            raise ConnectionUnavailable() from None
        raise ProtocolError() from None
    except (ValueError, AttributeError, TypeError):
        raise ProtocolError() from None
