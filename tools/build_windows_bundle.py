"""Build a fresh Windows portable bundle from approved finite local material."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import zipfile

from collect_release import ROOT, digest, read_regular, relative
from check_release import check_tree


def build(source: Path, material_path: Path, expected: str, roots: dict[str, Path], output: Path, *, private_candidate: bool = False) -> dict:
    if os.name != "nt":
        raise ValueError("Build and verify the Windows bundle on Windows")
    source_result = check_tree(source)
    data = read_regular(material_path.parent, material_path.name, limit=8 * 1024 * 1024)
    material = json.loads(data)
    if (digest(data) != expected or (material.get("approved") is not True and not private_candidate)
            or material.get("schema") != "sharebus-windows-material/1"):
        raise ValueError("A reviewed exact local runtime material lock is required")
    output = output.absolute()
    if not output.is_relative_to(ROOT) or output.exists() or output == ROOT:
        raise ValueError("Choose a fresh bundle directory inside this preparation tree")
    for ancestor in output.parents:
        if ancestor.exists() and (ancestor.is_symlink() or getattr(ancestor.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Linked bundle output rejected")
    files = []
    destinations = set()
    total = 0
    for item in material["files"]:
        name = item["destination"]
        relative(name)
        if not name.startswith("python/") or name.casefold() in destinations or item["source_slot"] not in roots:
            raise ValueError("Unexpected runtime destination or source slot")
        destinations.add(name.casefold())
    def prepare(item):
        name = item["destination"]
        content = read_regular(roots[item["source_slot"]], item["source"], limit=64 * 1024 * 1024)
        if digest(content) != item["sha256"] or len(content) != item["size"]:
            raise ValueError("Frozen runtime material changed: " + name)
        for root in roots.values():
            if any(prefix in content for prefix in (str(root).encode(), str(root).encode("utf-16-le"))):
                raise ValueError("Material contains a local input path: " + name)
        return name, content
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, content in pool.map(prepare, sorted(material["files"], key=lambda item: item["destination"])):
            total += len(content)
            if total > 300 * 1024 * 1024:
                raise ValueError("Runtime material exceeds the size budget")
            files.append((name, content))
    manifest = json.loads((source / "SOURCE-MANIFEST.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"].startswith("outputs/sharebus/") and not entry["path"].startswith("outputs/sharebus/tests/"):
            name = "sharebus/" + entry["path"][len("outputs/sharebus/"):]
            files.append((name, read_regular(source, entry["path"])))
    if not any(name == "sharebus/launcher.py" for name, _ in files):
        raise ValueError("Final reviewed GUI launcher is not collected yet")
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise ValueError("The local Windows compiler is unavailable; no runtime download was attempted")
    output.mkdir(parents=True, exist_ok=False)
    for name, content in files:
        target = output / relative(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (output / "python/python311._pth").write_text(".\nDLLs\nLib\nLib/site-packages\n../sharebus\nimport site\n", encoding="ascii")
    (output / "PYTHON-RUNTIME.json").write_text(json.dumps({"schema": "avalokita-python-runtime/1", "product": "ShareBus",
        "executable": "python.exe", "version": material["python_version"], "material_sha256": expected}) + "\n", encoding="utf-8")
    bootstrap = read_regular(ROOT, "packaging/ShareBusBootstrap.cs")
    compiler_input = output / "ShareBusBootstrap.cs"
    compiler_input.write_bytes(bootstrap)
    result = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/debug-", "/optimize+",
                             "/reference:System.Windows.Forms.dll", "/out:ShareBus.exe", "ShareBusBootstrap.cs"],
                            cwd=output, capture_output=True, timeout=30)
    (output.parent / (output.name + "-compiler.stdout.txt")).write_bytes(result.stdout)
    (output.parent / (output.name + "-compiler.stderr.txt")).write_bytes(result.stderr)
    if result.returncode:
        raise ValueError("Windowless bootstrap compile failed; raw output was preserved")
    (output / "LICENSE").write_bytes(read_regular(source, "LICENSE"))
    (output / "RUNTIME-LICENSES.json").write_text(json.dumps(material["dependencies"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    public_files = [{"path": path.relative_to(output).as_posix(), "sha256": digest(path.read_bytes()), "size": path.stat().st_size}
                    for path in sorted(output.rglob("*")) if path.is_file()]
    bundle_manifest = {"schema": "sharebus-windows-bundle/1", "version": "0.1.0a1", "source": source_result,
                       "private_candidate_only": private_candidate,
                       "source_manifest_sha256": digest((source / "SOURCE-MANIFEST.json").read_bytes()),
                       "runtime_material_sha256": expected, "files": public_files}
    (output / "BUNDLE-MANIFEST.json").write_text(json.dumps(bundle_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    archive_path = output.parent / (output.name + ".zip")
    if archive_path.exists():
        raise ValueError("Archive exists; preserve it")
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                item = zipfile.ZipInfo(output.name + "/" + path.relative_to(output).as_posix(), (2026, 10, 27, 0, 0, 0))
                item.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(item, path.read_bytes())
    return {"archive": archive_path.name, "sha256": digest(archive_path.read_bytes()), "files": len(public_files),
            "copied_runtime_bytes": total, "gui_launch_tested": False, "native_inputs": 0,
            "private_candidate_only": private_candidate}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--material", type=Path, required=True)
    parser.add_argument("--material-sha256", required=True)
    parser.add_argument("--python-root", type=Path, required=True)
    parser.add_argument("--site-packages", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-candidate", action="store_true", help="Local build only; independent runtime/release review stays pending")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.material, args.material_sha256,
                           {"python": args.python_root, "site": args.site_packages}, args.output,
                           private_candidate=args.private_candidate), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
