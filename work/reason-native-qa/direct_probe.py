"""Operator-only exact existing Codex direct-input QA; never a peer relay.

prepare --spec PRIVATE_JSON writes only private QA metadata. The spec has the
same workspace/engine_path/engine_sha256/python_path/delivery_timeout fields as
runner.py and exactly one participants entry: provider codex, its existing
NativeBinding, role retained, and allowed_targets []. A prior unfinished goal
using that binding is rejected; this tool never changes an earlier goal.

Only seed --manifest PRIVATE_MANIFEST requests one native input. inspect and
verify are read-only. No Claude input, approval, settings, daemon, history scan,
project edits, automatic retry, or fallback is requested by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "outputs" / "avalokita"))
from adapters.reason_engine import NativeBinding
from reason_sessions import (
    Participant,
    ReasonSessions,
    SessionError,
    _json,
    _read,
)

CONTRACT = "avalokita.reason-direct-codex-qa/1"


def _manifest(path: Path) -> tuple[dict[str, Any], ReasonSessions, str]:
    value = json.loads(_read(path))
    if value.get("contract") != CONTRACT:
        raise SessionError("A private direct Codex QA manifest is required")
    store = ReasonSessions(value["workspace"])
    if path.parent != store.private:
        raise SessionError("Direct QA manifests stay in the workspace private namespace")
    return value, store, _read(store.operator_path).strip()


def prepare(path: Path) -> dict[str, Any]:
    """Create one independent human goal; no native input or previous-goal edits."""
    spec = json.loads(_read(path))
    definitions = spec["participants"]
    if not isinstance(definitions, list) or len(definitions) != 1:
        raise SessionError("Exactly one existing Codex participant is required")
    definition = dict(definitions[0])
    definition["binding"] = NativeBinding(**definition["binding"])
    definition["allowed_targets"] = tuple(definition["allowed_targets"])
    participant = Participant(**definition)
    if participant.binding.provider != "codex" or participant.allowed_targets != ():
        raise SessionError("Direct Codex QA cannot enroll any peer target")
    ttl = spec.get("deadline_seconds", 900)
    if type(ttl) not in (int, float) or not 60 <= ttl <= 1800:
        raise SessionError("A bounded direct QA lifetime is required")
    store = ReasonSessions(spec["workspace"])
    if store.db_path.exists():
        operator = _read(store.operator_path).strip()
        with store._db() as db:
            config = store._config(db)
        if config["engine_path"] != spec["engine_path"] or config["engine_sha256"] != spec["engine_sha256"]:
            raise SessionError("The existing pinned QA engine cannot be silently replaced")
    else:
        store, operator = ReasonSessions.bootstrap(spec["workspace"], engine_path=spec["engine_path"], engine_sha256=spec["engine_sha256"],
            python_path=spec.get("python_path", sys.executable), delivery_timeout=spec.get("delivery_timeout", 60))
    nonce = secrets.token_hex(12)
    a, b = int(nonce[:4], 16) % 97 + 1, int(nonce[4:8], 16) % 89 + 1
    prompt = ("The human requests this new, bounded direct QA in this existing Codex conversation only. "
        "Keep your original role, session, subscription tools and native approvals. "
        "Do not contact Claude or any other session, create peers/subagents/replacement sessions, inspect history or project files, "
        "change files/plans/settings, install anything, invoke model APIs, or retry a failed/uncertain input. "
        f"Challenge nonce={nonce}; a={a}; b={b}. Compute a*b+a-b with one short local arithmetic tool call. "
        "Then call the provided helper final operation exactly once, linked to this input, with one selected JSON body "
        "containing step='direct_final', the exact nonce, a, b, and integer result. Use actual JSON double quotes. "
        "No peer send/reply is authorized. The final helper call itself is your response to the operator; do not request manual message copying. "
        "Do not display permit contents. If native approval is needed, respect it and wait.")
    run_id = str(uuid.uuid4())
    goal = store.start_human_goal(operator, prompt=prompt, participants=[participant], budget=1,
        deadline=time.time() + ttl, goal_key=spec.get("run_key", run_id))
    value = {"contract": CONTRACT, "run_id": run_id, "workspace": str(store.workspace), "goal_id": goal["goal_id"],
             "actor_id": participant.actor_id, "binding_sha256": participant.binding.sha256,
             "nonce": nonce, "a": a, "b": b, "created_at": time.time(),
             "scope": "isolated_existing_codex_direct_input_not_peer_or_gui_integration"}
    manifest = store.private / ("reason-direct-codex-qa-" + run_id + ".json")
    store._write_private(manifest, _json(value))
    return {"manifest_path": str(manifest), "goal_id": goal["goal_id"], "state": "prepared_no_native_input"}


def seed(path: Path) -> dict[str, Any]:
    value, store, operator = _manifest(path)
    return store.seed_human_goal(operator, goal_id=value["goal_id"], target=value["actor_id"])


def inspect(path: Path) -> dict[str, Any]:
    value, store, _ = _manifest(path)
    return store.public_status(value["goal_id"])


def verify(path: Path) -> dict[str, Any]:
    value, store, operator = _manifest(path)
    public = store.public_status(value["goal_id"])
    selected = store.selected_results(operator, goal_id=value["goal_id"])
    checks = {"one_original_codex": len(public["participants"]) == 1
        and public["participants"][0]["provider"] == "codex"
        and public["participants"][0]["binding_sha256"] == value["binding_sha256"],
        "one_input_budget": public["used"] == public["budget"] == 1,
        "two_selected_events": len(selected) == 2, "completed": public["state"] == "completed"}
    if len(selected) == 2:
        first, final = selected
        checks["exact_native_callback"] = (first["kind"] == "human_seed" and first["target"] == value["actor_id"]
            and first["native_response_verified"] == 1 and final["kind"] == "final"
            and final["sender"] == value["actor_id"] and final["parent_id"] == first["id"] and final["target"] is None)
        try:
            result = json.loads(final["body"])
            checks["nonce_arithmetic"] = (result.get("step") == "direct_final" and result.get("nonce") == value["nonce"]
                and type(result.get("a")) is int and result["a"] == value["a"]
                and type(result.get("b")) is int and result["b"] == value["b"]
                and type(result.get("result")) is int and result["result"] == value["a"] * value["b"] + value["a"] - value["b"])
        except (ValueError, TypeError, AttributeError):
            checks["nonce_arithmetic"] = False
    return {"contract": CONTRACT, "goal_id": value["goal_id"], "checks": checks,
        "verdict": "existing_codex_direct_final_pass" if all(checks.values()) else "incomplete_or_failed",
        "scope": "selected_exact_native_callback_not_peer_roundtrip_or_gui_or_arithmetic_tool_trace",
        "nonce_sha256": hashlib.sha256(value["nonce"].encode()).hexdigest(),
        "selected_event_hashes": [row["body_sha256"] for row in selected], "automatic_retry_requested": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    command = commands.add_parser("prepare")
    command.add_argument("--spec", required=True)
    for operation in ("seed", "inspect", "verify"):
        command = commands.add_parser(operation)
        command.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(Path(args.spec).absolute()) if args.operation == "prepare" else globals()[args.operation](Path(args.manifest).absolute())
        sys.stdout.write(_json(result) + "\n")
        return 0
    except (SessionError, OSError, ValueError, TypeError, KeyError):
        sys.stdout.write(_json({"error": "direct_codex_qa_authority_or_state_unavailable", "previous_goal_changed": False}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
