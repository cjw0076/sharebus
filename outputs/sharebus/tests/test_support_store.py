"""Adverse durable help-state tests; no native/model process or user-home writes."""
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support_store import PURPOSE, SupportError, SupportPaths, SupportStore, digest


@pytest.fixture
def support(tmp_path):
    workspace, root = tmp_path / "selected", tmp_path / "app-owned"
    workspace.mkdir()
    paths = SupportPaths(workspace, root=root)
    paths.ensure()
    store = SupportStore(paths)
    store.open("claude", "initial-spec")
    return workspace, paths, store


def active(store, text="도움이 필요해요", key="human-request-001"):
    job, capability = store.create_job(text, key)
    store.reserve_native(job["id"], "exact-owned-native")
    store.receipt(job["id"], {"type": "input_attempt"})
    store.receipt(job["id"], {"type": "session", "session_id": "exact-owned-native"})
    store.profile_ready(job["id"])
    return job, capability


def test_paths_capture_and_help_open_do_not_touch_target(support):
    workspace, paths, store = support
    assert list(workspace.iterdir()) == []
    assert not paths.directory.is_relative_to(workspace)
    binding = store.binding()
    assert binding["purpose"] == PURPOSE
    assert binding["native_id"] is None and binding["reserved_id"] is None
    assert binding["state"] == "ready"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_old_binding_schema_adds_profile_reason_without_replacing_native_identity(tmp_path):
    workspace = tmp_path / "selected"
    workspace.mkdir()
    paths = SupportPaths(workspace, root=tmp_path / "help")
    paths.ensure()
    with sqlite3.connect(paths.file("support.sqlite3")) as db:
        db.execute("CREATE TABLE binding (one INTEGER PRIMARY KEY,target TEXT,device INTEGER,inode INTEGER,"
                   "purpose TEXT,provider TEXT,native_id TEXT,reserved_id TEXT,state TEXT,epoch INTEGER,spec_hash TEXT)")
        db.execute("INSERT INTO binding VALUES (1,?,?,?,?,?,'saved-native','saved-native','ready',7,'initial-spec')",
                   (str(workspace), paths.selection.device, paths.selection.inode, PURPOSE, "claude"))
    reopened = SupportStore(paths)
    binding = reopened.open("claude", "initial-spec")
    assert binding["native_id"] == "saved-native" and binding["reserved_id"] == "saved-native"
    assert binding["epoch"] == 7 and binding["reason_code"] is None
    assert reopened.public("offline")["guide"]["can_send"]


def test_profile_block_survives_restart_and_only_positive_current_reprobe_clears(support):
    _, paths, store = support
    first, _ = active(store)
    store.finish(first["id"], text="selected reply", native_id="exact-owned-native")
    failed, capability = store.create_job("later help", "human-request-002")
    store.finish(failed["id"], state="failed", reason_code="unsupported_version")
    reopened = SupportStore(paths)
    reopened.recover()
    binding = reopened.open("claude", "initial-spec")
    assert binding["native_id"] == "exact-owned-native"
    assert reopened.public("offline")["guide"] == {
        "provider": "claude", "label": "ShareBus 도움", "state": "error", "can_send": False,
        "reason_code": "unsupported_version", "active_request_id": None}
    with pytest.raises(SupportError):
        reopened.create_job("try again", "human-request-003")
    with pytest.raises(SupportError):
        reopened.authorize_diagnostic(capability)
    assert not reopened.refresh_profile(binding["epoch"] - 1, None)
    assert reopened.refresh_profile(binding["epoch"], "profile_unverified")
    assert reopened.public("offline")["guide"]["can_send"] is False
    current = reopened.binding()
    assert reopened.refresh_profile(current["epoch"], None)
    assert reopened.public("offline")["guide"]["state"] == "ready"
    assert reopened.public("offline")["guide"]["can_send"] is True
    assert reopened.binding()["native_id"] == "exact-owned-native"
    with reopened.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_profile_failure_after_attempt_remains_unknown_and_cannot_be_reprobed(support):
    _, _, store = support
    job, _ = active(store)
    store.finish(job["id"], state="failed", reason_code="unsupported_version")
    binding = store.binding()
    assert binding["state"] == "unknown" and binding["reason_code"] is None
    assert not store.refresh_profile(binding["epoch"], None)
    assert not store.public("offline")["guide"]["can_send"]
    assert binding["native_id"] == "exact-owned-native"


