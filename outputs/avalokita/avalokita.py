"""Avalokita CLI. Local capabilities are read from private files, never printed."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

from bridge import BridgeError, BusStore, NoRedirect, private_read


def request(url, token, endpoint, payload=None):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.path not in {"", "/"} or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise BridgeError("Exact local Avalokita URL required", 403)
    headers = {"X-Avalokita-Request": "1", "Authorization": "Bearer " + token}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url.rstrip("/") + endpoint, data=data, headers=headers), timeout=12) as response:
        return json.load(response)


def capability(workspace: Path, ready_file: str | None = None):
    workspace = BusStore.checked_workspace(workspace)
    ready = json.loads(private_read(Path(ready_file) if ready_file else workspace / ".avalokita/private/runtime.json"))
    if ready.get("workspace") != str(workspace) or type(ready.get("pid")) is not int:
        raise BridgeError("Runtime does not belong to this workspace", 403)
    health = request(ready["url"], ready["token"], "/health")
    if not health.get("ok") or any(health.get(key) != ready.get(key) for key in ("pid", "instance_id", "workspace")):
        raise BridgeError("Runtime identity changed", 403)
    return ready


def peer_operation(workspace, *, actor, native_session_id, root_id, operation, arguments, ready_file=None, url=None):
    root = BusStore.checked_workspace(workspace)
    if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", actor):
        raise BridgeError("Exact registered actor ID required")
    if not native_session_id:
        raise BridgeError("Native session proof required", 403)
    # Own exact identity is checked through the existing authenticated bus before
    # handing its capability to Avalokita. No operator token is a peer identity.
    from bridge import SharebusBridge
    bridge = SharebusBridge(root)
    token = private_read(root / ".sharebus/private/credentials" / (actor + ".token"))
    own = bridge.authenticate_peer(token, allow_paused=operation in {"capture-pane", "read-context"})
    if own.get("id") != actor or own.get("native_session_id") != native_session_id:
        raise BridgeError("Capability is not bound to this exact original native actor", 403)
    target = url or capability(root, ready_file)["url"]
    if not isinstance(arguments, dict) or set(arguments) & {"actor", "root_id", "operation"}:
        raise BridgeError("Identity and operation cannot be overridden")
    return request(target, token, "/api/peer", {**arguments, "actor": actor, "root_id": root_id, "operation": operation})


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--ready-file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("state")
    prompt = sub.add_parser("prompt")
    prompt.add_argument("text")
    prompt.add_argument("--idempotency-key", required=True)
    prompt.add_argument("--reply-to", help="Exact pending public question ID")
    prompt.add_argument("--budget", type=int, default=24, help="Maximum native automatic turns for this human input")
    prompt.add_argument("--ttl", type=float, default=3600, help="Human authority lifetime in seconds")
    control = sub.add_parser("control")
    control.add_argument("action", choices=["pause", "resume"])
    peer = sub.add_parser("peer", help="Existing native job only; retains exact actor and human root")
    peer.add_argument("--actor", required=True)
    peer.add_argument("--native-session-id", required=True)
    peer.add_argument("--root-id", required=True, help="Avalokita Harmony goal ID from the verified human job")
    peer.add_argument("--url", help="Exact loopback callback from verified job (no tokens in URL)")
    peer.add_argument("--operation", required=True, choices=["publish", "send-keys", "capture-pane", "claim", "renew", "release", "handoff", "context", "read-context"])
    peer.add_argument("--arguments", default="{}", help="JSON operation arguments; never include tokens")
    args = parser.parse_args()
    try:
        if args.command == "peer":
            result = peer_operation(args.workspace, actor=args.actor, native_session_id=args.native_session_id,
                                    root_id=args.root_id, operation=args.operation, arguments=json.loads(args.arguments),
                                    ready_file=args.ready_file, url=args.url)
        else:
            runtime = capability(args.workspace, args.ready_file)
            endpoint, payload = "/api/state", None
            if args.command == "prompt":
                endpoint, payload = "/api/prompt", {"text": args.text, "idempotency_key": args.idempotency_key, "reply_to": args.reply_to,
                    "budget": args.budget, "ttl": args.ttl}
            elif args.command == "control":
                endpoint, payload = "/api/control", {"action": args.action}
            result = request(runtime["url"], runtime["token"], endpoint, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        # Avoid printing a URL/request object which could contain capability data.
        parser.exit(1, f"Avalokita operation failed ({type(exc).__name__}); inspect the local connection or current root.\n")


if __name__ == "__main__":
    main()
