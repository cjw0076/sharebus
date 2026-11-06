"""Compile externally proposed tasks into an advisory, capability-gated graph.

This module neither decomposes natural language nor dispatches work. Callers must
obtain semantic proposals from the current human/native frontier router and must
still perform native admission against current authority before execution.
"""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import PurePosixPath
import re
import time
import unicodedata
from typing import Literal, TypedDict

from preference_learning import featureize


class Branch(TypedDict):
    id: str
    purpose: str
    objective: str
    required_capabilities: list[str]
    edit_scope: list[str]
    dependencies: list[str]
    condition: dict | None
    join: dict | None
    actor_id: str | None
    review_owner: str | None
    turn_cost: int
    state: str
    evidence_refs: list[str]
    posterior: dict | None


class BranchSet(TypedDict):
    schema: str
    advisory: Literal[True]
    dispatch_authorized: Literal[False]
    execution_verified: Literal[False]
    state: str
    branches: list[Branch]
    errors: list[dict]
    budget: dict
    control_epoch: int | None


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_WINDOWS_DEVICE = re.compile(r"(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[0-9\u00b9\u00b2\u00b3]|LPT[0-9\u00b9\u00b2\u00b3])\Z", re.I)
_WINDOWS_SHORT = re.compile(r"~[0-9]+(?:\.|\Z)")
_TASK_FIELDS = {"id", "purpose", "objective", "required_capabilities", "edit_scope",
                "dependencies", "condition", "join", "recipient_id", "review_owner", "turn_cost"}
_POSTERIOR_FIELDS = {"score", "confidence", "uncertainty", "lower_bound", "upper_bound"}
_MAX_TASKS = 64


def _identifier(value, pattern=_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("invalid_identifier")
    return value


def _strings(value, *, labels=False):
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("invalid_list")
    result = [_identifier(item, _LABEL if labels else _ID) for item in value]
    if len(result) != len(set(result)):
        raise ValueError("duplicate_list_item")
    return result


def _fresh(value, now):
    return (type(value.get("observed_at")) in {int, float}
            and type(value.get("expires_at")) in {int, float}
            and math.isfinite(value["observed_at"]) and math.isfinite(value["expires_at"])
            and 0 < value["observed_at"] <= now < value["expires_at"])


def _scopes(value):
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("invalid_edit_scope")
    result = []
    for item in value:
        if (not isinstance(item, str) or not item or len(item) > 256
                or any(character in item for character in "\\:*?[]") or any(ord(character) < 32 for character in item)
                or item.startswith("/") or any(part in {"", ".", ".."} for part in item.split("/"))):
            raise ValueError("invalid_edit_scope")
        for part in item.split("/"):
            if (part.endswith((".", " ")) or _WINDOWS_DEVICE.fullmatch(part.split(".", 1)[0].rstrip(" "))
                    or _WINDOWS_SHORT.search(part)):
                # Do not turn Win32 device, trailing-dot/space or DOS short-name
                # spellings into separate ownership. Actual filesystem aliases
                # and leases still require admission in the execution layer.
                raise ValueError("invalid_edit_scope")
        result.append(PurePosixPath(item).as_posix())
    if len(result) != len(set(result)):
        raise ValueError("duplicate_edit_scope")
    return result


def _overlap(left, right):
    # Conservative on case-sensitive volumes; safe for default Windows/macOS
    # volumes where two differently cased spellings can name the same file.
    return any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
               for a in (unicodedata.normalize("NFC", item).casefold() for item in left)
               for b in (unicodedata.normalize("NFC", item).casefold() for item in right))


