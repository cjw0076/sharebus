"""Editable collaboration preferences and truthful native execution plans.

No function in this module calls a model or changes a native host setting.
Catalog/capability evidence is supplied by the native caller, never inferred
from user-facing model names or a provider's presence on the bus.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TEAM_PATH = Path(".sharebus/team.json")
POLICY_PATH = Path("SHAREBUS.md")
MANIFEST_PATH = Path(".sharebus/policy-install.json")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
ROUTINE_TASKS = {"routine", "routine_code", "routine_test", "routine_docs"}
VISUAL_TASKS = {"visual", "design", "computer_use"}
MAIN_TASKS = {"unknown", "decision", "planning", "deep_thinking", "architecture", "auth", "security",
              "concurrency", "root_cause", "irreversible_infra"}
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


class PolicyConflict(ValueError):
    """The preference file changed or requires manual preservation/review."""


def _text(value: Any, name: str, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise ValueError(name + " must be a nonempty string without control characters")


def validate_team(value: Any) -> dict[str, Any]:
    """Return a detached validated profile; never normalize user model IDs."""
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("Team schema_version must be 1")
    if set(value) - {"schema_version", "notes", "roles", "providers"}:
        raise ValueError("Unknown team setting")
    providers, roles = value.get("providers"), value.get("roles")
    if not isinstance(providers, dict) or not providers or not isinstance(roles, dict):
        raise ValueError("Team providers and roles must be objects; providers cannot be empty")
    for provider, profiles in providers.items():
        if not isinstance(provider, str) or not KEY.fullmatch(provider) or not isinstance(profiles, dict):
            raise ValueError("Invalid provider key or profiles")
        if not {"main", "routine", "visual"} <= set(profiles) or set(profiles) - {"main", "routine", "routine_docs", "visual"}:
            raise ValueError("Provider needs main, routine, visual and optional routine_docs profiles")
        for name, profile in profiles.items():
            if not isinstance(profile, dict) or set(profile) != {"display_model", "model_id", "effort", "tier"}:
                raise ValueError("Profile needs display_model, model_id, effort and tier")
            _text(profile["display_model"], "display_model")
            _text(profile["model_id"], "model_id", nullable=True)
            if profile["model_id"] is not None and any(c.isspace() for c in profile["model_id"]):
                raise ValueError("Use an exact native catalog model_id without whitespace")
            if profile["effort"] is not None and (not isinstance(profile["effort"], str) or profile["effort"] not in EFFORTS):
                raise ValueError("Unknown effort spelling")
            if not isinstance(profile["tier"], str) or profile["tier"] not in {"frontier", "economy"} or (name in {"main", "visual"} and profile["tier"] != "frontier"):
                raise ValueError("Main and visual profiles require frontier tier")
    for role, settings in roles.items():
        if not isinstance(role, str) or not KEY.fullmatch(role) or not isinstance(settings, dict):
            raise ValueError("Invalid role key or settings")
        if (set(settings) != {"label", "preferred_provider"} or not isinstance(settings["preferred_provider"], str)
                or settings["preferred_provider"] not in providers):
            raise ValueError("Role needs label and a configured preferred_provider")
        _text(settings["label"], "role label")
    return copy.deepcopy(value)


def default_team() -> dict[str, Any]:
    return validate_team(json.loads((HERE / "templates/team.json").read_text(encoding="utf-8")))


def render_shared_policy() -> str:
    return (HERE / "templates/SHAREBUS.md").read_text(encoding="utf-8")


def _guard_workspace(workspace_guard: Callable[[], None] | None) -> None:
    if workspace_guard is not None:
        workspace_guard()


def load_team(workspace: str | Path, *, workspace_guard: Callable[[], None] | None = None) -> dict[str, Any]:
    """Load local edits or detached defaults without creating workspace files."""
    root = Path(workspace).absolute()
    _check_paths(root, workspace_guard=workspace_guard)
    _guard_workspace(workspace_guard)
    path = root.resolve(strict=True) / TEAM_PATH
    _guard_workspace(workspace_guard)
    if not path.exists():
        return default_team()
    _guard_workspace(workspace_guard)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Team preferences exceed 1 MiB")
    _guard_workspace(workspace_guard)
    return validate_team(json.loads(path.read_text(encoding="utf-8-sig")))


def default_role(workspace: str | Path, provider: str, *,
                 workspace_guard: Callable[[], None] | None = None) -> str | None:
    """Suggested label for a new identity only; never update a registered role."""
    team = load_team(workspace, workspace_guard=workspace_guard)
    return next((settings["label"] for settings in team["roles"].values()
                 if settings["preferred_provider"] == provider), None)


def _check_paths(root: Path, *, workspace_guard: Callable[[], None] | None = None) -> None:
    for path in (root, *root.parents, root / ".sharebus", root / TEAM_PATH, root / POLICY_PATH, root / MANIFEST_PATH,
                 root / ".sharebus/policy-backups"):
        _guard_workspace(workspace_guard)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Policy operation cannot follow linked workspace paths")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("Policy operation cannot use hardlinked workspace files")


def read_policy(workspace: str | Path, *, workspace_guard: Callable[[], None] | None = None) -> dict[str, Any]:
    """Public operator view: preferences/revision only; no notes, paths or tokens."""
    root = Path(workspace).absolute()
    _check_paths(root, workspace_guard=workspace_guard)
    _guard_workspace(workspace_guard)
    root = root.resolve(strict=True)
    path = root / TEAM_PATH
    _guard_workspace(workspace_guard)
    exists = path.exists()
    _guard_workspace(workspace_guard)
    raw = path.read_bytes() if exists else None
    revision = hashlib.sha256(raw).hexdigest() if raw is not None else None
    try:
        if raw is not None and len(raw) > 1024 * 1024:
            raise ValueError("Team preferences exceed 1 MiB")
        team = validate_team(json.loads(raw.decode("utf-8-sig"))) if raw is not None else default_team()
        team.pop("notes", None)
        status = "configured" if raw is not None else "default"
    except (ValueError, TypeError, KeyError):
        team, status = None, "invalid_custom"
    _guard_workspace(workspace_guard)
    return {"revision": revision, "team": team, "exists": raw is not None, "status": status,
            "preference_only": True, "runtime_enforced": False, "effort_levels": sorted(EFFORTS)}


def save_team(workspace: str | Path, value: Any, *, expected_revision: str | None,
              workspace_guard: Callable[[], None] | None = None) -> dict[str, Any]:
    """Explicit operator edit with exact-byte CAS and backup; never a model switch."""
    if expected_revision is not None and (not isinstance(expected_revision, str)
                                          or not re.fullmatch(r"[0-9a-f]{64}", expected_revision)):
        raise ValueError("expected_revision must be the current policy revision or null")
    if isinstance(value, dict) and "notes" in value:
        raise ValueError("Public preference edits cannot modify private notes")
    team = validate_team(value)
    root = Path(workspace).absolute()
    _check_paths(root, workspace_guard=workspace_guard)
    _guard_workspace(workspace_guard)
    root = root.resolve(strict=True)
    view = read_policy(root, workspace_guard=workspace_guard)
    if view["revision"] != expected_revision:
        raise PolicyConflict("Team preferences changed; reload before saving")
    if view["status"] == "invalid_custom":
        raise PolicyConflict("Existing custom team file requires local review before editing")
    path = root / TEAM_PATH
    _guard_workspace(workspace_guard)
    if path.exists():
        existing = load_team(root, workspace_guard=workspace_guard)
        if "notes" in existing:
            team["notes"] = existing["notes"]
    content = json.dumps(team, ensure_ascii=False, indent=2) + "\n"
    from datetime import datetime, timezone

    from install import ConfigurationConflict, PreparedChanges, apply
    changes = PreparedChanges([path], workspace_guard=workspace_guard)
    changes[path] = content
    changes.baseline[path] = expected_revision
    backup = root / ".sharebus/policy-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    try:
        apply(changes, backup, workspace_guard=workspace_guard)
    except ConfigurationConflict:
        raise PolicyConflict("Team preferences changed; reload before saving") from None
    return read_policy(root, workspace_guard=workspace_guard)


def plan_task(profile: dict[str, Any], *, provider: str, task_kind: str = "decision",
              role: str | None = None, existing_role: str | None = None,
              capabilities: dict[str, Any] | None = None, catalog: dict[str, Any] | None = None,
              uncertain: bool = False, root: dict[str, Any] | None = None,
              now: float | None = None) -> dict[str, Any]:
    """Plan only. Native caller verifies evidence and invokes its own agent tool.

    catalog: {provider, verified, source,
              models: {exact_id: {tier, efforts, native_model_value?}}}
    capabilities: {provider, verified, source, subagents, model_override, effort_override,
                   native_tool, model_argument, effort_argument,
                   allowed_model_values, visual_tools?,
                   observed_model_id?, observed_effort?}
    A verified flag alone is insufficient; source and provider must match. The
    caller must refresh evidence from its own native host rather than peer data.
    Supply the native current timestamp as now when root has an absolute deadline.
    This output is a plan, not dispatch authorization or a cost reservation.
    """
    team = validate_team(profile)
    if provider not in team["providers"]:
        raise ValueError("Provider is not configured in team preferences")
    if task_kind not in ROUTINE_TASKS | VISUAL_TASKS | MAIN_TASKS:
        raise ValueError("Unknown task kind; classify unbounded work as a main task")
    for name, value in (("role", role), ("existing_role", existing_role)):
        _text(value, name, nullable=True)
    if role is not None and role not in team["roles"] and existing_role is None:
        raise ValueError("Requested role is not configured")
    if type(uncertain) is not bool or (root is not None and not isinstance(root, dict)):
        raise ValueError("uncertain must be boolean and root an inherited object")
    if now is not None and (type(now) not in {int, float} or not math.isfinite(now)):
        raise ValueError("now must be a finite native timestamp")
    main = uncertain or task_kind in MAIN_TASKS
    selected = "main" if main else "visual" if task_kind in VISUAL_TASKS else "routine"
    if not main and task_kind == "routine_docs" and "routine_docs" in team["providers"][provider]:
        selected = "routine_docs"
    requested = copy.deepcopy(team["providers"][provider][selected])
    caps, models = capabilities or {}, catalog or {}
    cap_verified = (caps.get("verified") is True and caps.get("provider") == provider
                    and isinstance(caps.get("source"), str) and bool(caps["source"].strip()))
    observed = {"model_id": caps.get("observed_model_id") if cap_verified else None,
                "effort": caps.get("observed_effort") if cap_verified else None,
                "source": caps.get("source") if cap_verified else None, "verified": cap_verified}
    result: dict[str, Any] = {
        "provider": provider, "role": existing_role if existing_role is not None else role,
        "role_source": "existing_identity" if existing_role is not None else "explicit_preference" if role else "unassigned",
        "task_kind": task_kind, "profile": selected, "requested": requested,
        "observed": observed, "effective": {"mode": "current_main", **observed},
        "status": "pending_capability", "native_tool_args": None, "native_tool": None,
        "runtime_enforced": False, "policy_mode": "advisory",
        "requires_frontier_review": selected in {"routine", "routine_docs"},
        "root": copy.deepcopy(root), "reason": "Native capability evidence is missing",
    }
    if root and (root.get("cancelled") is True or root.get("paused") is True or root.get("stopped") is True
                 or any(type(root.get(key)) in {int, float} and root[key] <= 0
                        for key in ("remaining_turns", "remaining_seconds", "remaining_tokens", "remaining_cost", "budget_remaining"))
                 or (type(root.get("budget")) in {int, float} and type(root.get("used")) in {int, float}
                     and root["used"] >= root["budget"])):
        result.update(status="blocked_parent", reason="Parent cancellation, pause/stop or exhausted budget forbids delegation")
        return result
    if root and "deadline" in root:
        deadline = root["deadline"]
        if type(deadline) not in {int, float} or not math.isfinite(deadline) or now is None:
            result.update(status="pending_parent_budget", reason="Verify the inherited deadline against the native current timestamp")
            return result
        if deadline <= now:
            result.update(status="blocked_parent", reason="Inherited parent deadline expired; no delegation")
            return result
    if not cap_verified:
        return result
    catalog_verified = (models.get("verified") is True and models.get("provider") == provider
                        and isinstance(models.get("source"), str) and bool(models["source"].strip()))
    if not catalog_verified or requested["model_id"] is None:
        result.update(status="pending_catalog", reason="Resolve the requested display name against the current native catalog")
        return result
    entries = models.get("models", {})
    entry = entries.get(requested["model_id"]) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        result.update(status="unsupported_model", reason="Requested exact model_id is absent from the native catalog; no substitution")
        return result
    if entry.get("tier") != requested["tier"]:
        result.update(status="unsupported_model", reason="Catalog does not verify the requested model tier; no downgrade")
        return result
    efforts = entry.get("efforts")
    if requested["effort"] is not None and (not isinstance(efforts, list) or requested["effort"] not in efforts):
        result.update(status="unsupported_effort", reason="Requested effort is unsupported by this exact model; no substitution")
        return result
    if main:
        matches = (observed["model_id"] == requested["model_id"]
                   and (requested["effort"] is None or observed["effort"] == requested["effort"]))
        result.update(status="current_main" if matches else "pending_model_selection",
                      reason="Observed current main matches preferences" if matches else "Main model/effort selection requires native host confirmation")
        return result
    for key in ("subagents", "model_override"):
        if caps.get(key) is not True:
            result["reason"] = key + " is unsupported or unknown; retain current main"
            return result
    if selected == "visual" and caps.get("visual_tools") is not True:
        result["reason"] = "Required visual/computer-use tool availability in the native subagent is unverified"
        return result
    native_model = entry.get("native_model_value", requested["model_id"])
    allowed = caps.get("allowed_model_values")
    if not isinstance(native_model, str) or not isinstance(allowed, list) or native_model not in allowed:
        result["reason"] = "Requested model has no verified allowed native invocation value"
        return result
    needed = ["native_tool", "model_argument"]
    if requested["effort"] is not None:
        if caps.get("effort_override") is not True:
            result["reason"] = "effort_override is unsupported or unknown; retain current main"
            return result
        needed.append("effort_argument")
    if any(not isinstance(caps.get(key), str) or not KEY.fullmatch(caps[key]) for key in needed):
        result["reason"] = "Exact native tool/argument contract is unverified"
        return result
    args = {caps["model_argument"]: native_model}
    if requested["effort"] is not None:
        if caps["effort_argument"] == caps["model_argument"]:
            result["reason"] = "Model and effort arguments cannot be the same"
            return result
        args[caps["effort_argument"]] = requested["effort"]
    result.update(status="planned_native_subagent", native_tool=caps["native_tool"], native_tool_args=args,
                  effective={"mode": "planned_native_subagent", "model_id": requested["model_id"] if native_model == requested["model_id"] else None,
                             "requested_model_id": requested["model_id"], "native_model_value": native_model,
                             "effort": requested["effort"], "source": models["source"]},
                  policy_mode="verified_plan", reason="Pass these arguments to the native agent tool and observe the actual invocation")
    return result


def install_policy(workspace: str | Path, *, apply_changes: bool = False,
                   workspace_guard: Callable[[], None] | None = None) -> dict[str, Any]:
    """Prepare defaults/known-managed updates, preserving user edits byte for byte.

    Changed file rows contain content and current_hash for the native installer's
    PreparedChanges CAS/backup transaction. Standalone apply uses that same path.
    """
    root = Path(workspace).absolute()
    _check_paths(root, workspace_guard=workspace_guard)
    _guard_workspace(workspace_guard)
    root = root.resolve(strict=True)
    manifest_path = root / MANIFEST_PATH
    _guard_workspace(workspace_guard)
    manifest_exists = manifest_path.exists()
    _guard_workspace(workspace_guard)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_exists else {"schema_version": 1, "files": {}}
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unknown policy installation manifest; preserve it for review")
    previous = manifest["files"]
    desired = {POLICY_PATH: render_shared_policy(), TEAM_PATH: json.dumps(default_team(), ensure_ascii=False, indent=2) + "\n"}
    files, managed = [], {}
    for relative, content in desired.items():
        path = root / relative
        _guard_workspace(workspace_guard)
        exists = path.exists()
        _guard_workspace(workspace_guard)
        raw = path.read_bytes() if exists else None
        old_hash = hashlib.sha256(raw).hexdigest() if raw is not None else None
        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        status = ("create" if raw is None else "unchanged" if old_hash == new_hash else
                  "update" if old_hash == previous.get(relative.as_posix()) else "preserved_custom")
        row: dict[str, Any] = {"path": str(path), "status": status, "current_hash": old_hash, "new_hash": new_hash}
        if status != "preserved_custom":
            managed[relative.as_posix()] = new_hash
        if status in {"create", "update"}:
            row["content"] = content
        files.append(row)
    manifest_text = json.dumps({"schema_version": 1, "files": managed}, indent=2) + "\n"
    _guard_workspace(workspace_guard)
    manifest_exists = manifest_path.exists()
    _guard_workspace(workspace_guard)
    old_manifest = manifest_path.read_bytes() if manifest_exists else None
    if old_manifest != manifest_text.encode("utf-8"):
        files.append({"path": str(manifest_path), "status": "update" if old_manifest is not None else "create",
                      "current_hash": hashlib.sha256(old_manifest).hexdigest() if old_manifest is not None else None,
                      "new_hash": hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(), "content": manifest_text})
    result = {"files": files, "applied": apply_changes, "shared_policy_path": str(root / POLICY_PATH),
              "team_path": str(root / TEAM_PATH)}
    if apply_changes:
        from datetime import datetime, timezone

        from install import PreparedChanges, apply
        changes = PreparedChanges([Path(row["path"]) for row in files if "content" in row],
                                  workspace_guard=workspace_guard)
        for row in files:
            if "content" in row:
                path = Path(row["path"])
                changes[path] = row["content"]
                changes.baseline[path] = row["current_hash"]
        backup = root / ".sharebus/policy-backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        result["backups_path"] = str(backup)
        result["writes"] = apply(changes, backup, workspace_guard=workspace_guard)
    return result
