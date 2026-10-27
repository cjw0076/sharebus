"""Finite export failure cases using synthetic text files only."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import collect_release as collect
from check_release import scan_bytes


@pytest.mark.parametrize("name", ["../secret", ".sharebus/room", "C:/input", "a\\b", "CON.txt", "room.sqlite3"])
def test_unsafe_file_names_are_rejected(name):
    with pytest.raises(ValueError):
        collect.relative(name)


def fixture(tmp_path, monkeypatch, files):
    own = tmp_path / "prep"
    origin = tmp_path / "source"
    own.mkdir()
    origin.mkdir()
    (own / "release-files.json").write_text(json.dumps({"files": list(files)}))
    entries = []
    for name, data in files.items():
        target = origin / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append({"source_slot": "fixture", "source": name, "destination": name,
                        "sha256": collect.digest(data)})
    frozen = own / "freeze.json"
    frozen.write_text(json.dumps({"schema": "sharebus-source-freeze/1", "approved": True,
                                 "complete": False, "files": entries}))
    monkeypatch.setattr(collect, "ROOT", own)
    return origin, frozen, own / "candidate", entries


def test_exact_finite_collection_and_public_provenance(tmp_path, monkeypatch):
    origin, frozen, output, _ = fixture(tmp_path, monkeypatch, {"README.md": b"public\n"})
    result = collect.collect(frozen, collect.digest(frozen.read_bytes()), {"fixture": origin}, output, execute=True)
    assert result["files"] == 1 and result["written"]
    manifest = json.loads((output / "SOURCE-MANIFEST.json").read_text())
    assert manifest["files"][0]["source_class"] == "fixture"
    assert str(origin) not in json.dumps(manifest)
    assert (output / "README.md").read_bytes() == b"public\n"


def test_late_hash_drift_writes_no_partial_candidate(tmp_path, monkeypatch):
    origin, frozen, output, _ = fixture(tmp_path, monkeypatch, {"README.md": b"safe", "LICENSE": b"notice"})
    (origin / "LICENSE").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        collect.collect(frozen, collect.digest(frozen.read_bytes()), {"fixture": origin}, output, execute=True)
    assert not output.exists()


def test_untrusted_selection_hash_and_existing_destination_fail(tmp_path, monkeypatch):
    origin, frozen, output, _ = fixture(tmp_path, monkeypatch, {"README.md": b"safe"})
    with pytest.raises(ValueError, match="trusted selection"):
        collect.collect(frozen, "0" * 64, {"fixture": origin}, output, execute=True)
    output.mkdir()
    with pytest.raises(ValueError, match="fresh candidate"):
        collect.collect(frozen, collect.digest(frozen.read_bytes()), {"fixture": origin}, output, execute=True)


def test_private_text_is_reported_without_exposing_matched_content():
    value = (chr(67) + ":/" + "/".join(("Users", "example", "secret"))).encode()
    report = scan_bytes("README.md", value)
    assert report == ["review literal-user-path: README.md:1"]
    assert "example" not in report[0]


def test_one_site_subscription_derivations_are_not_reapplied():
    original = b'                options: dict[str, Any] = {"on_event": lambda event: self._native_event(agent_id, event)}\r\n'
    changed = collect.subscription_only_owned(original)
    assert b'if agent["mode"] != "native_attached":\r\n' in changed
    with pytest.raises(ValueError):
        collect.subscription_only_owned(changed)
    proxy = b'        super().__init__("claude", workspace, agent_id, token_file, on_event)\n'
    changed = collect.subscription_only_claude_proxy(proxy)
    assert b"subscription_only=True" in changed
    with pytest.raises(ValueError):
        collect.subscription_only_claude_proxy(changed)