class JudgmentRouter:
    """Select public actors for typed proposals using independent posteriors.

    Controls contain workspace_key, epoch, participants, human_authorized,
    paused, stopped, observed_at and expires_at. Candidate observations contain
    the same workspace_key/control_epoch, freshness, verified_capabilities and
    readiness={state:'ready',can_dispatch:True}. A readiness callback may refresh
    that last check, but cannot replace workspace, roster or capability checks.
    These are planning inputs, never a native execution grant.
    """

    def __init__(self, model, readiness=None):
        self.model = model
        self.readiness = readiness

    def plan(self, tasks: list[dict], candidates: list[dict], *, controls: dict, budget: dict) -> BranchSet:
        now = time.time()
        result: BranchSet = {"schema": "avalokita.branch-set/1", "advisory": True,
                             "dispatch_authorized": False, "execution_verified": False,
                             "state": "blocked", "branches": [], "errors": [], "budget": {},
                             "control_epoch": None}

        def block(code, task=None):
            result["errors"].append({"code": code, **({"task_id": task} if task else {})})

        if (not isinstance(controls, dict) or controls.get("human_authorized") is not True
                or controls.get("paused") is not False or controls.get("stopped") is not False
                or not _fresh(controls, now) or type(controls.get("epoch")) is not int
                or controls["epoch"] < 0):
            block("unavailable_controls")
            return result
        try:
            workspace_key = _identifier(controls.get("workspace_key"))
            roster = _strings(controls.get("participants"))
        except ValueError:
            block("invalid_control_roster")
            return result
        result["control_epoch"] = controls["epoch"]
        if (not isinstance(budget, dict) or type(budget.get("remaining_turns")) is not int
                or not 0 <= budget["remaining_turns"] <= 10000
                or type(budget.get("deadline")) not in {int, float}
                or not math.isfinite(budget["deadline"]) or budget["deadline"] <= now
                or type(budget.get("max_parallel", 1)) is not int
                or not 1 <= budget.get("max_parallel", 1) <= _MAX_TASKS):
            block("unavailable_budget")
            return result
        result["budget"] = {"remaining_turns": budget["remaining_turns"], "deadline": budget["deadline"],
                            "max_parallel": budget.get("max_parallel", 1), "reserved_turns": 0}
        if not isinstance(tasks, list) or len(tasks) > _MAX_TASKS or not isinstance(candidates, list) or len(candidates) > 128:
            block("invalid_proposals")
            return result
        normalized = []
        ids = set()
        intents = set()
        try:
            for task in tasks:
                if not isinstance(task, dict) or task.keys() - _TASK_FIELDS:
                    raise ValueError("unknown_task_field")
                task_id = _identifier(task.get("id"))
                purpose = _identifier(task.get("purpose"), _LABEL)
                objective = task.get("objective")
                if not isinstance(objective, str) or not objective.strip() or len(objective) > 32768:
                    raise ValueError("invalid_objective")
                if task_id in ids:
                    raise ValueError("duplicate_task")
                edit_scope = _scopes(task.get("edit_scope", []))
                intent = (" ".join(objective.casefold().split()), tuple(sorted(edit_scope)))
                if intent in intents:
                    raise ValueError("duplicated_intent")
                ids.add(task_id)
                intents.add(intent)
                branch: Branch = {"id": task_id, "purpose": purpose, "objective": objective,
                                  "required_capabilities": _strings(task.get("required_capabilities", []), labels=True),
                                  "edit_scope": edit_scope, "dependencies": _strings(task.get("dependencies", [])),
                                  "condition": None, "join": None, "actor_id": None,
                                  "review_owner": None, "turn_cost": task.get("turn_cost", 1),
                                  "state": "unassigned", "evidence_refs": [], "posterior": None}
                if type(branch["turn_cost"]) is not int or not 1 <= branch["turn_cost"] <= 1000:
                    raise ValueError("invalid_turn_cost")
                if task.get("condition") is not None:
                    condition = task["condition"]
                    if (not isinstance(condition, dict) or set(condition) != {"after", "when"}
                            or condition["when"] not in {"succeeded", "failed", "completed", "verified"}):
                        raise ValueError("invalid_condition")
                    after = _identifier(condition["after"])
                    if after not in branch["dependencies"]:
                        raise ValueError("condition_requires_dependency")
                    branch["condition"] = dict(condition)
                if task.get("join") is not None:
                    join = task["join"]
                    if (not isinstance(join, dict) or set(join) != {"requires", "mode"}
                            or join["mode"] not in {"all_success", "all_verified", "all_complete"}):
                        raise ValueError("invalid_join")
                    requires = _strings(join["requires"])
                    if not requires or not set(requires).issubset(branch["dependencies"]):
                        raise ValueError("join_requires_dependencies")
                    branch["join"] = {"requires": requires, "mode": join["mode"]}
                recipient = task.get("recipient_id")
                reviewer = task.get("review_owner")
                if recipient is not None:
                    _identifier(recipient)
                if reviewer is not None:
                    branch["review_owner"] = _identifier(reviewer)
                normalized.append((branch, recipient))
        except (ValueError, TypeError) as error:
            block(str(error) if isinstance(error, ValueError) else "invalid_proposal_type")
            return result
        dependencies = {branch["id"]: set(branch["dependencies"]) for branch, _ in normalized}
        if any(not parents.issubset(ids) or task_id in parents for task_id, parents in dependencies.items()):
            block("unknown_dependency")
            return result
        ancestors: dict[str, set[str]] = {}
        pending = dict(dependencies)
        while pending:
            ready = [key for key, parents in pending.items() if parents.issubset(ancestors)]
            if not ready:
                block("dependency_cycle")
                return result
            for key in ready:
                ancestors[key] = set(dependencies[key])
                for parent in dependencies[key]:
                    ancestors[key].update(ancestors[parent])
                del pending[key]
        for index, (left, _) in enumerate(normalized):
            for right, _ in normalized[index + 1:]:
                if (_overlap(left["edit_scope"], right["edit_scope"])
                        and left["id"] not in ancestors[right["id"]]
                        and right["id"] not in ancestors[left["id"]]):
                    block("parallel_edit_collision", right["id"])
        cost = sum(branch["turn_cost"] for branch, _ in normalized)
        if cost > budget["remaining_turns"]:
            block("budget_exhausted")
        if result["errors"]:
            return result
        result["budget"]["reserved_turns"] = cost
        eligible = {}
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            actor = candidate.get("id")
            if not isinstance(actor, str) or actor in seen:
                block("invalid_candidate_roster")
                return result
            seen.add(actor)
            try:
                provider = _identifier(candidate.get("provider"), _LABEL)
                capabilities = _strings(candidate.get("capabilities", []), labels=True)
                verified = _strings(candidate.get("verified_capabilities", []), labels=True)
            except ValueError:
                continue
            if (actor not in roster or candidate.get("workspace_key") != workspace_key
                    or type(candidate.get("control_epoch")) is not int
                    or candidate["control_epoch"] != controls["epoch"] or not _fresh(candidate, now)
                    or candidate.get("paused") is True or candidate.get("stopped") is True
                    or not set(verified).issubset(capabilities)):
                continue
            try:
                readiness = self.readiness(deepcopy(candidate), deepcopy(controls)) if self.readiness else candidate.get("readiness")
            except Exception:
                continue
            if (not isinstance(readiness, dict) or readiness.get("state") != "ready"
                    or readiness.get("can_dispatch") is not True):
                continue
            eligible[actor] = {"id": actor, "provider": provider, "capabilities": verified}
        for branch, recipient in normalized:
            matching = [dict(candidate) for candidate in eligible.values()
                        if set(branch["required_capabilities"]).issubset(candidate["capabilities"])
                        and (recipient is None or candidate["id"] == recipient)]
            reviewer = branch["review_owner"]
            if branch["edit_scope"] and (reviewer not in eligible or "review" not in eligible[reviewer]["capabilities"]):
                block("unavailable_review_owner", branch["id"])
                branch["state"] = "blocked"
            elif not matching:
                block("unavailable_recipient" if recipient else "unmet_capability", branch["id"])
                branch["state"] = "blocked"
            else:
                for candidate in matching:
                    candidate["capability"] = branch["required_capabilities"][0] if branch["required_capabilities"] else None
                try:
                    predictions = self.model.predict({"purpose": branch["purpose"], "features": featureize(branch["objective"], branch["purpose"]), "now": now}, matching)
                    scores = self._predictions(predictions, matching)
                except Exception:
                    scores = []
                if not scores:
                    branch["state"] = "needs_native_judgment"
                elif recipient is None and len(matching) > 1 and not any(item["model_trained"] for item in scores):
                    branch["state"] = "needs_native_judgment"
                else:
                    selected = max(scores, key=lambda item: (item["lower_bound"], item["score"], item["id"]))
                    if branch["edit_scope"] and reviewer == selected["id"]:
                        block("review_owner_conflict", branch["id"])
                        branch["state"] = "blocked"
                    else:
                        branch["actor_id"] = selected["id"]
                        branch["posterior"] = {key: selected[key] for key in _POSTERIOR_FIELDS | {"training_state", "model_trained", "model_revision"}}
                        branch["evidence_refs"] = selected["evidence_refs"]
                        branch["state"] = "conditional" if branch["condition"] else "waiting" if branch["dependencies"] else "proposed"
            result["branches"].append(branch)
        if result["errors"]:
            result["state"] = "blocked"
            # A rejected graph is never a partial permission to execute siblings.
            for branch in result["branches"]:
                branch["actor_id"] = None
        elif any(branch["state"] == "needs_native_judgment" for branch in result["branches"]):
            result["state"] = "needs_native_judgment"
        else:
            result["state"] = "ready_for_admission" if tasks else "empty"
        return result

    @staticmethod
    def _predictions(predictions, candidates):
        if not isinstance(predictions, list) or len(predictions) != len(candidates):
            return []
        roster = {item["id"]: item["provider"] for item in candidates}
        seen = set()
        safe = []
        for item in predictions:
            if not isinstance(item, dict):
                return []
            actor = item.get("id")
            if actor not in roster or actor in seen or item.get("provider") != roster[actor]:
                return []
            seen.add(actor)
            if (any(type(item.get(key)) not in {int, float} or not math.isfinite(item[key])
                    or not 0 <= item[key] <= 1 for key in _POSTERIOR_FIELDS)
                    or not item["lower_bound"] <= item["score"] <= item["upper_bound"]
                    or type(item.get("model_revision")) is not int or item["model_revision"] < 0
                    or type(item.get("model_trained")) is not bool
                    or item.get("training_state") not in {"untrained", "inferred", "feedback_trained"}
                    or not isinstance(item.get("evidence_refs"), list) or len(item["evidence_refs"]) > 16
                    or any(not isinstance(ref, str) or not _HASH.fullmatch(ref) for ref in item["evidence_refs"])):
                return []
            safe.append({key: deepcopy(item[key]) for key in _POSTERIOR_FIELDS | {"id", "training_state", "model_trained", "model_revision", "evidence_refs"}})
        return safe
