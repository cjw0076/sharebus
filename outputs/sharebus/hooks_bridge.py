"""Native lifecycle context and exact pending-result correlation; no human inference."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

from mcp_server import BusClient, BusError, _read_private


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_INPUT_MARKER = re.compile(r"\[SHAREBUS_EVENT:(" + _UUID + r");JOB:(" + _UUID + r")\]")
_RESULT_MARKER = re.compile(r"(?:^|\n)\s*<!-- SHAREBUS_RESULT:(" + _UUID + r");JOB:(" + _UUID + r") -->\s*\Z")


def _owned_pending(state: dict, agent: dict, event_id: str, job_id: str) -> bool:
    if agent.get("mode") != "native_attached" or not agent.get("active_root"):
        return False
    return any(job.get("id") == job_id and job.get("event_id") == event_id
               and job.get("agent_id") == agent["id"] and job.get("state") == "awaiting_native_reply"
               and job.get("root_id") == agent["active_root"]
               for job in state.get("active_jobs", state.get("jobs", [])))


def _save_context(context_file: Path, context: dict) -> None:
    temporary = context_file.parent / (uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(context, stream)
        os.replace(temporary, context_file)
    finally:
        if temporary.exists():
            temporary.unlink()


def forward(provider: str, workspace: Path, payload: dict) -> bool:
    if any(os.environ.get(key) for key in ("SHAREBUS_AGENT_ID", "SHAREBUS_MANAGED", "SHAREBUS_AGENT_TOKEN_FILE")):
        return False
    root = workspace.resolve()
    if provider == "antigravity":
        from antigravity_bridge import register_native_session
        return register_native_session(root, payload, payload.get("hook_event_name", "SessionStart"))
    if not (root / ".sharebus/enabled").is_file():
        return False
    _read_private(root / ".sharebus/enabled", root)
    event = payload.get("hook_event_name")
    if event not in ("UserPromptSubmit", "Stop"):
        return False
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or Path(cwd).resolve() != root:
        return False
    native_id = payload.get("session_id")
    prompt = payload.get("prompt" if event == "UserPromptSubmit" else "last_assistant_message")
    if not isinstance(native_id, str) or not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode("utf-8")) > 65536:
        return False
    if event == "UserPromptSubmit" and prompt.lstrip().startswith("/sharebus"):
        return False
    marker = _RESULT_MARKER.search(prompt) if event == "Stop" else None
    context_dir = root / ".sharebus/private/hook-context"
    if event == "Stop" and marker is None and not context_dir.is_dir():
        return False
    if event == "UserPromptSubmit":
        # Native hooks also run for peer delivery, background work and scheduled
        # invocations. No prefix test can prove that a human typed this prompt.
        if not prompt.startswith(("[Sharebus forwarded request;", "[SHAREBUS_EVENT:")):
            return False
        markers = _INPUT_MARKER.findall(prompt.partition("<incoming_message>")[0])
        if len(markers) != 1:
            return False
    client = BusClient(provider, root, request_timeout=2)
    operator = _read_private(root / ".sharebus/private/operator.token", root)
    state = client.request(root, "/api/state", operator)
    attached = [agent for agent in state.get("agents", []) if agent.get("provider") == provider
                and agent.get("native_session_id") == native_id
                and agent.get("mode") in ("external", "external_attached", "native_attached")]
    if len(attached) != 1:
        return False
    context_file = context_dir / (attached[0]["id"] + ".json")
    if event == "UserPromptSubmit":
        if not _owned_pending(state, attached[0], *markers[0]):
            return False
        context_dir.mkdir(exist_ok=True)
        info = context_dir.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
        event_id, job_id = markers[0]
        _save_context(context_file, {"native_session_id": native_id, "event_id": event_id,
                                    "job_id": job_id, "turn_id": payload.get("turn_id")})
    else:
        if marker:
            prompt = prompt[:marker.start()].rstrip()
            if not prompt or not _owned_pending(state, attached[0], *marker.groups()):
                return False
            event_id, job_id = marker.groups()
        else:
            if not context_file.is_file():
                return False
            try:
                context = _read_private(context_file, root, json_file=True)
            except BusError:
                return False
            turn_id = payload.get("turn_id")
            if (not isinstance(context, dict) or context.get("native_session_id") != native_id
                    or not isinstance(turn_id, str) or not turn_id.strip()
                    or not isinstance(context.get("turn_id"), str) or context["turn_id"] != turn_id):
                return False
            context_event, context_job = context.get("event_id"), context.get("job_id")
            if (not isinstance(context_event, str) or not isinstance(context_job, str)
                    or not _owned_pending(state, attached[0], context_event, context_job)):
                return False
            event_id, job_id = context_event, context_job
        # Lifecycle output is private unless this exact pending work selected it.
        # Never read the agent credential or send a passive, uncorrelated result.
        token_path = root / ".sharebus/private/credentials" / (attached[0]["id"] + ".token")
        agent_token = _read_private(token_path, root)
        result = {"native_session_id": native_id, "last_assistant_message": prompt,
                  "event_id": event_id, "job_id": job_id}
        receipt = client.request(root, "/api/native/result", agent_token, result)
        if receipt.get("completed") and context_file.exists():
            current = _read_private(context_file, root, json_file=True)
            if isinstance(current, dict) and all(current.get(key) == result.get(key) for key in ("native_session_id", "event_id", "job_id")):
                context_file.unlink()
    return True


def _workspace(provider: str, payload: dict, selected: Path | None) -> Path | None:
    paths = payload.get("workspacePaths") if provider == "antigravity" else [payload.get("cwd")]
    if not isinstance(paths, list):
        return None
    roots = {Path(path).resolve() for path in paths if isinstance(path, str) and Path(path).is_absolute()}
    if selected is not None:
        root = selected.resolve()
        return root if root in roots else None
    # A multi-root app must select a room explicitly rather than joining each.
    return next(iter(roots)) if len(roots) == 1 else None


def bootstrap(provider: str, payload: dict, workspace: Path | None = None) -> Path | None:
    from auto_enroll import bootstrap_hook

    root = _workspace(provider, payload, workspace)
    if root is None:
        return None
    return bootstrap_hook(provider, payload, event=payload.get("hook_event_name", "SessionStart"), workspace=root)


def context_output(provider: str, event: str, context_file: Path | None, workspace: Path | None) -> dict:
    if context_file is None or workspace is None or event not in {"SessionStart", "UserPromptSubmit", "PreInvocation"}:
        return {}
    if provider == "antigravity" and event not in {"SessionStart", "PreInvocation"}:
        return {}
    template = (Path(__file__).resolve().parent / "commands/sharebus/native_context.md").read_text(encoding="utf-8")
    command = [sys.executable, *(["-B"] if os.environ.get("AVALOKITA_PACKAGED_RUNTIME") == "1" or sys.dont_write_bytecode else []),
               str(Path(__file__).resolve().with_name("auto_enroll.py")), "connect",
               "--context-file", str(context_file), "--provider", provider, "--workspace", str(workspace)]
    text = (template.replace("@WORKSPACE@", json.dumps(str(workspace), ensure_ascii=True))
            .replace("@CONTEXT_FILE@", json.dumps(str(context_file), ensure_ascii=True))
            .replace("@CONNECT_COMMAND@", json.dumps(command, ensure_ascii=True)))
    if provider == "antigravity":
        return {"injectSteps": [{"ephemeralMessage": text}]}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def main():
    parser = argparse.ArgumentParser(description="Bootstrap opted-in native context and correlate existing Sharebus work")
    parser.add_argument("--provider", choices=("claude", "codex", "antigravity"), required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--event", choices=("SessionStart", "PreInvocation", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd", "StopFailure"))
    parser.add_argument("--bootstrap", action="store_true", help="Discover only the exact native payload workspace")
    args = parser.parse_args()
    payload = {}
    output = {}
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("Hook input exceeds its byte limit.")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Hook input must be an object.")
        if args.event:
            if payload.get("hook_event_name", args.event) != args.event:
                raise ValueError("Native hook event does not match its lifecycle entry.")
            payload["hook_event_name"] = args.event
        event = payload.get("hook_event_name")
        root = _workspace(args.provider, payload, args.workspace)
        if root is not None:
            if event in {"Interrupt", "SessionEnd", "StopFailure"} and args.provider in {"codex", "claude"}:
                from auto_watch import cancel
                cancel(args.provider, payload, root)
            else:
                context = bootstrap(args.provider, payload, root)
                output = context_output(args.provider, event, context, root)
                forward(args.provider, root, payload)
    except Exception:
        # These are optional observation hooks, never native authorization guards.
        # Record only a bounded fixed diagnostic; native host permissions remain.
        paths = [args.workspace] if args.workspace else payload.get("workspacePaths", []) if isinstance(payload, dict) else []
        for path in paths if isinstance(paths, list) else []:
            try:
                root = Path(path).resolve()
                _read_private(root / ".sharebus/enabled", root)
                private = root / ".sharebus/private"
                for target in (root / ".sharebus", private, private / "hook-errors.jsonl"):
                    if target.exists():
                        info = target.lstat()
                        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                            raise ValueError("Unsafe diagnostic path.")
                log = private / "hook-errors.jsonl"
                if log.exists() and log.stat().st_size >= 1048576:
                    continue
                event = args.event or (payload.get("hook_event_name") if isinstance(payload, dict) else None)
                event = event if event in {"SessionStart", "PreInvocation", "UserPromptSubmit", "Stop", "Interrupt", "SessionEnd", "StopFailure"} else "unknown"
                descriptor = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps({"time": time.time(), "provider": args.provider, "event": event,
                                             "error": "optional_observation_failed"}) + "\n")
            except Exception:
                pass
        print("Sharebus optional observation failed; native permissions still apply.", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
