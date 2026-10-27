"""Inventory local CPython/Tk and an installed MCP dependency closure; copy nothing."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import distributions
import json
import os
from pathlib import Path
import re

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from collect_release import digest, read_regular


def inventory(python_root: Path, site: Path, *, dependencies_only: bool = False, checkpoint: Path | None = None) -> dict:
    installed = {canonicalize_name(item.metadata["Name"]): item for item in distributions(path=[str(site)])}
    pending = [Requirement("mcp==1.30.0"), Requirement("pydantic>=2.11,<3")]
    extras = {}
    closure = {}
    environment = default_environment()
    environment.update({"python_version": "3.11", "python_full_version": "3.11.9",
                        "sys_platform": "win32", "platform_system": "Windows", "extra": ""})
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        distribution = installed.get(name)
        if distribution is None or distribution.version not in requirement.specifier:
            raise ValueError("Installed dependency does not satisfy: " + name)
        requested = set(requirement.extras)
        if name in closure and requested <= extras[name]:
            continue
        closure[name] = distribution
        extras.setdefault(name, set()).update(requested)
        for value in distribution.requires or []:
            dependency = Requirement(value)
            if dependency.marker is None or any(dependency.marker.evaluate({**environment, "extra": extra})
                                                for extra in extras[name] | {""}):
                pending.append(dependency)
    if dependencies_only:
        return {"schema": "sharebus-installed-dependencies/1",
                "dependencies": [{"name": name, "version": distribution.version} for name, distribution in sorted(closure.items())]}
    entries = []
    pending_files = []
    packages = []
    print(json.dumps({"phase": "dependency-closure", "packages": len(closure)}), flush=True)

    def add(slot, name, destination):
        pending_files.append((slot, name, destination))

    for name in ("python.exe", "pythonw.exe", "python311.dll", "python3.dll", "LICENSE.txt"):
        add("python", name, "python/" + name)
    for path in sorted(python_root.glob("vcruntime*.dll")):
        add("python", path.name, "python/" + path.name)
    excluded = {"site-packages", "__pycache__", "test", "tests", "idlelib", "turtledemo", "ensurepip"}
    for directory in ("DLLs", "Lib", "tcl"):
        for current, directories, filenames in os.walk(python_root / directory, topdown=True, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in excluded)
            for name in directories:
                path = Path(current) / name
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError("Linked Python material is not accepted")
            for name in sorted(filenames):
                path = Path(current) / name
                if path.suffix in {".pyc", ".pyo"} or name in {"sitecustomize.py", "usercustomize.py"}:
                    continue
                relative = path.relative_to(python_root).as_posix()
                add("python", relative, "python/" + relative)
        print(json.dumps({"phase": "python-component-plan", "component": directory, "files": len(pending_files)}), flush=True)
    for name, distribution in sorted(closure.items()):
        selected = []
        for item in distribution.files or []:
            relative = item.as_posix()
            if (".." in item.parts or item.suffix in {".pyc", ".pyo"}
                    or item.name in {"direct_url.json", "INSTALLER", "REQUESTED"}):
                continue
            if item.suffix == ".pth" and item.name != "pywin32.pth":
                raise ValueError("Unreviewed dependency startup hook: " + item.name)
            add("site", relative, "python/Lib/site-packages/" + relative)
            selected.append(relative)
        licenses = [item for item in selected if re.search(r"(?:license|copying|copyright)", Path(item).name, re.I)]
        if not licenses:
            raise ValueError("Dependency has no preserved license file: " + name)
        packages.append({"name": name, "version": distribution.version,
                         "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "See preserved notice",
                         "license_files": licenses})
    paths = [item[2].casefold() for item in pending_files]
    if len(set(paths)) != len(paths) or len(paths) > 12000:
        raise ValueError("Duplicate or excessive runtime input list")
    required = {"python/pythonw.exe", "python/DLLs/_tkinter.pyd", "python/Lib/tkinter/__init__.py", "python/tcl/tk8.6/license.terms"}
    if not required <= {item[2] for item in pending_files}:
        raise ValueError("Full local Tk runtime and license are required")
    def inspect(item):
        slot, name, destination = item
        data = read_regular(python_root if slot == "python" else site, name, limit=64 * 1024 * 1024)
        return {"source_slot": slot, "source": name, "destination": destination,
                "sha256": digest(data), "size": len(data)}
    stream = checkpoint.open("x", encoding="utf-8") if checkpoint else None
    try:
        if stream:
            stream.write(json.dumps({"schema": "sharebus-runtime-checkpoint/1", "approved": False}) + "\n")
        with ThreadPoolExecutor(max_workers=4) as pool:
            for entry in pool.map(inspect, sorted(pending_files, key=lambda item: item[2])):
                entries.append(entry)
                if stream:
                    stream.write(json.dumps(entry, sort_keys=True) + "\n")
                if len(entries) % 500 == 0:
                    if stream:
                        stream.flush()
                    print(json.dumps({"phase": "hash-material", "files": len(entries), "io_threads": 4}), flush=True)
    finally:
        if stream:
            stream.close()
    return {"schema": "sharebus-windows-material/1", "approved": False,
            "python_version": "3.11.9", "provider": "local-cpython-full-install",
            "dependencies": packages, "files": entries,
            "notes": "Proposed finite inventory only; approval and isolated runtime checks still required"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependencies-only", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Inventory output exists; preserve it")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint = None if args.dependencies_only else args.output.with_suffix(".progress.jsonl")
    result = inventory(args.python_root, args.site_packages, dependencies_only=args.dependencies_only, checkpoint=checkpoint)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"files": len(result.get("files", [])), "dependencies": len(result["dependencies"]),
                      "copied": 0, "approved": False, "sha256": digest(args.output.read_bytes())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
