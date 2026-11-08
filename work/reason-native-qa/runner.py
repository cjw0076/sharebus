"""Operator runner for one exact existing-native A -> B -> A selected QA.

No live action occurs on import, prepare, inspect, or verify. Only the explicit
operator ``seed`` command requests native input, once. Subsequent sends must
come from the real native helper hosts. Never use this isolated controller as
evidence of production composer/Harmony integration or general usefulness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from dataclasses import asdict
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

REVIEW_REQUEST = (
    "This is only a bounded native-peer transport QA, not project work. Keep your existing CEO role and native approvals. "
    "Do not inspect unrelated history, edit the project, install anything, invoke model APIs, create subagents, or start another session. "
    "Generate your own fresh 32-character lowercase hex nonce_b locally. Compute SHA256 UTF-8 of nonce_a + '|' + nonce_b. "
    "Provide a concrete pair of distinct 32-character hex values sharing the proposed first-six-character ID as a counterexample. "
    "Reply to this exact incoming input using the provided native helper reply operation with one selected JSON body: "
    "{step:'review',nonce_a:ORIGINAL,nonce_b:FRESH,digest:FULL_SHA256,collision:{left:VALUE,right:VALUE},recommendation:'full_sha256'}. "
    "Use real JSON double quotes. Do not ask the operator to copy your reply and do not print private permit contents."
)


def seed_prompt(initiator: str, reviewer: str) -> str:
    return (
        "The human authorizes one bounded existing-native peer verification in this workspace. "
        "Preserve both existing roles/conversations, subscription tools, and native approvals. Do not read project files or unrelated history, "
        "change project/plans/settings, install anything, use model APIs, create subagents, or create replacement sessions. "
        "Only local nonce computation, helper status, one peer send/reply, and one selected final are in scope. "
        f"You are the enrolled initiator {initiator}; your exact reviewer is {reviewer}. "
        "Generate your own fresh 32-character lowercase hex nonce_a locally, after receiving this input. "
        "Propose candidate_id=nonce_a[:6], ask the reviewer to challenge whether that short prefix is sufficient as a unique proof. "
        "Send one selected JSON body through helper send to the exact reviewer, with step='challenge', nonce_a, proposal='short_prefix', "
        "candidate_id, and review_request set verbatim to the following review contract: "
        + REVIEW_REQUEST + "\n"
        "After the reviewer responds through the native helper, recompute the full digest using its fresh nonce_b, check its collision counterexample, "
        "and publish one helper final result linked to that exact reply input: "
        "{step:'final',nonce_a:ORIGINAL,nonce_b:REVIEWER_NONCE,digest:VERIFIED_FULL_SHA256,chosen_policy:'full_sha256',review_applied:true}. "
        "Use real JSON double quotes. Your final must depend on the reviewer's new information; do not anticipate or invent its reply. "
        "The operator seeds only this first input and does not relay peer bodies. A transport ACK is not the final result. "
        "If native approval is required, respect it; if delivery fails or is uncertain, stop instead of retrying with a new key."
    )


def _manifest(path: Path) -> tuple[dict[str, Any], ReasonSessions, str]:
    value = json.loads(_read(path))
    if value.get("contract") != "avalokita.reason-native-qa/1":
        raise SessionError("A pinned private QA manifest is required")
    store = ReasonSessions(value["workspace"])
    if path.parent != store.private:
        raise SessionError("QA manifests stay in the workspace private directory")
    return value, store, _read(store.operator_path).strip()


def prepare(spec_path: Path) -> dict[str, Any]:
    """Trusted operator setup; no native invocation and no known session IDs in source."""
    spec = json.loads(_read(spec_path))
    workspace = Path(spec["workspace"]).absolute()
    participants = []
    for definition in spec["participants"]:
        item = dict(definition)
        item["binding"] = NativeBinding(**item["binding"])
        item["allowed_targets"] = tuple(item["allowed_targets"])
        participants.append(Participant(**item))
    initiator, reviewer = spec["initiator"], spec["reviewer"]
    if {p.actor_id for p in participants} != {initiator, reviewer} or initiator == reviewer:
        raise SessionError("Exactly two pinned existing native peers are required")
    ttl = spec.get("deadline_seconds", 900)
    if type(ttl) not in (int, float) or not 60 <= ttl <= 1800:
        raise SessionError("A bounded QA lifetime is required")
    store = ReasonSessions(workspace)
    if store.db_path.exists():
        operator = _read(store.operator_path).strip()
        with store._db() as db:
            config = store._config(db)
        if config["engine_path"] != spec["engine_path"] or config["engine_sha256"] != spec["engine_sha256"]:
            raise SessionError("An existing QA engine pin cannot be silently replaced")
    else:
        store, operator = ReasonSessions.bootstrap(workspace, engine_path=spec["engine_path"], engine_sha256=spec["engine_sha256"],
            python_path=spec.get("python_path", sys.executable), delivery_timeout=spec.get("delivery_timeout", 60))
    run_id = str(uuid.uuid4())
    goal = store.start_human_goal(operator, prompt=seed_prompt(initiator, reviewer), participants=participants,
        budget=3, deadline=time.time() + ttl, goal_key=spec.get("run_key", run_id))
    value = {"contract": "avalokita.reason-native-qa/1", "run_id": run_id, "workspace": str(workspace),
             "goal_id": goal["goal_id"], "initiator": initiator, "reviewer": reviewer,
             "participants_sha256": hashlib.sha256(_json([asdict(p) for p in participants]).encode()).hexdigest(),
             "created_at": time.time(), "automatic_retries": 0, "operator_peer_relays": 0,
             "scope": "isolated_native_qa_not_production_composer"}
    path = store.private / ("reason-native-qa-" + run_id + ".json")
    store._write_private(path, _json(value))
    return {"manifest_path": str(path), "goal_id": goal["goal_id"], "state": "prepared_no_native_input"}


def seed(path: Path) -> dict[str, Any]:
    value, store, operator = _manifest(path)
    return store.seed_human_goal(operator, goal_id=value["goal_id"], target=value["initiator"])


def inspect(path: Path) -> dict[str, Any]:
    value, store, _ = _manifest(path)
    return store.public_status(value["goal_id"])


def verify(path: Path) -> dict[str, Any]:
    value, store, operator = _manifest(path)
    public = store.public_status(value["goal_id"])
    selected = store.selected_results(operator, goal_id=value["goal_id"])
    checks = {"exact_four_events": len(selected) == 4, "native_inputs_three": public["used"] == 3,
              "all_native_callbacks": bool(selected) and all(row["native_response_verified"] == 1 for row in selected),
              "completed": public["state"] == "completed"}
    if len(selected) == 4:
        first, challenge, review, final = selected
        checks["exact_causal_chain"] = (first["kind"] == "human_seed" and first["target"] == value["initiator"]
            and challenge["sender"] == value["initiator"] and challenge["target"] == value["reviewer"] and challenge["parent_id"] == first["id"]
            and review["sender"] == value["reviewer"] and review["target"] == value["initiator"] and review["parent_id"] == challenge["id"]
            and final["sender"] == value["initiator"] and final["kind"] == "final" and final["parent_id"] == review["id"])
        try:
            a, b, result = (json.loads(row["body"]) for row in (challenge, review, final))
            nonce_a, nonce_b = a.get("nonce_a"), b.get("nonce_b")
            checks["native_fresh_nonces"] = (isinstance(nonce_a, str) and isinstance(nonce_b, str)
                and re.fullmatch(r"[0-9a-f]{32}", nonce_a) is not None
                and re.fullmatch(r"[0-9a-f]{32}", nonce_b) is not None and nonce_a != nonce_b
                and nonce_a not in first["body"] and nonce_b not in first["body"] and nonce_b not in challenge["body"])
            digest = hashlib.sha256((str(nonce_a) + "|" + str(nonce_b)).encode()).hexdigest()
            collision = b.get("collision", {})
            left, right = collision.get("left"), collision.get("right")
            checks["review_counterexample"] = (isinstance(left, str) and isinstance(right, str) and left != right
                and re.fullmatch(r"[0-9a-f]{32}", left) is not None and re.fullmatch(r"[0-9a-f]{32}", right) is not None
                and left[:6] == right[:6] == a.get("candidate_id") and a.get("candidate_id") == nonce_a[:6])
            checks["review_changes_final"] = (a.get("step") == "challenge" and a.get("proposal") == "short_prefix"
                and b.get("step") == "review" and b.get("nonce_a") == nonce_a and b.get("digest") == digest
                and b.get("recommendation") == "full_sha256" and result.get("step") == "final"
                and result.get("nonce_a") == nonce_a and result.get("nonce_b") == nonce_b and result.get("digest") == digest
                and result.get("chosen_policy") == "full_sha256" and result.get("review_applied") is True)
        except (ValueError, TypeError, AttributeError):
            checks["selected_result_schema"] = False
    passed = bool(checks) and all(checks.values())
    return {"contract": "avalokita.reason-native-qa-verification/1", "goal_id": value["goal_id"],
            "verdict": "native_peer_roundtrip_pass" if passed else "incomplete_or_failed", "checks": checks,
            "scope": "isolated_existing_native_nonce_review_not_production_gui_or_usefulness",
            "automatic_retry_requested": False, "operator_peer_relay_implemented": False,
            "selected_event_hashes": [row["body_sha256"] for row in selected]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare_parser = commands.add_parser("prepare", help="Trusted operator only; metadata and private QA files, no native input")
    prepare_parser.add_argument("--spec", required=True)
    for operation in ("seed", "inspect", "verify"):
        command = commands.add_parser(operation)
        command.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(Path(args.spec).absolute()) if args.operation == "prepare" else globals()[args.operation](Path(args.manifest).absolute())
        sys.stdout.write(_json(result) + "\n")
        return 0
    except (SessionError, OSError, ValueError, TypeError, KeyError):
        sys.stdout.write(_json({"error": "operator_qa_configuration_or_state_unavailable"}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
