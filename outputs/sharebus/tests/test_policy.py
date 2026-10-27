"""Local model-policy checks; no model calls, native settings, or services."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import policy


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-policy-")
        self.root = Path(self.directory.name).resolve()
        self.team = policy.default_team()
        for provider in self.team["providers"]:
            for name, profile in self.team["providers"][provider].items():
                profile["model_id"] = provider + "-fixture-" + name

    def tearDown(self):
        self.directory.cleanup()

    def capabilities(self, provider="codex", **extra):
        return {"provider": provider, "verified": True, "source": "synthetic native capability receipt",
                "subagents": True, "model_override": True, "effort_override": True,
                "native_tool": "Agent", "model_argument": "model", "effort_argument": "effort",
                "allowed_model_values": [row["model_id"] for row in self.team["providers"][provider].values()],
                "visual_tools": True,
                "observed_model_id": provider + "-fixture-main", "observed_effort": "high", **extra}

    def catalog(self, provider="codex"):
        return {"provider": provider, "verified": True, "source": "synthetic native catalog receipt",
                "models": {row["model_id"]: {"tier": row["tier"], "efforts": ["low", "medium", "high"]}
                           for row in self.team["providers"][provider].values()}}

    def plan(self, **extra):
        extra.setdefault("task_kind", "routine")
        return policy.plan_task(self.team, provider="codex", capabilities=self.capabilities(),
                                catalog=self.catalog(), **extra)

    def test_unclassified_work_defaults_to_frontier_main_and_routine_requires_explicit_classification(self):
        default = policy.plan_task(self.team, provider="codex", capabilities=self.capabilities(), catalog=self.catalog())
        self.assertEqual(default["task_kind"], "decision")
        self.assertEqual(default["profile"], "main")
        self.assertEqual(default["requested"]["tier"], "frontier")
        self.assertIsNone(default["native_tool_args"])
        unknown = self.plan(task_kind="unknown")
        self.assertEqual(unknown["profile"], "main")
        self.assertIsNone(unknown["native_tool_args"])
        routine = self.plan(task_kind="routine_code")
        self.assertEqual(routine["requested"]["tier"], "economy")
        self.assertEqual(routine["status"], "planned_native_subagent")

    def test_load_defaults_read_only_and_custom_provider_roles_are_editable(self):
        first = policy.load_team(self.root)
        self.assertFalse((self.root / ".sharebus").exists())
        self.assertTrue(all(row["model_id"] is None for rows in first["providers"].values() for row in rows.values()))
        first["roles"]["CEO"] = {"label": "기존 최고경영자", "preferred_provider": "codex"}
        first["roles"]["ideation"]["preferred_provider"] = "claude"
        first["providers"]["custom_host"] = copy.deepcopy(first["providers"]["codex"])
        first["providers"]["custom_host"]["routine"]["display_model"] = "My own model preference"
        path = self.root / policy.TEAM_PATH
        path.parent.mkdir()
        path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(policy.load_team(self.root), first)
        first["roles"].clear()
        self.assertTrue(policy.default_team()["roles"])

    def test_role_preferences_do_not_replace_registered_ceo_or_cto(self):
        for role in ("CEO", "CTO", "기존 역할"):
            plan = self.plan(role="frontend_design", existing_role=role)
            self.assertEqual(plan["role"], role)
            self.assertEqual(plan["role_source"], "existing_identity")
        self.assertIsNone(self.plan()["role"])

    def test_effort_typo_and_unsafe_visual_tier_are_rejected(self):
        for invalid in ("hgh", "HIGH", [], 3):
            changed = copy.deepcopy(self.team)
            changed["providers"]["codex"]["routine"]["effort"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                policy.validate_team(changed)
        for name in ("main", "visual"):
            changed = copy.deepcopy(self.team)
            changed["providers"]["codex"][name]["tier"] = "economy"
            with self.assertRaises(ValueError):
                policy.validate_team(changed)

    def test_routine_verified_native_args_are_a_plan_not_enforced_execution(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "planned_native_subagent")
        self.assertEqual(plan["native_tool_args"], {"model": "codex-fixture-routine", "effort": "medium"})
        self.assertEqual(plan["effective"]["mode"], "planned_native_subagent")
        self.assertEqual(plan["observed"]["model_id"], "codex-fixture-main")
        self.assertTrue(plan["requires_frontier_review"])
        self.assertFalse(plan["runtime_enforced"])
        self.assertFalse((self.root / ".sharebus").exists())

    def test_visual_design_and_computer_use_remain_frontier_in_subagents(self):
        for kind in ("visual", "design", "computer_use"):
            plan = self.plan(task_kind=kind)
            self.assertEqual(plan["profile"], "visual")
            self.assertEqual(plan["requested"]["tier"], "frontier")
            self.assertEqual(plan["native_tool_args"]["model"], "codex-fixture-visual")
        catalog = self.catalog()
        catalog["models"]["codex-fixture-visual"]["tier"] = "economy"
        plan = policy.plan_task(self.team, provider="codex", task_kind="visual",
                                capabilities=self.capabilities(), catalog=catalog)
        self.assertEqual(plan["status"], "unsupported_model")
        self.assertIsNone(plan["native_tool_args"])
        self.assertEqual(plan["effective"]["mode"], "current_main")

    def test_important_code_and_uncertainty_stay_on_main_without_switching_it(self):
        for kind in policy.MAIN_TASKS:
            with self.subTest(kind=kind):
                plan = self.plan(task_kind=kind)
                self.assertEqual(plan["profile"], "main")
                self.assertEqual(plan["status"], "current_main")
                self.assertIsNone(plan["native_tool_args"])
                self.assertFalse(plan["runtime_enforced"])
        self.assertEqual(self.plan(uncertain=True)["profile"], "main")
        caps = self.capabilities(observed_model_id="other-current-main")
        plan = policy.plan_task(self.team, provider="codex", task_kind="security", capabilities=caps, catalog=self.catalog())
        self.assertEqual(plan["status"], "pending_model_selection")
        self.assertEqual(plan["effective"]["model_id"], "other-current-main")
        self.assertIsNone(plan["native_tool_args"])

    def test_missing_unverified_or_foreign_catalog_never_executes_display_name(self):
        for catalog in (None, {}, {"verified": True, "provider": "codex"}, self.catalog("claude")):
            plan = policy.plan_task(self.team, provider="codex", task_kind="routine", capabilities=self.capabilities(), catalog=catalog)
            self.assertEqual(plan["status"], "pending_catalog")
            self.assertIsNone(plan["native_tool_args"])
        plan = policy.plan_task(policy.default_team(), provider="codex", task_kind="routine", capabilities=self.capabilities(), catalog=self.catalog())
        self.assertIsNone(plan["requested"]["model_id"])
        self.assertEqual(plan["status"], "pending_catalog")
        self.assertIsNone(plan["native_tool_args"])

    def test_unavailable_requested_id_or_effort_never_falls_back_to_cheaper_entry(self):
        catalog = self.catalog()
        del catalog["models"]["codex-fixture-visual"]
        plan = policy.plan_task(self.team, provider="codex", task_kind="design", capabilities=self.capabilities(), catalog=catalog)
        self.assertEqual(plan["status"], "unsupported_model")
        self.assertEqual(plan["requested"]["model_id"], "codex-fixture-visual")
        self.assertIsNone(plan["native_tool_args"])
        catalog = self.catalog()
        catalog["models"]["codex-fixture-routine"]["efforts"] = ["low"]
        plan = policy.plan_task(self.team, provider="codex", task_kind="routine", capabilities=self.capabilities(), catalog=catalog)
        self.assertEqual(plan["status"], "unsupported_effort")
        self.assertIsNone(plan["native_tool_args"])

    def test_unsupported_unknown_or_wrong_host_capability_retains_main(self):
        for key in ("subagents", "model_override", "effort_override"):
            for value in (False, None):
                caps = self.capabilities(**{key: value})
                plan = policy.plan_task(self.team, provider="codex", task_kind="routine", capabilities=caps, catalog=self.catalog())
                self.assertEqual(plan["status"], "pending_capability")
                self.assertIsNone(plan["native_tool_args"])
                self.assertEqual(plan["effective"]["model_id"], "codex-fixture-main")
        for caps in (None, {}, self.capabilities(verified=False), self.capabilities(source=""), self.capabilities("claude")):
            plan = policy.plan_task(self.team, provider="codex", task_kind="routine", capabilities=caps, catalog=self.catalog())
            self.assertEqual(plan["status"], "pending_capability")
            self.assertFalse(plan["runtime_enforced"])
            self.assertIsNone(plan["native_tool_args"])
            self.assertIsNone(plan["observed"]["model_id"])
            self.assertIsNone(plan["observed"]["effort"])
            self.assertFalse(plan["observed"]["verified"])
            self.assertIsNone(plan["effective"]["model_id"])

    def test_native_argument_contract_must_be_known_and_nonconflicting(self):
        for extra in ({"model_argument": None}, {"native_tool": None}, {"effort_argument": "model"}, {"allowed_model_values": None}):
            plan = policy.plan_task(self.team, provider="codex", task_kind="routine", capabilities=self.capabilities(**extra), catalog=self.catalog())
            self.assertEqual(plan["status"], "pending_capability")
            self.assertIsNone(plan["native_tool_args"])
        self.team["providers"]["codex"]["routine"]["effort"] = None
        plan = policy.plan_task(self.team, provider="codex", task_kind="routine", capabilities=self.capabilities(effort_override=False), catalog=self.catalog())
        self.assertEqual(plan["native_tool_args"], {"model": "codex-fixture-routine"})

    def test_tier_selector_mapping_does_not_claim_an_actual_model_or_visual_tools(self):
        self.team["providers"]["antigravity"]["visual"]["effort"] = None
        catalog = self.catalog("antigravity")
        catalog["models"]["antigravity-fixture-visual"]["native_model_value"] = "pro"
        caps = self.capabilities("antigravity", allowed_model_values=["inherit", "flash", "pro"], effort_override=False)
        plan = policy.plan_task(self.team, provider="antigravity", task_kind="computer_use", capabilities=caps, catalog=catalog)
        self.assertEqual(plan["native_tool_args"], {"model": "pro"})
        self.assertIsNone(plan["effective"]["model_id"])
        self.assertEqual(plan["effective"]["native_model_value"], "pro")
        self.assertFalse(plan["runtime_enforced"])
        caps["visual_tools"] = False
        plan = policy.plan_task(self.team, provider="antigravity", task_kind="computer_use", capabilities=caps, catalog=catalog)
        self.assertEqual(plan["status"], "pending_capability")
        self.assertIsNone(plan["native_tool_args"])

    def test_parent_goal_root_budget_and_cancellation_are_preserved_without_mutation(self):
        root = {"root_id": "human-root", "correlation_id": "parent-task", "goal": "original goal",
                "remaining_turns": 2, "deadline": 12345, "cancelled": True}
        before_team, before_root = copy.deepcopy(self.team), copy.deepcopy(root)
        plan = self.plan(root=root)
        self.assertEqual(plan["root"], root)
        self.assertEqual(plan["status"], "blocked_parent")
        self.assertIsNone(plan["native_tool_args"])
        plan["root"]["remaining_turns"] = 99
        self.assertEqual(root, before_root)
        self.assertEqual(self.team, before_team)
        for control in ({"paused": True}, {"stopped": True}, {"remaining_turns": 0}, {"remaining_seconds": -1}, {"budget": 2, "used": 2}):
            plan = self.plan(root=control)
            self.assertEqual(plan["status"], "blocked_parent")
            self.assertIsNone(plan["native_tool_args"])

    def test_parent_deadline_needs_native_clock_and_blocks_expiry(self):
        parent = {"root_id": "original-root", "budget": 24, "used": 2, "deadline": 100}
        pending = self.plan(root=parent)
        self.assertEqual(pending["status"], "pending_parent_budget")
        self.assertIsNone(pending["native_tool_args"])
        expired = self.plan(root=parent, now=101)
        self.assertEqual(expired["status"], "blocked_parent")
        self.assertIsNone(expired["native_tool_args"])
        self.assertEqual(self.plan(root=parent, now=99)["status"], "planned_native_subagent")

    def test_claude_routine_docs_uses_edited_docs_profile(self):
        plan = policy.plan_task(self.team, provider="claude", task_kind="routine_docs",
                                capabilities=self.capabilities("claude"), catalog=self.catalog("claude"))
        self.assertEqual(plan["profile"], "routine_docs")
        self.assertEqual(plan["native_tool_args"], {"model": "claude-fixture-routine_docs", "effort": "low"})

    def test_policy_preparation_is_read_only_and_existing_custom_bytes_survive(self):
        plan = policy.install_policy(self.root)
        self.assertFalse(plan["applied"])
        self.assertFalse((self.root / ".sharebus").exists())
        self.assertTrue(all("content" in row for row in plan["files"]))
        custom_policy = b"# Existing custom shared policy\r\nKeep this exact text\r\n"
        custom_team = b'{"custom": "keep bytes even if this is not schema v1"}\r\n'
        (self.root / policy.POLICY_PATH).write_bytes(custom_policy)
        (self.root / policy.TEAM_PATH).parent.mkdir()
        (self.root / policy.TEAM_PATH).write_bytes(custom_team)
        receipt = policy.install_policy(self.root, apply_changes=True)
        self.assertEqual((self.root / policy.POLICY_PATH).read_bytes(), custom_policy)
        self.assertEqual((self.root / policy.TEAM_PATH).read_bytes(), custom_team)
        rows = receipt["files"][:2]
        self.assertEqual([row["status"] for row in rows], ["preserved_custom", "preserved_custom"])
        self.assertTrue(all("content" not in row for row in rows))

    def test_known_managed_revision_updates_with_backup_and_then_custom_edits_are_kept(self):
        first = policy.install_policy(self.root, apply_changes=True)
        self.assertEqual(policy.load_team(self.root), policy.default_team())
        old = (self.root / policy.POLICY_PATH).read_bytes()
        updated = policy.render_shared_policy() + "\nNew managed revision.\n"
        with patch.object(policy, "render_shared_policy", return_value=updated):
            second = policy.install_policy(self.root, apply_changes=True)
        self.assertEqual((self.root / policy.POLICY_PATH).read_text(encoding="utf-8"), updated)
        backup_files = list(Path(second["backups_path"]).glob("*.bak"))
        self.assertTrue(any(path.read_bytes() == old for path in backup_files))
        custom = updated + "\nMy own policy edit.\n"
        (self.root / policy.POLICY_PATH).write_text(custom, encoding="utf-8")
        third = policy.install_policy(self.root, apply_changes=True)
        self.assertEqual(third["files"][0]["status"], "preserved_custom")
        self.assertEqual((self.root / policy.POLICY_PATH).read_text(encoding="utf-8"), custom)
        self.assertTrue(first["writes"])

    def test_apply_preserves_edit_made_after_preparation(self):
        import install
        prepared = policy.install_policy(self.root)
        rows = [row for row in prepared["files"] if "content" in row]
        changes = install.PreparedChanges([Path(row["path"]) for row in rows])
        for row in rows:
            changes[Path(row["path"])] = row["content"]
            changes.baseline[Path(row["path"])] = row["current_hash"]
        (self.root / policy.POLICY_PATH).write_text("Concurrent user edit\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            install.apply(changes, self.root / "backups")
        self.assertEqual((self.root / policy.POLICY_PATH).read_text(encoding="utf-8"), "Concurrent user edit\n")
        self.assertFalse((self.root / policy.TEAM_PATH).exists())

    def test_unknown_manifest_is_rejected_without_replacing_files(self):
        path = self.root / policy.MANIFEST_PATH
        path.parent.mkdir()
        path.write_text('{"schema_version":99,"files":{}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            policy.install_policy(self.root, apply_changes=True)
        self.assertFalse((self.root / policy.POLICY_PATH).exists())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(b'{"schema_version":99,"files":{}}').hexdigest())

    def test_public_policy_view_hides_notes_and_saves_customization_with_cas_backup(self):
        policy.install_policy(self.root, apply_changes=True)
        path = self.root / policy.TEAM_PATH
        team = policy.load_team(self.root)
        team["notes"] = "private-note-with-local-path-and-token-sentinel"
        path.write_text(json.dumps(team), encoding="utf-8")
        raw = path.read_bytes()
        view = policy.read_policy(self.root)
        self.assertNotIn("private-note", json.dumps(view))
        self.assertNotIn(str(self.root), json.dumps(view))
        self.assertNotIn("notes", view["team"])
        view["team"]["roles"]["ideation"]["preferred_provider"] = "claude"
        view["team"]["providers"]["codex"]["routine"]["display_model"] = "Edited preferred model"
        saved = policy.save_team(self.root, view["team"], expected_revision=view["revision"])
        self.assertNotEqual(saved["revision"], view["revision"])
        self.assertEqual(saved["team"]["providers"]["codex"]["routine"]["display_model"], "Edited preferred model")
        self.assertEqual(policy.load_team(self.root)["notes"], team["notes"])
        self.assertTrue(saved["preference_only"])
        self.assertFalse(saved["runtime_enforced"])
        self.assertTrue(any(backup.read_bytes() == raw for backup in (self.root / ".sharebus/policy-backups").rglob("*.bak")))
        with self.assertRaises(policy.PolicyConflict):
            policy.save_team(self.root, view["team"], expected_revision=view["revision"])
        self.assertEqual(policy.read_policy(self.root)["revision"], saved["revision"])

    def test_public_save_defaults_invalid_custom_and_missing_revision_are_truthful(self):
        view = policy.read_policy(self.root)
        self.assertEqual(view["status"], "default")
        self.assertIsNone(view["revision"])
        self.assertFalse((self.root / ".sharebus").exists())
        saved = policy.save_team(self.root, view["team"], expected_revision=None)
        self.assertEqual(saved["status"], "configured")
        self.assertEqual(policy.default_role(self.root, "codex"), "Frontend & Design")
        self.assertIsNone(policy.default_role(self.root, "unknown-provider"))
        path = self.root / policy.TEAM_PATH
        path.write_bytes(b'{"legacy_custom_token":"must remain local"}')
        invalid = policy.read_policy(self.root)
        self.assertEqual(invalid["status"], "invalid_custom")
        self.assertIsNone(invalid["team"])
        self.assertNotIn("must remain local", json.dumps(invalid))
        before = path.read_bytes()
        with self.assertRaises(policy.PolicyConflict):
            policy.save_team(self.root, view["team"], expected_revision=invalid["revision"])
        self.assertEqual(path.read_bytes(), before)

    def test_public_save_rejects_invalid_effort_and_private_notes_without_writing(self):
        view = policy.read_policy(self.root)
        view["team"]["providers"]["codex"]["routine"]["effort"] = "high-ish"
        with self.assertRaises(ValueError):
            policy.save_team(self.root, view["team"], expected_revision=None)
        self.assertFalse((self.root / ".sharebus").exists())
        team = policy.default_team()
        with self.assertRaises(ValueError):
            policy.save_team(self.root, team, expected_revision=None)
        self.assertFalse((self.root / ".sharebus").exists())

    def test_linked_preference_files_are_not_read_enrolled_or_replaced(self):
        source = self.root / "separate-original.json"
        source.write_text(json.dumps(policy.default_team()), encoding="utf-8")
        target = self.root / policy.TEAM_PATH
        target.parent.mkdir()
        os.link(source, target)
        original = source.read_bytes()
        for operation in (policy.load_team, policy.read_policy, policy.install_policy,
                          lambda root: policy.default_role(root, "codex")):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                operation(self.root)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((self.root / policy.POLICY_PATH).exists())


if __name__ == "__main__":
    unittest.main()
