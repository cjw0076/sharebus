"""Fail closed until the reviewed build/release prerequisites are recorded."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_GATES = {
    'build_gates': {
        'portable_backend_launcher_integrated',
        'bundled_mcp_and_guard_paths_verified',
        'relocated_native_extension_imports_verified',
        'runtime_archives_and_licenses_frozen',
        'per_platform_python_hash_locks_frozen',
        'reviewed_source_and_asset_allowlist_frozen',
    },
    'release_gates': {
        'version_and_source_revision_frozen',
        'existing_claude_codex_roundtrip_verified',
        'antigravity_exact_session_verified',
        'human_ingress_and_harmonize_verified',
        'supported_platform_package_smoke_verified',
        'public_source_licenses_and_notices_reviewed',
        'usefulness_trial_and_limitations_published',
        'signature_status_and_install_flow_documented',
    },
    'stable_release_gates': {
        'windows_publisher_signature_verified',
        'macos_developer_id_and_notarization_verified',
    },
}


def check(mode):
    policy = json.loads((ROOT / "release-policy.json").read_text(encoding="utf-8"))
    if not isinstance(policy, dict) or policy.get('schema') != 'avalokita-release-policy/1':
        raise ValueError('Unsupported release policy schema')
    for group_name, required in REQUIRED_GATES.items():
        group = policy.get(group_name)
        if not isinstance(group, dict) or set(group) != required:
            raise ValueError('Invalid required gate keys: ' + group_name)
        if any(type(value) is not bool for value in group.values()):
            raise ValueError('Gate values must be booleans: ' + group_name)
    if type(policy.get('public_publish')) is not bool:
        raise ValueError('public_publish must be a boolean')
    version = policy["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", version):
        raise ValueError("Version must be a pinned SemVer")
    channel = version.split('-', 1)[1].split('.')[0] if '-' in version else 'stable'
    if policy['channel'] != channel:
        raise ValueError('Release channel does not match the version')
    for name in ("package.json", "package-lock.json", "outputs/avalokita/app/package.json", "outputs/avalokita/app/package-lock.json"):
        package = json.loads((ROOT / name).read_text(encoding="utf-8"))
        if package["version"] != version or ("packages" in package and package["packages"][""]["version"] != version):
            raise ValueError(f"Version drift: {name}")
    if mode in {"build", "publish"}:
        missing = [name for name, value in policy["build_gates"].items() if value is not True]
        if missing:
            raise ValueError("Build contract is not integrated: " + ", ".join(missing))
    if mode == "publish":
        missing = [name for name, value in policy["release_gates"].items() if value is not True]
        if missing or policy["public_publish"] is not True or policy["version_status"] != "frozen":
            raise ValueError("Release evidence is incomplete: " + ", ".join(missing))
        statuses = policy['signing']['platform_status']
        if set(statuses) != {item['id'] for item in policy['platforms']} or any(value not in {'unsigned', 'ad_hoc', 'signed', 'signed_notarized'} for value in statuses.values()):
            raise ValueError('Every platform must disclose its observed signature status')
        if channel == 'stable':
            missing = [name for name, value in policy['stable_release_gates'].items() if value is not True]
            if missing or statuses['win-x64'] != 'signed' or any(statuses[name] != 'signed_notarized' for name in ('mac-arm64', 'mac-x64')):
                raise ValueError('Signed stable evidence is incomplete: ' + ', '.join(missing))
        elif not policy['signing']['prerelease_allows_unsigned'] and any(value in {'unsigned', 'ad_hoc'} for value in statuses.values()):
            raise ValueError('This prerelease policy does not permit unsigned packages')
        tag = 'v' + version
        if (os.environ.get('GITHUB_REF_TYPE') != 'tag'
                or os.environ.get('GITHUB_REF') != 'refs/tags/' + tag
                or os.environ.get('GITHUB_REF_NAME') != tag):
            raise ValueError('The triggering ref does not match the frozen release tag')
        tagged_commit = subprocess.check_output(['git', 'rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}'], cwd=ROOT, text=True).strip()
        head = subprocess.check_output(['git', 'rev-parse', '--verify', 'HEAD'], cwd=ROOT, text=True).strip()
        if not re.fullmatch(r'[0-9a-f]{40}', tagged_commit) or tagged_commit != head:
            raise ValueError('The triggering tag does not resolve to the checked-out source commit')
    return {"ok": True, "version": version, "mode": mode, "native_inputs": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["validate", "build", "publish"], default="validate")
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.mode)))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