def test_concurrent_open_is_one_immutable_binding(support):
    _, _, store = support
    with ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(lambda _: store.open("claude", "initial-spec"), range(16)))
    assert all(row == rows[0] for row in rows)
    with pytest.raises(SupportError, match="Claude"):
        store.open("codex", "initial-spec")
    assert store.binding()["provider"] == "claude"


def test_duplicate_ack_loss_key_creates_one_job_and_mismatch_refused(support):
    _, _, store = support
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _: store.create_job("same human text", "one-request-key"), range(16)))
    assert len({job["id"] for job, _ in outcomes}) == 1
    assert sum(token is not None for _, token in outcomes) == 1
    with pytest.raises(SupportError) as error:
        store.create_job("changed human text", "one-request-key")
    assert error.value.code == "duplicate" and error.value.status == 409
    with pytest.raises(SupportError):
        store.create_job(" same human text", "one-request-key")
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_capability_requires_active_job_attempt_and_verified_inventory(support):
    _, _, store = support
    job, token = store.create_job("diagnose", "human-request-001")
    with pytest.raises(SupportError):
        store.authorize_diagnostic(token)
    store.reserve_native(job["id"], "exact-owned-native")
    store.receipt(job["id"], {"type": "input_attempt"})
    with pytest.raises(SupportError):
        store.authorize_diagnostic(token)
    store.receipt(job["id"], {"type": "session", "session_id": "exact-owned-native"})
    store.profile_ready(job["id"])
    assert store.authorize_diagnostic(token)["id"] == job["id"]
    with pytest.raises(SupportError):
        store.authorize_diagnostic("wrong-human-capability")
    assert store.finish(job["id"], text="selected reply", native_id="exact-owned-native")
    with pytest.raises(SupportError):
        store.authorize_diagnostic(token)
    assert store.public("paused")["workspace"]["status"] == "paused"


def test_expiry_and_spec_refresh_revoke_old_capability_preserve_conversation(support):
    _, _, store = support
    job, token = active(store)
    store.open("claude", "updated-spec")
    with pytest.raises(SupportError):
        store.authorize_diagnostic(token)
    public = store.public("offline")
    assert public["guide"]["state"] == "unknown"
    assert public["guide"]["can_send"] is False
    assert public["messages"][0]["status"] == "unknown"
    assert store.binding()["native_id"] == "exact-owned-native"
    assert store.binding()["spec_hash"] == "updated-spec"


def test_expired_diagnostic_and_late_result_are_not_completed(support):
    _, _, store = support
    job, token = active(store)
    with store.connection() as db:
        db.execute("UPDATE jobs SET deadline=? WHERE id=?", (time.time() - 1, job["id"]))
    with pytest.raises(SupportError):
        store.authorize_diagnostic(token)
    store.finish(job["id"], text="late answer", native_id="exact-owned-native")
    result = store.public("offline")
    assert result["guide"]["state"] == "unknown"
    assert [message["role"] for message in result["messages"]] == ["user"]


def test_restart_input_attempt_recovers_unknown_without_replay(support):
    _, paths, store = support
    job, token = active(store)
    reopened = SupportStore(paths)
    reopened.recover()
    assert reopened.binding()["native_id"] == "exact-owned-native"
    assert reopened.public("offline")["guide"]["state"] == "unknown"
    with pytest.raises(SupportError) as error:
        reopened.create_job("do it again", "human-request-002")
    assert error.value.code == "unknown"
    repeated, second_token = reopened.create_job("도움이 필요해요", "human-request-001")
    assert repeated["id"] == job["id"] and second_token is None
    with pytest.raises(SupportError):
        reopened.authorize_diagnostic(token)


def test_cancel_before_input_and_cancel_after_input_differ(support):
    _, _, store = support
    first, cap = store.create_job("not sent", "human-request-001")
    assert store.cancel(first["request_id"]) == "cancelled"
    assert store.public("offline")["guide"]["can_send"] is True
    second, cap = active(store, "already sent", "human-request-002")
    assert store.cancel(second["request_id"]) == "unknown"
    assert not store.finish(second["id"], text="late answer", native_id="exact-owned-native")
    assert store.public("offline")["guide"]["active_request_id"] == second["request_id"]
    with pytest.raises(SupportError):
        store.authorize_diagnostic(cap)


