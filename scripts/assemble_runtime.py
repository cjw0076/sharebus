"""Candidate assembly: no downloads/install/build occur without --execute."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def probe_runtime(executable, *, environment=None, cwd=None):
    """Use the shipped interpreter's exact isolated MCP/ACP import path."""
    code = """import importlib.metadata,json,re,sys
try:
    import ssl,sqlite3,acp
    from mcp.server.fastmcp import FastMCP
    from acp.schema import ClientCapabilities,EnvVariable,Implementation,McpServerStdio,RequestPermissionResponse
    from cryptography.hazmat.primitives import serialization
    if sys.platform=='win32':
        import win32api,win32file,win32pipe,pywintypes
    assert callable(acp.connect_to_agent)
    result={'ok':True,'python':sys.version.split()[0],'mcp':importlib.metadata.version('mcp'),'acp':importlib.metadata.version('agent-client-protocol')}
except Exception as error:
    result={'ok':False,'error_type':type(error).__name__}
    module=getattr(error,'name',None)
    if isinstance(module,str) and re.fullmatch(r'[A-Za-z0-9_.]{1,128}',module): result['module']=module
print(json.dumps(result))
raise SystemExit(0 if result['ok'] else 1)
"""
    try:
        result = subprocess.run([str(executable), "-B", "-I", "-c", code],
            env=environment, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", timeout=10, check=False)
        selected = json.loads(result.stdout) if len(result.stdout) <= 4096 else {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise ValueError("Bundled MCP/ACP import probe did not return within its startup contract") from None
    if result.returncode != 0 or selected.get("ok") is not True:
        category = selected.get("error_type", "runtime_start_failed")
        module = selected.get("module", "unknown_module")
        raise ValueError(f"Bundled MCP/ACP imports failed: {category} ({module})")
    return {key: selected[key] for key in ("python", "mcp", "acp")}


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Runtime metadata requires a relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Runtime metadata path escapes its bundle")
    return Path(*path.parts)


def validate_entry(entry):
    if entry.get("provider") not in {"cpython-embeddable", "astral-python-build-standalone"}:
        raise ValueError("Use an explicitly supported official Python runtime provider")
    url = urlsplit(entry.get("url") or "")
    if url.scheme != "https" or url.hostname not in {"www.python.org", "github.com"} or "/latest/" in url.path or url.username or url.password:
        raise ValueError("Freeze an HTTPS official Python/PBS release URL first")
    if not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256") or "") or not re.fullmatch(r"3\.(11|12|13|14)\.\d+", entry.get("version") or ""):
        raise ValueError("Freeze the Python archive SHA256 and supported exact version first")
    if entry.get("provider") == "astral-python-build-standalone" and (url.hostname != "github.com" or not url.path.startswith("/astral-sh/python-build-standalone/releases/download/")):
        raise ValueError("Use the pinned official Astral PBS release archive")
    if entry.get("provider") == "cpython-embeddable" and (url.hostname != "www.python.org" or not url.path.startswith("/ftp/python/" + entry["version"] + "/")):
        raise ValueError("Use the pinned official CPython release archive")
    relative(entry["executable"])
    relative(entry["site_packages"])
    if entry["archive"] not in {"zip", "tar.gz"}:
        raise ValueError("Unsupported runtime archive format")
    if entry["archive"] == "zip":
        relative(entry.get("pth"))
    if entry.get("strip_prefix"):
        relative(entry["strip_prefix"])


def extract(archive, destination, entry):
    destination.mkdir(exist_ok=False)
    if entry["archive"] == "zip":
        with zipfile.ZipFile(archive) as package:
            for item in package.infolist():
                relative(item.filename.rstrip("/"))
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("ZIP runtime cannot contain symbolic links")
            package.extractall(destination)
    else:
        with tarfile.open(archive, "r:gz") as package:
            # CPython's data filter checks link targets as well as entry names.
            package.extractall(destination, filter="data")


def assemble(platform_id, execute=False):
    policy = json.loads((ROOT / "release-policy.json").read_text(encoding="utf-8"))
    target_platform = next(item for item in policy["platforms"] if item["id"] == platform_id)
    entry = json.loads((ROOT / "packaging/python-runtime-lock.json").read_text(encoding="utf-8"))["platforms"][platform_id]
    validate_entry(entry)
    dependency_lock = ROOT / "packaging" / f"requirements-{platform_id}.lock"
    if not dependency_lock.is_file() or "--hash=sha256:" not in dependency_lock.read_text(encoding="utf-8"):
        raise ValueError("A reviewed per-platform Python dependency hash lock is required")
    machine = platform.machine().lower()
    expected_machine = {"x64": {"x86_64", "amd64"}, "arm64": {"aarch64", "arm64"}}
    if sys.platform != target_platform["os"] or machine not in expected_machine[target_platform["arch"]]:
        raise ValueError("Assemble on the matching native OS/CPU runner")
    if sys.version.split()[0].split('.')[:2] != entry["version"].split('.')[:2]:
        raise ValueError("The build interpreter ABI must match the bundled Python")
    if not execute:
        return {"ok": True, "validated": True, "downloaded": False, "installed": False}
    target = ROOT / "build/runtime/python"
    if target.exists():
        raise ValueError("Runtime target already exists; choose a fresh build workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="runtime-", dir=target.parent) as temporary:
        temporary = Path(temporary)
        archive = temporary / "runtime.archive"
        digest = hashlib.sha256()
        with urlopen(entry["url"], timeout=60) as response, archive.open("xb") as stream:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 180 * 1024 * 1024:
                    raise ValueError("Runtime archive exceeds the pinned size budget")
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != entry["sha256"]:
            raise ValueError("Runtime archive SHA256 mismatch")
        if entry.get("bytes") is not None and total != entry["bytes"]:
            raise ValueError("Runtime archive byte count differs from its lock")
        unpacked = temporary / "unpacked"
        extract(archive, unpacked, entry)
        source = unpacked / relative(entry["strip_prefix"]) if entry["strip_prefix"] else unpacked
        if not source.resolve().is_relative_to(unpacked.resolve()) or not target.resolve().is_relative_to((ROOT / "build/runtime").resolve()):
            raise ValueError("Runtime move escapes the build workspace")
        shutil.move(str(source), str(target))
    if entry["archive"] == "zip":
        pth = target / relative(entry["pth"])
        if not pth.is_file() or not re.fullmatch(r"python\d+\._pth", pth.name):
            raise ValueError("Expected official embeddable _pth file is missing")
        lines = pth.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if line.strip() not in {"#import site", "import site", entry["site_packages"]}]
        # _pth retains isolated search paths. Enable only the bundled package dir
        # and its pywin32 bootstrap .pth support, never a user's Python install.
        pth.write_text("\n".join([*lines, entry["site_packages"], "import site"]) + "\n", encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    environment.update(PIP_CONFIG_FILE=os.devnull, PIP_KEYRING_PROVIDER="disabled")
    subprocess.run([sys.executable, "-m", "pip", "install", "--isolated", "--index-url", "https://pypi.org/simple",
        "--require-hashes", "--only-binary=:all:", "--no-compile", "--target", str(target / relative(entry["site_packages"])), "-r", str(dependency_lock)], env=environment, check=True)
    executable = (target / relative(entry["executable"])).resolve(strict=True)
    if not executable.is_relative_to(target.resolve()):
        raise ValueError("Python executable resolves outside the bundle")
    probe_receipt = {"schema": "avalokita-runtime-imports/1", "platform": platform_id, "ok": False, "native_inputs": 0}
    try:
        versions = probe_runtime(executable)
        if versions != {"python": entry["version"], "mcp": "1.30.0", "acp": "0.12.1"}:
            raise ValueError("Bundled runtime import/version probe did not match its lock")
        probe_receipt.update(ok=True, versions=versions)
    except ValueError as error:
        probe_receipt["reason"] = str(error)
        raise
    finally:
        (target.parent / f"IMPORT-PROBE-{platform_id}.json").write_text(json.dumps(probe_receipt, indent=2) + "\n", encoding="utf-8")
    sdk = json.loads((ROOT / "packaging/acp-sdk-runtime.json").read_text(encoding="utf-8"))
    license_file = target / relative(entry["site_packages"]) / "agent_client_protocol-0.12.1.dist-info/licenses/LICENSE"
    if sdk.get("version") != "0.12.1" or hashlib.sha256(license_file.read_bytes()).hexdigest() != sdk.get("license_sha256"):
        raise ValueError("Bundled ACP SDK license differs from its reviewed manifest")
    metadata = {"schema": "avalokita-python-runtime/1", "executable": executable.relative_to(target.resolve()).as_posix(), "version": entry["version"], "archive_sha256": entry["sha256"], "provider": entry["provider"], "dependency_lock_sha256": hashlib.sha256(dependency_lock.read_bytes()).hexdigest(), "native_inputs": 0}
    metadata.update(dependency_versions=versions, fastmcp_import_verified=True,
                    acp_sdk_license_relative_to_python=license_file.relative_to(target).as_posix(),
                    acp_sdk_manifest_sha256=hashlib.sha256((ROOT / "packaging/acp-sdk-runtime.json").read_bytes()).hexdigest())
    (target.parent / "PYTHON-RUNTIME.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "platform": platform_id, "native_inputs": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(assemble(args.platform, args.execute)))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
