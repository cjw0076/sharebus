"""Private native-hook enrollment and the cached-schema, agent-only CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from store import BusError, BusStore

from sharebus import Client

_EVENTS = {"claude": {"SessionStart", "UserPromptSubmit", "Stop"},
           "codex": {"SessionStart", "UserPromptSubmit", "Stop"},
           "antigravity": {"SessionStart", "PreInvocation", "ConversationStart", "BeforeAgent", "AfterAgent", "UserPromptSubmit", "Stop"}}
_STOP_STATES = {"paused", "stopped", "needs_human"}


def _managed() -> bool:
    return any(os.environ.get(key) for key in ("SHAREBUS_MANAGED", "SHAREBUS_AGENT_ID", "SHAREBUS_AGENT_TOKEN_FILE"))


def _root(value: str | Path) -> Path:
    root = Path(value)
    if not root.is_absolute() or ".." in root.parts:
        raise BusError("Automatic participation requires an exact absolute workspace", 403)
    root = BusStore.checked_workspace(root)
    if not root.is_dir():
        raise BusError("Automatic workspace must be a directory", 403)
    return root


def _read(root: Path, path: Path, *, json_file: bool = True):
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
        raise BusError("Automatic private path belongs outside this workspace", 403)
    BusStore.reject_linked_path(path)
    try:
        info = path.stat()
        if not path.is_file() or info.st_nlink != 1 or info.st_size > 65536:
            raise BusError("Automatic private file is invalid", 403)
        content = path.read_text(encoding="utf-8")
        return json.loads(content) if json_file else content.strip()
    except (OSError, ValueError):
        raise BusError("Automatic private file is unavailable or invalid", 409) from None


def _auto(root: Path) -> dict[str, Any] | None:
    path = root / ".sharebus/auto.json"
    BusStore.reject_linked_path(path)
    if not path.exists():
        return None
    value = _read(root, path)
    if (not isinstance(value, dict) or value.get("version") != 1
            or value.get("workspace") != str(root) or not isinstance(value.get("workspace_id"), str)):
        raise BusError("Automatic settings belong to another workspace", 403)
    return value


def _identity(provider: str, payload: dict[str, Any], workspace: str | Path | None):
    if _managed() or provider not in _EVENTS or not isinstance(payload, dict):
        return None
    native = payload.get("conversationId") if provider == "antigravity" else payload.get("session_id")
    if not isinstance(native, str) or not 1 <= len(native) <= 256 or any(ord(ch) < 32 for ch in native):
        return None
    selected = _root(workspace) if workspace is not None else None
    roots = payload.get("workspacePaths") if provider == "antigravity" else [payload.get("cwd")]
    if not isinstance(roots, list) or not roots:
        return None
    candidates = []
    for value in roots:
        if not isinstance(value, str):
            return None
        root = _root(value)
        if root not in candidates:
            candidates.append(root)
    if selected is not None:
        if selected not in candidates:
            return None
        candidates = [selected]
    enabled = [(root, settings) for root in candidates if (settings := _auto(root)) and settings.get("enabled") is True]
    if len(enabled) != 1:
        return None
    root, settings = enabled[0]
    if settings.get("suspended") or settings.get("paused"):
        return None
    return root, native


def bootstrap_hook(provider: str, payload: dict[str, Any], event: str = "SessionStart",
                   workspace: str | Path | None = None) -> Path | None:
    """Register only exact native metadata; a prompt is never human authority."""
    if provider not in _EVENTS or event not in _EVENTS[provider] or not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name", event) != event or any(payload.get(key) is True for key in ("cancelled", "interrupted", "aborted")):
        return None
    selected = _identity(provider, payload, workspace)
    if selected is None:
        return None
    root, native = selected
    # The enabled mirror is inspected before any BusStore constructor can make
    # directories, initialize SQLite, or write an operator capability.
    try:
        return BusStore(root, timeout=0.2).prepare_auto_context(provider, native, provider + ":" + event)
    except sqlite3.Error:
        # An optional hook must not hold the native app behind a busy database.
        return None


make_hook_context = bootstrap_hook


def bootstrap_native_environment(workspace: str | Path) -> Path | None:
    """Explicit operator activation for this already-open Codex native process.

    The caller supplies no native ID. This is separate from lifecycle hooks:
    it records the real environment source and exact process cwd honestly.
    """
    if _managed() or any(os.environ.get(key) for key in (
            "CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
            "ANTIGRAVITY_CONVERSATION_ID", "GEMINI_CLI")):
        return None
    native = os.environ.get("CODEX_THREAD_ID")
    try:
        if not isinstance(native, str) or str(uuid.UUID(native)) != native:
            return None
    except ValueError:
        return None
    root = _root(workspace)
    if _root(Path.cwd()) != root:
        return None
    settings = _auto(root)
    if not settings or settings.get("enabled") is not True or settings.get("suspended") or settings.get("paused"):
        return None
    try:
        return BusStore(root, timeout=0.2).prepare_auto_context("codex", native, "codex:native_environment")
    except sqlite3.Error:
        return None


def _context(path: str | Path, provider: str, root: Path) -> tuple[Path, dict[str, Any]]:
    path = Path(path)
    if path.parent != root / ".sharebus/private/auto/contexts":
        raise BusError("Automatic context must use this workspace's private context directory", 403)
    value = _read(root, path)
    keys = ("workspace_id", "provider", "native_session_id", "hook_source", "context_id", "agent_id", "code", "claim_id")
    if (not isinstance(value, dict) or value.get("version") != 1 or value.get("purpose") != "auto_bootstrap"
            or value.get("workspace") != str(root) or value.get("provider") != provider
            or any(not isinstance(value.get(key), str) or not value[key] or len(value[key]) > 512 for key in keys)):
        raise BusError("Automatic context identity is invalid", 403)
    expected = hashlib.sha256((value["workspace_id"] + "\0" + provider + "\0" + value["native_session_id"]).encode()).hexdigest()
    sources = {provider + ":" + event for event in _EVENTS.get(provider, set())}
    if provider == "codex":
        sources.add("codex:mcp_transport")
        sources.add("codex:native_environment")
    if (value["context_id"] != expected or path.name != expected + ".json"
            or value["hook_source"] not in sources or len(value["native_session_id"]) > 256
            or any(ord(ch) < 32 for ch in value["native_session_id"])):
        raise BusError("Automatic context does not match its native identity", 403)
    try:
        if str(uuid.UUID(value["agent_id"])) != value["agent_id"]:
            raise ValueError
    except ValueError:
        raise BusError("Automatic context agent identity is invalid", 403) from None
    return path, value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BusError("Automatic connection refused an HTTP redirect", 403)


def _request(root: Path, path: str, token: str = "", payload: dict | None = None,
             *, deadline: float) -> dict[str, Any]:
    """Pin process identity before sending proof/token; share one total deadline."""
    runtime = _read(root, root / ".sharebus/private/runtime.json")
    if not isinstance(runtime, dict):
        raise BusError("Automatic daemon runtime is invalid", 409)
    try:
        parsed = urlsplit(runtime.get("url", ""))
        valid = (parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port
                 and not parsed.username and not parsed.password and parsed.path in ("", "/")
                 and not parsed.query and not parsed.fragment and runtime.get("workspace") == str(root)
                 and runtime.get("workspace_id") == (_auto(root) or {}).get("workspace_id")
                 and type(runtime.get("pid")) is int and runtime["pid"] > 0
                 and isinstance(runtime.get("nonce"), str) and len(runtime["nonce"]) >= 16)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise BusError("Automatic daemon must be this workspace's loopback process", 403)
    opener = build_opener(ProxyHandler({}), _NoRedirect())

    def fetch(request: Request) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BusError("Automatic connection exceeded its time limit", 409)
        try:
            with opener.open(request, timeout=remaining) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise TypeError
            return value
        except HTTPError as exc:
            raise BusError("Automatic request was rejected (HTTP " + str(exc.code) + ")", exc.code) from None
        except (OSError, URLError, ValueError, TypeError):
            raise BusError("Automatic daemon is unavailable or returned invalid data", 409) from None

    url = runtime["url"].rstrip("/")
    health = fetch(Request(url + "/health"))
    if health.get("ok") is not True or any(health.get(key) != runtime.get(key) for key in ("workspace_id", "pid", "nonce")):
        raise BusError("Automatic runtime identity changed; no credential was sent", 403)
    headers = {"X-Sharebus-Request": "1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    return fetch(Request(url + path, data=data, headers=headers))


def _local_controls(root: Path, context: dict[str, Any]) -> dict[str, Any]:
    database = root / ".sharebus/private/bus.sqlite3"
    BusStore.reject_linked_path(database)
    if not database.is_file() or database.stat().st_nlink != 1:
        raise BusError("Automatic workspace database is unavailable", 409)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.5)) as db:
        db.row_factory = sqlite3.Row
        settings = dict(db.execute("SELECT key,value FROM config WHERE key IN ('workspace_id','auto_enabled','auto_suspended','paused')"))
        row = db.execute("SELECT * FROM agents WHERE id=?", (context["agent_id"],)).fetchone()
        if (settings.get("workspace_id") != context["workspace_id"] or row is None
                or row["provider"] != context["provider"] or row["native_session_id"] != context["native_session_id"]
                or row["mode"] not in {"external", "external_attached", "native_attached"}):
            raise BusError("Automatic context does not own this native agent", 403)
        proof = db.execute("SELECT * FROM auto_proofs WHERE code_hash=?", (hashlib.sha256(context["code"].encode()).hexdigest(),)).fetchone()
        keys = ("workspace_id", "provider", "native_session_id", "hook_source", "context_id", "agent_id")
        proof_valid = (proof is not None and all(proof[key] == context[key] for key in keys)
                       and proof["claim_hash"] == hashlib.sha256(context["claim_id"].encode()).hexdigest()
                       and proof["transport_binding"] == context.get("transport_binding")
                       and math.isfinite(proof["expires_at"]) and proof["expires_at"] > time.time())
        return {"active": settings.get("auto_enabled") == "true" and settings.get("auto_suspended") != "true"
                and settings.get("paused") != "true", "state": row["state"], "token_hash": row["token_hash"],
                "proof_valid": proof_valid}


def connect_context(context_file: str | Path, provider: str, workspace: str | Path, *,
                    request_timeout: float = 3.0, start_daemon: bool = False) -> dict[str, Any]:
    if _managed() or provider not in _EVENTS or type(request_timeout) not in (int, float) or not math.isfinite(request_timeout) or not 0 < request_timeout <= 60:
        raise BusError("Automatic connection options are invalid", 403)
    deadline = time.monotonic() + request_timeout
    root = _root(workspace)
    path, context = _context(context_file, provider, root)
    settings = _auto(root)
    if not settings or settings.get("workspace_id") != context["workspace_id"]:
        raise BusError("Automatic context settings do not match this workspace", 403)
    controls = _local_controls(root, context)
    binding_file = root / ".sharebus/private/auto/bindings" / (context["context_id"] + ".json")
    BusStore.reject_linked_path(binding_file)
    binding = _read(root, binding_file) if binding_file.exists() else None
    token_file = root / ".sharebus/private/credentials" / (context["agent_id"] + ".token")
    expected = {"workspace": str(root), "workspace_id": context["workspace_id"], "provider": provider,
                "native_session_id": context["native_session_id"], "agent_id": context["agent_id"],
                "token_file": str(token_file), "context_file": str(path)}
    if binding is not None and (not isinstance(binding, dict) or any(binding.get(key) != value for key, value in expected.items())):
        raise BusError("Automatic binding receipt does not match this native context", 403)
    if binding is None:
        if not controls["active"] or controls["state"] in _STOP_STATES:
            raise BusError("Automatic participation is disabled, suspended, or controlled by the operator", 409)
        if (not controls["proof_valid"] or type(context.get("expires_at")) not in (int, float)
                or not math.isfinite(context["expires_at"]) or context["expires_at"] <= time.time()):
            raise BusError("Automatic bootstrap expired; wait for the next native hook", 403)
    token = _read(root, token_file, json_file=False)
    if (not 32 <= len(token) <= 4096 or any(ord(char) < 32 for char in token)
            or hashlib.sha256(token.encode()).hexdigest() != controls["token_hash"]):
        raise BusError("Automatic credential integrity check failed", 403)
    if start_daemon:
        if not controls["active"] or controls["state"] in _STOP_STATES:
            raise BusError("Operator controls prevent an automatic daemon start", 409)
        # Explicit callers can opt in. Native lifecycle hooks and watchers never
        # take this 120-second service-management path.
        Client(root).start("none")
    if binding is None:
        result = _request(root, "/api/auto/redeem", payload=context, deadline=deadline)
        own = result.get("agent") or {}
        redeemed_token = result.get("token")
        if (any(own.get(key) != context[field] for key, field in (("id", "agent_id"), ("provider", "provider"), ("native_session_id", "native_session_id")))
                or not isinstance(redeemed_token, str) or redeemed_token != token):
            raise BusError("Automatic redemption returned a different native owner", 403)
        # The daemon's stable credential already exists. Never rotate or rewrite
        # it, including after an uncertain/lost HTTP redemption response.
        if _read(root, token_file, json_file=False) != token:
            raise BusError("Automatic credential integrity check failed", 403)
        binding = {**expected, "hook_source": context["hook_source"]}
        BusStore(root, timeout=min(0.2, request_timeout)).write_auto_private(binding_file, binding)
    state = _request(root, "/api/state", token, deadline=deadline)
    own = state.get("own_agent") or {}
    if any(own.get(key) != expected[field] for key, field in (("id", "agent_id"), ("provider", "provider"), ("native_session_id", "native_session_id"))):
        raise BusError("Authenticated automatic identity does not match this native context", 403)
    auto = state.get("auto") or {}
    if (provider == "codex" and own.get("mode") in {"external", "external_attached"} and own.get("state") == "idle"
            and auto.get("active") is True and auto.get("codex_delivery") == "native_queue"):
        promoted = _request(root, "/api/agent/heartbeat", token, {}, deadline=deadline).get("agent") or {}
        if any(promoted.get(key) != expected[field] for key, field in (("id", "agent_id"), ("provider", "provider"), ("native_session_id", "native_session_id"))):
            raise BusError("Native queue attachment changed this conversation identity", 403)
    return binding


def trusted_transport_binding(thread_id: str | None, session_id: str | None,
                              workspace: str | Path) -> Path | None:
    if (_managed() or not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 256
            or any(ord(ch) < 32 for ch in thread_id)):
        return None
    root = _root(workspace)
    settings = _auto(root)
    if not settings or settings.get("enabled") is not True or settings.get("suspended") or settings.get("paused"):
        return None
    digest = hashlib.sha256((settings["workspace_id"] + "\0codex\0" + thread_id).encode()).hexdigest()
    path = root / ".sharebus/private/auto/contexts" / (digest + ".json")
    if not path.exists():
        return None
    _, context = _context(path, "codex", root)
    source = context.get("native_hook_source", context["hook_source"])
    if source not in {"codex:" + event for event in _EVENTS["codex"]} | {"codex:native_environment"}:
        return None
    controls = _local_controls(root, context)
    if not controls["active"] or controls["state"] in _STOP_STATES:
        return None
    # The transport session is kept separate and may be absent. It is never
    # substituted for the exact host-owned native thread ID above.
    return BusStore(root, timeout=0.2).prepare_auto_context("codex", thread_id, "codex:mcp_transport", session_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Use the current native session's private Sharebus context")
    parser.add_argument("command", choices=("connect", "status", "read", "send", "publish", "ack"))
    parser.add_argument("--context-file", required=True, type=Path)
    parser.add_argument("--provider", choices=tuple(_EVENTS))
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--after", type=int, default=0)
    parser.add_argument("--to", default="all")
    parser.add_argument("--body")
    parser.add_argument("--kind", default="message")
    parser.add_argument("--in-reply-to")
    parser.add_argument("--source")
    parser.add_argument("--text")
    parser.add_argument("--filename")
    parser.add_argument("--job-id")
    parser.add_argument("--event-id")
    parser.add_argument("--native-session-id")
    parser.add_argument("--request-timeout", type=float, default=3.0)
    args = parser.parse_args(argv)
    try:
        correlation = (args.job_id, args.event_id, args.native_session_id)
        completing = args.command == "send" and any(value is not None for value in correlation)
        if completing and (not all(correlation) or args.to != "user" or args.kind != "result"):
            raise BusError("Native completion needs to=user, kind=result and all three correlation IDs")
        if args.command != "send" and (args.event_id is not None or args.native_session_id is not None):
            raise BusError("Native completion IDs apply only to send")
        root = _root(args.workspace or args.context_file.parents[4])
        record = _read(root, args.context_file)
        if completing and record.get("native_session_id") != args.native_session_id:
            raise BusError("Native completion belongs to a different attached conversation", 403)
        provider = args.provider or record.get("provider")
        binding = connect_context(args.context_file, provider, root, request_timeout=args.request_timeout)
        token = _read(root, Path(binding["token_file"]), json_file=False)
        deadline = time.monotonic() + args.request_timeout
        if args.command == "connect":
            result = {"connected": True, **{key: binding[key] for key in ("agent_id", "provider", "native_session_id", "workspace")}}
        elif args.command == "status":
            result = _request(root, "/api/state", token, deadline=deadline)
        elif args.command == "read":
            result = _request(root, "/api/agent/inbox", token, {"after": args.after}, deadline=deadline)
        elif args.command == "send":
            if completing:
                result = _request(root, "/api/native/result", token, {"job_id": args.job_id, "event_id": args.event_id,
                                  "native_session_id": args.native_session_id, "last_assistant_message": args.body}, deadline=deadline)
            else:
                result = _request(root, "/api/message", token, {"to": args.to, "body": args.body, "kind": args.kind,
                                  "in_reply_to": args.in_reply_to}, deadline=deadline)
        elif args.command == "publish":
            result = _request(root, "/api/artifact/publish", token, {"source": args.source, "text": args.text,
                              "filename": args.filename}, deadline=deadline)
        else:
            if not args.job_id:
                raise BusError("Acknowledgement requires an owned job ID")
            result = _request(root, "/api/agent/inbox", token, {"ack": [args.job_id], "claim": False}, deadline=deadline)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (BusError, OSError, ValueError, TypeError, KeyError, IndexError, sqlite3.Error):
        print("Sharebus automatic operation failed; check this native context and workspace controls.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
