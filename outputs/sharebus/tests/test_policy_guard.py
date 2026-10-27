"""Temp-only workspace retirement barriers; no stores, native hosts or models."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from functools import partial
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import install
import policy


class RetiredWorkspace(ValueError):
    pass


class WorkspaceFixture:
    """A live, permanently latched guard plus actual replaceable Temp roots."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="sharebus-policy-guard-")
        self.base = Path(self.directory.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        policy.install_policy(self.root, apply_changes=True)
        self.original = self.base / "original"
        self.replacement = self.base / "replacement"
        shutil.copytree(self.root, self.replacement)
        info = self.root.lstat()
        self.identity = (info.st_dev, info.st_ino)
        self.retired = self.mutating = self.guarding = False
        self.error = RetiredWorkspace("fixture workspace permanently retired")
        self.attempts: list[tuple[str, str]] = []
        self.trigger = None
        self.hits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.directory.cleanup()

    def guard(self):
        if self.retired:
            raise self.error
        self.guarding = True
        try:
            info = self.root.lstat()
            if (info.st_dev, info.st_ino) != self.identity:
                self.retired = True
                raise self.error
        finally:
            self.guarding = False

    def swap_and_retire(self):
        self.mutating = True
        try:
            self.root.rename(self.original)
            self.replacement.rename(self.root)
            try:
                self.guard()
            except RetiredWorkspace:
                pass
            else:
                raise AssertionError("actual replacement did not retire the guard")
        finally:
            self.mutating = False

    def restore_original(self):
        self.mutating = True
        try:
            self.root.rename(self.replacement)
            self.original.rename(self.root)
        finally:
            self.mutating = False

    def prepare(self, *, guarded=True):
        target = self.root / "configuration.json"
        target.write_bytes(b"original configuration\n")
        (self.replacement / target.name).write_bytes(b"replacement sentinel\n")
        changes = install.PreparedChanges([target], workspace_guard=self.guard if guarded else None)
        changes[target] = "new configuration\n"
        return target, changes, self.root / "backups/snapshot"

    def observe(self, operation, path):
        if self.guarding or self.mutating:
            return
        lexical = Path(os.path.abspath(os.fspath(path)))
        if self.retired and lexical.is_relative_to(self.root):
            self.attempts.append((operation, str(lexical)))
            raise AssertionError("retired operation attempted workspace I/O: " + operation)

    def after(self, operation, path):
        if self.trigger is None or self.guarding or self.mutating:
            return
        name, predicate, ordinal = self.trigger
        if operation == name and predicate(path):
            self.hits += 1
            if self.hits == ordinal:
                self.trigger = None
                self.swap_and_retire()

    @contextmanager
    def tracked(self, trigger=None):
        """Record attempted lexical replacement I/O, excluding guard identity checks."""
        self.trigger = trigger
        with ExitStack() as stack:
            for name in ("lstat", "stat", "exists", "is_symlink", "is_file", "resolve",
                         "read_bytes", "read_text", "open", "mkdir", "chmod", "unlink"):
                original = getattr(Path, name)

                def wrapped(path, *args, _name=name, _original=original, **kwargs):
                    self.observe(_name, path)
                    result = _original(path, *args, **kwargs)
                    self.after(_name, path)
                    return result

                stack.enter_context(patch.object(Path, name, wrapped))
            for owner, name in ((shutil, "copy2"), (os, "replace")):
                original = getattr(owner, name)

                def paired(source, target, *args, _name=name, _original=original, **kwargs):
                    self.observe(_name, source)
                    self.observe(_name, target)
                    result = _original(source, target, *args, **kwargs)
                    self.after(_name, source)
                    return result

                stack.enter_context(patch.object(owner, name, paired))
            yield


class PolicyGuardTests(unittest.TestCase):
    def assert_retired(self, fixture, operation):
        with self.assertRaises(RetiredWorkspace) as caught:
            operation()
        self.assertIs(caught.exception, fixture.error)
        self.assertEqual(fixture.attempts, [])

    def test_each_policy_reader_rechecks_after_an_old_path_exists_result(self):
        operations = {
            "load_team": lambda root, guard: policy.load_team(root, workspace_guard=guard),
            "default_role": lambda root, guard: policy.default_role(root, "codex", workspace_guard=guard),
            "read_policy": lambda root, guard: policy.read_policy(root, workspace_guard=guard),
            "install_policy": lambda root, guard: policy.install_policy(root, workspace_guard=guard),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name), WorkspaceFixture() as fixture:
                target = fixture.root / (policy.MANIFEST_PATH if name == "install_policy" else policy.TEAM_PATH)
                with fixture.tracked(("exists", lambda path, target=target: path == target, 1)):
                    self.assert_retired(fixture, partial(operation, fixture.root, fixture.guard))
                self.assertEqual(fixture.hits, 1)

    def test_save_and_install_apply_pass_the_live_guard_into_baseline_construction(self):
        for name in ("save", "install"):
            with self.subTest(operation=name), WorkspaceFixture() as fixture:
                view = policy.read_policy(fixture.root)
                render = policy.render_shared_policy() + "\nManaged fixture update.\n"
                original = install.PreparedChanges.__init__

                def before_baseline(changes, paths, _original=original, **kwargs):
                    fixture.swap_and_retire()
                    _original(changes, paths, **kwargs)

                with fixture.tracked(), patch.object(install.PreparedChanges, "__init__", before_baseline), \
                        patch.object(policy, "render_shared_policy", return_value=render):
                    if name == "save":
                        operation = partial(policy.save_team, fixture.root, view["team"],
                                            expected_revision=view["revision"], workspace_guard=fixture.guard)
                    else:
                        operation = partial(policy.install_policy, fixture.root, apply_changes=True,
                                            workspace_guard=fixture.guard)
                    self.assert_retired(fixture, operation)

    def test_read_policy_does_not_turn_retirement_value_error_into_invalid_custom(self):
        with WorkspaceFixture() as fixture:
            def retire_during_validation(value):
                fixture.swap_and_retire()
                fixture.guard()

            with fixture.tracked(), patch.object(policy, "validate_team", side_effect=retire_during_validation):
                self.assert_retired(fixture, lambda: policy.read_policy(fixture.root, workspace_guard=fixture.guard))

    def test_restoring_original_directory_does_not_revive_any_old_entry_point(self):
        with WorkspaceFixture() as fixture:
            view = policy.read_policy(fixture.root)
            target, changes, backup = fixture.prepare()
            fixture.swap_and_retire()
            fixture.restore_original()
            operations = (
                lambda: policy.load_team(fixture.root, workspace_guard=fixture.guard),
                lambda: policy.default_role(fixture.root, "codex", workspace_guard=fixture.guard),
                lambda: policy.read_policy(fixture.root, workspace_guard=fixture.guard),
                lambda: policy.save_team(fixture.root, view["team"], expected_revision=view["revision"],
                                         workspace_guard=fixture.guard),
                lambda: policy.install_policy(fixture.root, workspace_guard=fixture.guard),
                lambda: install.checksum(target, workspace_guard=fixture.guard),
                lambda: install.PreparedChanges([target], workspace_guard=fixture.guard),
                lambda: install.apply(changes, backup),
            )
            with fixture.tracked():
                for operation in operations:
                    self.assert_retired(fixture, operation)

    def test_checksum_rechecks_before_reading_a_file_that_previously_existed(self):
        with WorkspaceFixture() as fixture:
            target, _, _ = fixture.prepare()
            with fixture.tracked(("exists", lambda path: path == target, 1)):
                self.assert_retired(fixture, lambda: install.checksum(target, workspace_guard=fixture.guard))

    def test_apply_enforces_retained_and_explicit_callbacks(self):
        for retained in (True, False):
            with self.subTest(retained=retained), WorkspaceFixture() as fixture:
                _, changes, backup = fixture.prepare(guarded=retained)
                fixture.swap_and_retire()
                with fixture.tracked():
                    self.assert_retired(fixture, partial(install.apply, changes, backup,
                                        workspace_guard=(lambda: None) if retained else fixture.guard))

    def test_apply_rechecks_between_validation_backup_write_and_final_cleanup(self):
        barriers = (
            ("backup path validation", "lstat", lambda path, target, backup: path == target.parent, 1),
            ("preflight read", "read_bytes", lambda path, target, backup: path == target, 1),
            ("content comparison", "read_bytes", lambda path, target, backup: path == target, 2),
            ("backup mkdir", "mkdir", lambda path, target, backup: path == backup, 1),
            ("backup directory chmod", "chmod", lambda path, target, backup: path == backup, 1),
            ("backup copy", "copy2", lambda path, target, backup: path == target, 1),
            ("backup file chmod", "chmod", lambda path, target, backup: path.parent == backup, 1),
            ("target mkdir", "mkdir", lambda path, target, backup: path == target.parent, 1),
            ("atomic replacement", "replace", lambda path, target, backup: path.parent == target.parent, 1),
        )
        for label, name, predicate, ordinal in barriers:
            with self.subTest(barrier=label), WorkspaceFixture() as fixture:
                target, changes, backup = fixture.prepare()
                trigger = (name, partial(lambda path, selected, target, backup: selected(path, target, backup),
                                         selected=predicate, target=target, backup=backup), ordinal)
                with fixture.tracked(trigger):
                    self.assert_retired(fixture, partial(install.apply, changes, backup))
                self.assertEqual(fixture.hits, ordinal)

    def test_retirement_after_temp_creation_does_not_probe_or_unlink_replacement_temp(self):
        with WorkspaceFixture() as fixture:
            target, changes, backup = fixture.prepare()
            original = install.checksum
            calls = 0
            temp_name = None

            def retire_before_final_checksum(path, **kwargs):
                nonlocal calls, temp_name
                calls += 1
                if calls == 2:
                    pending = list(fixture.root.glob("*.sharebus-install-*.tmp"))
                    self.assertEqual(len(pending), 1)
                    temp_name = pending[0].name
                    (fixture.replacement / temp_name).write_bytes(b"replacement temporary sentinel")
                    fixture.swap_and_retire()
                return original(path, **kwargs)

            with fixture.tracked(), patch.object(install, "checksum", side_effect=retire_before_final_checksum):
                self.assert_retired(fixture, lambda: install.apply(changes, backup))
            self.assertIsNotNone(temp_name)
            assert isinstance(temp_name, str)
            self.assertTrue((fixture.original / temp_name).is_file())
            self.assertEqual((fixture.root / temp_name).read_bytes(), b"replacement temporary sentinel")
            self.assertEqual(target.read_bytes(), b"replacement sentinel\n")

    def test_save_preserves_one_shot_guard_exception_at_apply_backup_and_replace(self):
        for boundary in ("apply entry", "backup", "replace"):
            with self.subTest(boundary=boundary), WorkspaceFixture() as fixture:
                view = policy.read_policy(fixture.root)
                view["team"]["roles"]["ideation"]["preferred_provider"] = "claude"
                refusal = ValueError("one-shot guard refusal")
                state = {"armed": False, "fired": False, "later_calls": 0, "checksums": 0}

                def one_shot_guard(state=state, refusal=refusal):
                    if state["armed"] and not state["fired"]:
                        state["fired"] = True
                        fixture.swap_and_retire()
                        raise refusal
                    if state["fired"]:
                        state["later_calls"] += 1
                    else:
                        fixture.guard()

                with fixture.tracked(), ExitStack() as stack:
                    if boundary == "apply entry":
                        original = install.private_backup_root

                        def arm_at_entry(*args, _original=original, state=state, **kwargs):
                            state["armed"] = True
                            return _original(*args, **kwargs)

                        stack.enter_context(patch.object(install, "private_backup_root", side_effect=arm_at_entry))
                    elif boundary == "backup":
                        original = install.shutil.copy2

                        def arm_after_copy(*args, _original=original, state=state, **kwargs):
                            result = _original(*args, **kwargs)
                            state["armed"] = True
                            return result

                        stack.enter_context(patch.object(install.shutil, "copy2", side_effect=arm_after_copy))
                    else:
                        original = install.checksum

                        def arm_before_replace(*args, _original=original, state=state, **kwargs):
                            result = _original(*args, **kwargs)
                            state["checksums"] += 1
                            if state["checksums"] == 3:
                                state["armed"] = True
                            return result

                        stack.enter_context(patch.object(install, "checksum", side_effect=arm_before_replace))
                    with self.assertRaises(ValueError) as caught:
                        policy.save_team(fixture.root, view["team"], expected_revision=view["revision"],
                                         workspace_guard=one_shot_guard)
                    self.assertTrue(state["fired"])
                    self.assertIs(caught.exception, refusal)
                    self.assertEqual(state["later_calls"], 0)
                    self.assertEqual(fixture.attempts, [])
                    one_shot_guard()
                    self.assertEqual(state["later_calls"], 1)
                if boundary == "replace":
                    self.assertEqual(len(list(fixture.original.rglob("*.sharebus-install-*.tmp"))), 1)

    def test_guard_refusal_is_latched_only_for_the_current_apply_call(self):
        with WorkspaceFixture() as fixture:
            target, changes, backup = fixture.prepare(guarded=False)
            refusal = ValueError("first apply guard refusal")
            calls = 0

            def one_shot_guard():
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise refusal

            with self.assertRaises(ValueError) as caught:
                install.apply(changes, backup, workspace_guard=one_shot_guard)
            self.assertIs(caught.exception, refusal)
            self.assertEqual(calls, 1)
            install.apply(changes, backup, workspace_guard=one_shot_guard)
            self.assertEqual(target.read_bytes(), b"new configuration\n")

    def test_save_preserves_storage_value_error_instead_of_reporting_a_cas_conflict(self):
        with WorkspaceFixture() as fixture:
            view = policy.read_policy(fixture.root)
            failure = ValueError("backup validation refused")
            with patch.object(install, "private_backup_root", side_effect=failure), self.assertRaises(ValueError) as caught:
                policy.save_team(fixture.root, view["team"], expected_revision=view["revision"],
                                 workspace_guard=fixture.guard)
            self.assertIs(caught.exception, failure)

    def test_permitted_guard_preserves_notes_revision_backups_and_managed_updates(self):
        with WorkspaceFixture() as fixture:
            path = fixture.root / policy.TEAM_PATH
            team = policy.load_team(fixture.root, workspace_guard=fixture.guard)
            team["notes"] = "private fixture notes"
            path.write_text(json.dumps(team), encoding="utf-8")
            previous = path.read_bytes()
            view = policy.read_policy(fixture.root, workspace_guard=fixture.guard)
            view["team"]["roles"]["ideation"]["preferred_provider"] = "claude"
            saved = policy.save_team(fixture.root, view["team"], expected_revision=view["revision"],
                                     workspace_guard=fixture.guard)
            self.assertNotEqual(saved["revision"], view["revision"])
            self.assertNotIn("notes", saved["team"])
            self.assertFalse(saved["runtime_enforced"])
            self.assertEqual(policy.load_team(fixture.root, workspace_guard=fixture.guard)["notes"], team["notes"])
            self.assertTrue(any(item.read_bytes() == previous for item in
                                (fixture.root / ".sharebus/policy-backups").rglob("*.bak")))
            with self.assertRaises(policy.PolicyConflict):
                policy.save_team(fixture.root, view["team"], expected_revision=view["revision"], workspace_guard=fixture.guard)
            old_policy = (fixture.root / policy.POLICY_PATH).read_bytes()
            updated = policy.render_shared_policy() + "\nManaged fixture revision.\n"
            with patch.object(policy, "render_shared_policy", return_value=updated):
                receipt = policy.install_policy(fixture.root, apply_changes=True, workspace_guard=fixture.guard)
            self.assertEqual((fixture.root / policy.POLICY_PATH).read_bytes(), updated.encode())
            self.assertTrue(any(item.read_bytes() == old_policy for item in Path(receipt["backups_path"]).glob("*.bak")))
            self.assertFalse(list(fixture.root.rglob("*.db")))

    def test_normal_cas_failure_removes_only_its_temp_and_preserves_concurrent_edit(self):
        with WorkspaceFixture() as fixture:
            target, changes, backup = fixture.prepare()
            original = install.checksum
            calls = 0

            def concurrent_edit(path, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    target.write_bytes(b"concurrent edit\n")
                return original(path, **kwargs)

            with patch.object(install, "checksum", side_effect=concurrent_edit), self.assertRaises(install.ConfigurationConflict):
                install.apply(changes, backup)
            self.assertEqual(target.read_bytes(), b"concurrent edit\n")
            self.assertFalse(list(fixture.root.glob("*.sharebus-install-*.tmp")))
            self.assertEqual(next(backup.glob("*.bak")).read_bytes(), b"original configuration\n")

    def test_preflight_cas_failure_has_the_dedicated_value_error_type(self):
        with WorkspaceFixture() as fixture:
            target, changes, backup = fixture.prepare()
            target.write_bytes(b"concurrent preflight edit\n")
            with self.assertRaises(install.ConfigurationConflict) as caught:
                install.apply(changes, backup)
            self.assertIsInstance(caught.exception, ValueError)
            self.assertEqual(target.read_bytes(), b"concurrent preflight edit\n")
            self.assertFalse(backup.exists())
            self.assertFalse(list(fixture.root.glob("*.sharebus-install-*.tmp")))

    def test_standalone_default_calls_keep_working_without_a_callback(self):
        with WorkspaceFixture() as fixture:
            target, changes, backup = fixture.prepare(guarded=False)
            install.apply(changes, backup)
            self.assertEqual(install.checksum(target), hashlib.sha256(b"new configuration\n").hexdigest())
            self.assertEqual(policy.default_role(fixture.root, "codex"), "Frontend & Design")
            self.assertEqual(policy.read_policy(fixture.root)["status"], "configured")


if __name__ == "__main__":
    unittest.main()