def test_replaced_folder_refuses_existing_and_new_controller_binding(support):
    workspace, paths, store = support
    workspace.rename(workspace.with_name("original-preserved"))
    workspace.mkdir()
    with pytest.raises(SupportError) as error:
        store.create_job("different folder", "human-request-001")
    assert error.value.code == "workspace"
    assert store.public("offline")["guide"]["state"] == "stale"
    new_paths = SupportPaths(workspace, root=paths.root)
    new_paths.ensure()
    with pytest.raises(SupportError) as error:
        SupportStore(new_paths).open("claude", "initial-spec")
    assert error.value.code == "stale"
    assert list(workspace.iterdir()) == []


def test_os_lease_excludes_owner_and_stale_file_does_not(support):
    _, paths, _ = support
    with paths.lease():
        with pytest.raises(SupportError) as error:
            with paths.lease():
                pytest.fail("A second owner acquired the active OS lock")
        assert error.value.code == "busy"
    assert paths.file("support.guard").is_file()
    with paths.lease():
        pass


def test_private_link_rejected_and_root_inside_target_refused(support):
    workspace, paths, _ = support
    with pytest.raises(SupportError):
        SupportPaths(workspace, root=workspace / "hidden-help")
    external = paths.private / "other-private-file"
    external.write_text("private", encoding="utf-8")
    linked = paths.private / "operator.token"
    import os
    os.link(external, linked)
    with pytest.raises(SupportError):
        paths.file("operator.token")


def test_wrong_observed_native_id_and_reserved_creation_never_clone(support):
    _, _, store = support
    job, token = store.create_job("help", "human-request-001")
    store.reserve_native(job["id"], "own-reserved-id")
    with pytest.raises(SupportError):
        store.receipt(job["id"], {"type": "session", "session_id": "peer-provided-id"})
    store.finish(job["id"], state="failed")
    assert store.public("offline")["guide"]["state"] == "unknown"
    assert store.binding()["native_id"] is None
    with pytest.raises(SupportError):
        store.create_job("try another session", "human-request-002")


def test_exception_after_attempt_in_existing_native_conversation_is_unknown(support):
    _, _, store = support
    first, _ = active(store)
    store.finish(first["id"], text="first reply", native_id="exact-owned-native")
    second, _ = active(store, "second request", "human-request-002")
    store.finish(second["id"], state="failed", native_id="exact-owned-native")
    assert store.public("offline")["guide"]["state"] == "unknown"
    assert store.public("offline")["guide"]["can_send"] is False


def test_public_state_has_stable_ids_request_correlation_without_private_binding(support):
    workspace, _, store = support
    job, token = active(store)
    store.finish(job["id"], text="도움을 드릴게요", native_id="exact-owned-native")
    first, second = store.public("paused"), store.public("paused")
    assert first == second
    assert {message["request_id"] for message in first["messages"]} == {job["request_id"]}
    assert {message["role"] for message in first["messages"]} == {"user", "assistant"}
    assert first["guide"]["active_request_id"] is None
    raw = json.dumps(first)
    for value in (token, "exact-owned-native", str(workspace), "spec_hash", "cap_hash", "native_id"):
        assert value not in raw


@pytest.mark.parametrize("text,key", [("", "request-001"), ("x" * 12001, "request-001"), ("\ud800", "request-001"),
                                       ("hello", "bad/../key"), ("hello", None)])
def test_bad_input_rejected_before_creating_job(support, text, key):
    _, _, store = support
    with pytest.raises(SupportError):
        store.create_job(text, key)
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["support.sqlite3-journal", "support.sqlite3-wal", "support.sqlite3-shm"])
def test_metadata_leaf_vanishes_during_stat_without_adopting_another_path(support, monkeypatch, name):
    _, paths, _ = support
    leaf = paths.private / name
    leaf.write_text("optional sqlite fixture", encoding="utf-8")
    original = Path.lstat
    vanished = []
    def disappear(path, *args, **kwargs):
        if path == leaf:
            vanished.append(path)
            path.unlink()
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", disappear)
    assert paths.file(name) == leaf
    assert vanished == [leaf] and not leaf.exists()
    paths.target_check()
    paths.check()


