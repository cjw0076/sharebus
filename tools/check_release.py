"""Verify finite source manifests, preserved licenses and source archive privacy."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PINS = {
    "codex_bridge.py": "ac1a0e7dd29625f82f6fea33d331ec767d0c13bd27c6da87d0eb01c687c5daa7",
    "antigravity_bridge.py": "322b9686fb79cc0d9422a73292c2835998a236e260a1a37e1c00242f4a753916",
    "commands/sharebus/SKILL.md": "9c2f82314ec3213a7115d1be315425607ec6c96aeb73e9087f89c37e93094adb",
}
UPSTREAM = {
    "happy/LICENSE": "e251d0448ef3ce023c20ebac9b90a7d8642b1434825838247d6e457668eb3e00",
    "happy/session_hook_forwarder.cjs": "7bcf91ea96a9a2a443975d3fe0c2a3523fd35548f74df2b89e790b54f28c45f6",
    "agmsg/LICENSE": "61e0e4783f9f5ab48745db7a35673c66400312335499071d763d4651d9fee9b3",
    "agmsg/watch-once.sh": "d6b7e8b140699907f0d50c387b3dc7e572a4ac4666475f0bb35c3fce7a9b234a",
}
PRIVATE = {".sharebus", "private", "credentials", "evidence", ".omc", "__pycache__"}
MEDIA = {"codex-spritesheet.webp", "clawd-laptop.webm", "clawd-poster.png", "antigravity-official-mark.svg"}
PATTERNS = {
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "model-api-key": re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "literal-user-path": re.compile(r"(?:[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]|/(?:Users|home)/)[^\\/\s\"']+", re.I),
}


def scan_bytes(name: str, data: bytes) -> list[str]:
    findings = []
    path = PurePosixPath(name.replace("\\", "/"))
    if (path.is_absolute() or any(part in {"..", *PRIVATE} for part in path.parts)
            or path.name in MEDIA or path.suffix.lower() in {".token", ".sqlite3", ".sqlite", ".db", ".pyc"}):
        findings.append(f"forbidden public path: {name}")
    if b"\0" in data:
        if (name.endswith("web/assets/PretendardVariable.woff2")
                and hashlib.sha256(data).hexdigest() == "9599f12fd42fc0bce1cd50b47a0c022e108d7aa64dd0d1bb0ed44f3282d900b4"):
            return findings
        return [*findings, f"unexpected binary source: {name}"]
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return [*findings, f"non-UTF-8 source: {name}"]
    while "\\\\" in text:
        text = text.replace("\\\\", "\\")
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append(f"review {label}: {name}:{number}")
    return findings


def check_tree(root: Path, *, require_complete: bool = True) -> dict:
    from collect_release import CORE_PINS, digest, read_regular

    encoded = read_regular(root, "SOURCE-MANIFEST.json", limit=256 * 1024)
    if read_regular(root, "SOURCE-MANIFEST.sha256").decode().strip() != digest(encoded):
        raise ValueError("Source manifest checksum mismatch")
    manifest = json.loads(encoded)
    if manifest.get("schema") != "sharebus-source-manifest/1":
        raise ValueError("Unknown source manifest schema")
    if require_complete and manifest.get("complete") is not True:
        raise ValueError("Selected source tree is explicitly incomplete")
    entries = manifest.get("files", [])
    allowed = set(json.loads((ROOT / "release-files.json").read_text(encoding="utf-8"))["files"])
    expected = {entry["path"] for entry in entries}
    if len(expected) != len(entries) or not expected <= allowed:
        raise ValueError("Manifest contains duplicate or unselected files")
    errors = []
    for entry in entries:
        name = entry["path"]
        data = read_regular(root, name)
        if digest(data) != entry["sha256"] or len(data) != entry["size"]:
            errors.append(f"selected byte mismatch: {name}")
        errors.extend(scan_bytes(name, data))
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if name.split("/", 1)[0] in {"build", "dist", ".venv", ".wheelhouse", ".pytest_cache", ".git", "sharebus_local.egg-info"}:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            errors.append(f"linked public input: {name}")
        elif path.is_file() and name not in expected | {"SOURCE-MANIFEST.json", "SOURCE-MANIFEST.sha256"}:
            errors.append(f"unlisted public input: {name}")
    runtime = root / "outputs/sharebus"
    for name, sha in PINS.items():
        path = runtime / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            errors.append(f"accepted compact source mismatch: {name}")
    for name, sha in UPSTREAM.items():
        path = runtime / "third_party" / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            errors.append(f"upstream MIT source/license mismatch: {name}")
    by_name = {entry["path"]: entry for entry in entries}
    for name, original in CORE_PINS.items():
        entry = by_name.get("outputs/sharebus/" + name, {})
        if entry.get("original_sha256") != original or entry.get("source_class") != "accepted-core":
            errors.append(f"standalone baseline provenance mismatch: {name}")
        if name == "supervisor.py" and entry.get("transform") != "subscription-only-owned-v1":
            errors.append("managed subscription guard derivation missing")
    initializer = by_name.get("outputs/sharebus/__init__.py", {})
    if initializer.get("transform") != "identity" or initializer.get("sha256") != initializer.get("original_sha256"):
        errors.append("original initializer compatibility bytes not preserved")
    if by_name.get("outputs/sharebus/native_bridge.py", {}).get("transform") != "subscription-only-claude-proxy-v1":
        errors.append("Claude delivery proxy subscription guard missing")
    character_manifest = runtime / "web/assets/characters/sharebus-original-artwork.json"
    if character_manifest.is_file():
        characters = json.loads(character_manifest.read_text(encoding="utf-8"))
        if any(asset.get("classification", "").startswith("official") for asset in characters.get("assets", [])):
            errors.append("public manifest includes official/app-extracted media")
    elif require_complete:
        errors.append("public character provenance missing")
    font_license = runtime / "web/assets/Pretendard-LICENSE.txt"
    if (not font_license.is_file() or hashlib.sha256(font_license.read_bytes()).hexdigest()
            != "8a2aba6114145294edbbce5998eab74320e7e07ce39999cc9f95ae2967d2eb9e"):
        errors.append("Pretendard OFL notice mismatch")
    if errors:
        raise ValueError("\n".join(errors))
    return {"files": len(entries), "source_pins": len(PINS), "upstream_hashes": len(UPSTREAM),
            "complete": manifest.get("complete") is True, "pattern_findings": 0,
            "manual_review_still_required": True}


def check_archive(path: Path) -> dict:
    entries = []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                if stat.S_ISLNK(item.external_attr >> 16) or item.file_size > 8 * 1024 * 1024:
                    raise ValueError("Unexpected link or large archive member")
                entries.append((item.filename, archive.read(item)))
    else:
        with tarfile.open(path, "r:gz") as archive:
            for item in archive.getmembers():
                if item.isdir():
                    continue
                if not item.isfile() or item.size > 8 * 1024 * 1024:
                    raise ValueError("Unexpected link or large archive member")
                stream = archive.extractfile(item)
                assert stream is not None
                entries.append((item.name, stream.read()))
    errors = [finding for name, data in entries for finding in scan_bytes(name, data)]
    if errors:
        raise ValueError("\n".join(errors))
    return {"archive": path.name, "members": len(entries), "pattern_findings": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--allow-incomplete", action="store_true", help="Preparation only; never publication approval")
    parser.add_argument("--archive", type=Path, action="append", default=[])
    args = parser.parse_args()
    try:
        result = check_tree(args.source_root, require_complete=not args.allow_incomplete)
        result["archives"] = [check_archive(path) for path in args.archive]
    except (ValueError, OSError, KeyError) as error:
        print(str(error))
        print("Release check failed; matched contents were not printed.")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
