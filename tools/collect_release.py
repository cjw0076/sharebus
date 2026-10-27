"""Collect a finite reviewed source freeze into a fresh local candidate tree."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat


ROOT = Path(__file__).resolve().parents[1]
CORE_PINS = {
    "daemon.py": "3325cc8d22a3b7e86aafd1e3d0e5007e881171aa33f20b6e2f1f6131da351191",
    "supervisor.py": "c7749a3a025e652ecb9ba3b1a6a8d7b83b819eeefd3b830eef14900148735f4e",
    "store.py": "0d5af21f824074cbf9a797ee98db77bb58c5edc7bafd8de0b5310d29964c5f30",
    "hosted_runtime.py": "7aec171974369e0e164cd11733fe8b733624723b37f3e4fb0df0ea2fc49e3211",
}
FORBIDDEN_PARTS = {".sharebus", ".omc", ".git", "private", "credentials", "evidence", "__pycache__"}
FORBIDDEN_NAMES = {"codex-spritesheet.webp", "clawd-laptop.webm", "clawd-poster.png", "antigravity-official-mark.svg"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(value: str) -> Path:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("Expected a safe relative POSIX file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part.casefold() in FORBIDDEN_PARTS for part in path.parts):
        raise ValueError("Private or escaping source path is not a release input")
    for part in path.parts:
        if (part.endswith((" ", ".")) or part.casefold().split(".")[0]
                in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}):
            raise ValueError("Reserved or ambiguous Windows file name")
    if path.name in FORBIDDEN_NAMES or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".token", ".pyc"}:
        raise ValueError("Private or unlicensed input file")
    return Path(*path.parts)


def read_regular(root: Path, name: str, *, limit: int = 4 * 1024 * 1024) -> bytes:
    path = root.absolute() / relative(name)
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Linked or reparse-point source rejected")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError("Expected one bounded regular source file")
    data = path.read_bytes()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("Source changed during its read")
    return data


def subscription_only_owned(data: bytes) -> bytes:
    needle = b'                options: dict[str, Any] = {"on_event": lambda event: self._native_event(agent_id, event)}'
    if data.count(needle) != 1 or b'options["subscription_only"] = True' in data:
        raise ValueError("Supervisor derivation does not match the accepted one-site baseline")
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    addition = (newline + b'                if agent["mode"] != "native_attached":' + newline
                + b'                    options["subscription_only"] = True')
    return data.replace(needle, needle + addition, 1)


def subscription_only_claude_proxy(data: bytes) -> bytes:
    needle = b'super().__init__("claude", workspace, agent_id, token_file, on_event)'
    if data.count(needle) != 1:
        raise ValueError("Claude proxy derivation does not match its one-site baseline")
    return data.replace(needle, needle[:-1] + b", subscription_only=True)", 1)


def reparse_stat_test(data: bytes) -> bytes:
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    before = '''    def test_linked_or_reparse_ancestor_rejected_before_store_construction(self):
        self.enable()
        parent = self.root.parent
        original = BusStore._reparse
        with patch.object(BusStore, "_reparse", side_effect=lambda path: path == parent or original(path)), self.assertRaises(BusError):
            self.hook()
'''.encode().replace(b"\n", newline)
    after = '''    def test_linked_or_reparse_ancestor_rejected_before_store_construction(self):
        from types import SimpleNamespace
        self.enable()
        parent = self.root.parent
        original = Path.lstat
        def reparse_stat(path):
            info = original(path)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if path == parent else info
        with patch.object(Path, "lstat", reparse_stat), patch.object(auto_enroll, "BusStore", wraps=BusStore) as constructor:
            with self.assertRaises(BusError):
                self.hook()
            constructor.assert_not_called()
'''.encode().replace(b"\n", newline)
    if data.count(before) != 1:
        raise ValueError("Reparse test derivation no longer matches its single approved baseline case")
    return data.replace(before, after, 1)


def collect(freeze: Path, expected_sha256: str, roots: dict[str, Path], destination: Path, *, execute: bool) -> dict:
    from check_release import scan_bytes

    raw = read_regular(freeze.parent, freeze.name, limit=256 * 1024)
    if digest(raw) != expected_sha256 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Freeze bytes do not match the trusted selection hash")
    specification = json.loads(raw)
    if specification.get("schema") != "sharebus-source-freeze/1" or specification.get("approved") is not True:
        raise ValueError("An approved exact source freeze is required")
    entries = specification.get("files", [])
    if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
        raise ValueError("Source freeze must contain a bounded finite file list")
    allowed = set(json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))["files"])
    destination = destination.absolute()
    if not destination.is_relative_to(ROOT) or destination == ROOT or destination.exists():
        raise ValueError("Choose a fresh candidate directory inside this preparation root")
    for ancestor in destination.parents:
        if ancestor.exists() and (ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Candidate ancestor is linked")
    prepared = []
    names = set()
    total = 0
    for entry in entries:
        name = entry["destination"]
        relative(name)
        if name not in allowed or name.casefold() in names:
            raise ValueError("Unexpected or duplicate public destination")
        names.add(name.casefold())
        slot = entry["source_slot"]
        if slot not in roots or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", slot):
            raise ValueError("Missing reviewed source slot")
        data = read_regular(roots[slot], entry["source"])
        original_sha = digest(data)
        if original_sha != entry.get("sha256"):
            raise ValueError(f"Source hash changed: {name}")
        if name.startswith("outputs/sharebus/") and name.rsplit("/", 1)[-1] in CORE_PINS:
            if slot != "accepted-core" or original_sha != CORE_PINS[name.rsplit("/", 1)[-1]]:
                raise ValueError("Current in-progress core cannot replace the accepted standalone baseline")
        transform = entry.get("transform", "identity")
        if name == "outputs/sharebus/supervisor.py" and transform != "subscription-only-owned-v1":
            raise ValueError("Managed subscription guard must be explicit")
        if name == "outputs/sharebus/native_bridge.py" and transform != "subscription-only-claude-proxy-v1":
            raise ValueError("Claude proxy subscription guard must be explicit")
        if transform == "subscription-only-owned-v1" and name == "outputs/sharebus/supervisor.py":
            data = subscription_only_owned(data)
        elif transform == "subscription-only-claude-proxy-v1" and name == "outputs/sharebus/native_bridge.py":
            data = subscription_only_claude_proxy(data)
        elif transform == "reparse-stat-test-v1" and name == "outputs/sharebus/tests/test_auto_enroll.py":
            data = reparse_stat_test(data)
        elif transform != "identity":
            raise ValueError("Unreviewed source transformation")
        findings = scan_bytes(name, data)
        if findings:
            raise ValueError("Public input requires privacy review: " + "; ".join(findings))
        total += len(data)
        if total > 32 * 1024 * 1024:
            raise ValueError("Source export exceeds the bounded size budget")
        prepared.append((name, data, {"path": name, "sha256": digest(data), "size": len(data),
                                    "original_sha256": original_sha, "source_class": slot,
                                    "transform": transform}))
    manifest = {"schema": "sharebus-source-manifest/1", "selection_sha256": expected_sha256,
                "standalone_core": "accepted-pre-authority-with-subscription-guard",
                "complete": specification.get("complete") is True,
                "files": [item[2] for item in prepared]}
    if execute:
        destination.mkdir(parents=True, exist_ok=False)
        for name, data, _ in prepared:
            target = destination / relative(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(data)
        encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        (destination / "SOURCE-MANIFEST.json").write_bytes(encoded)
        (destination / "SOURCE-MANIFEST.sha256").write_text(digest(encoded) + "\n", encoding="ascii")
    return {"files": len(prepared), "bytes": total, "written": execute,
            "complete": manifest["complete"], "selection_sha256": expected_sha256}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--freeze-sha256", required=True)
    parser.add_argument("--root", action="append", default=[], help="Reviewed slot=local-root mapping; never exported")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    roots = {}
    for item in args.root:
        slot, separator, value = item.partition("=")
        if not separator or slot in roots:
            raise ValueError("Root slots must be unique slot=path pairs")
        roots[slot] = Path(value)
    print(json.dumps(collect(args.freeze, args.freeze_sha256, roots, args.destination, execute=args.execute), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