@pytest.mark.parametrize("change", ["private-replaced", "root-replaced", "private-missing", "root-missing"])
def test_metadata_leaf_absence_revalidates_parent_and_root(support, tmp_path, monkeypatch, change):
    _, paths, _ = support
    leaf = paths.private / "support.sqlite3-journal"
    leaf.write_text("optional sqlite fixture", encoding="utf-8")
    original = Path.lstat
    changed = []
    def replaced_parent(path, *args, **kwargs):
        if path == leaf:
            source = paths.private if change.startswith("private") else paths.root
            preserved = source.with_name(source.name + "-preserved")
            assert source.resolve().is_relative_to(tmp_path.resolve())
            assert preserved.resolve().is_relative_to(tmp_path.resolve())
            source.rename(preserved)
            if change.endswith("replaced"):
                if change.startswith("root"):
                    paths.private.mkdir(parents=True)
                else:
                    source.mkdir()
            changed.append(source)
            raise FileNotFoundError("fixture leaf disappeared")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", replaced_parent)
    with pytest.raises(SupportError) as caught:
        paths.file(leaf.name)
    assert caught.value.code == "storage"
    assert len(changed) == 1


@pytest.mark.parametrize("change", ["replaced", "missing"])
def test_metadata_leaf_absence_preserves_stale_read_but_denies_changed_target_mutation(
        support, tmp_path, monkeypatch, change):
    workspace, paths, store = support
    leaf = paths.private / "support.sqlite3-journal"
    leaf.write_text("optional sqlite fixture", encoding="utf-8")
    original = Path.lstat
    changed = []
    def changed_target(path, *args, **kwargs):
        if path == leaf and not changed:
            preserved = workspace.with_name(workspace.name + "-preserved")
            assert workspace.resolve().is_relative_to(tmp_path.resolve())
            assert preserved.resolve().is_relative_to(tmp_path.resolve())
            workspace.rename(preserved)
            if change == "replaced":
                workspace.mkdir()
            leaf.unlink()
            changed.append(True)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", changed_target)
    assert paths.file(leaf.name) == leaf  # Metadata lookup grants no input authority.
    assert store.public("offline")["guide"]["state"] == "stale"
    with pytest.raises(SupportError) as caught:
        store.create_job("must not reach native input", "changed-target-request")
    assert caught.value.code == "workspace"
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "reparse"])
def test_metadata_leaf_present_unsafe_file_is_still_refused(support, tmp_path, monkeypatch, kind):
    _, paths, _ = support
    leaf = paths.private / "support.sqlite3-journal"
    if kind == "hardlink":
        source = tmp_path / "private-fixture"
        source.write_text("private fixture", encoding="utf-8")
        os.link(source, leaf)
    elif kind == "directory":
        leaf.mkdir()
    else:
        leaf.write_text("private fixture", encoding="utf-8")
        original = Path.lstat
        def unsafe_metadata(path, *args, **kwargs):
            if path == leaf:
                return SimpleNamespace(st_mode=(stat.S_IFLNK if kind == "symlink" else stat.S_IFREG) | 0o600,
                                       st_nlink=1, st_file_attributes=0x400 if kind == "reparse" else 0)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "lstat", unsafe_metadata)
    with pytest.raises(SupportError) as caught:
        paths.file(leaf.name)
    assert caught.value.code == "storage"


@pytest.mark.parametrize("error", [PermissionError("private fixture denied"),
                                  OSError(errno.EIO, "private fixture IO error"),
                                  NotADirectoryError("private fixture invalid parent")])
def test_metadata_leaf_other_stat_failures_are_never_absence(support, monkeypatch, error):
    _, paths, _ = support
    leaf = paths.private / "support.sqlite3-journal"
    leaf.write_text("optional sqlite fixture", encoding="utf-8")
    original = Path.lstat
    def denied(path, *args, **kwargs):
        if path == leaf:
            raise error
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(type(error)) as caught:
        paths.file(leaf.name)
    assert caught.value is error
