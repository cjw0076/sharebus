"""Record source/tool/lock provenance before packaging and checksums afterward."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from verify_reason_source import verify

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(final=False, platform_id=None):
    policy = json.loads((ROOT / 'release-policy.json').read_text(encoding='utf-8'))
    if final:
        if platform_id not in {item['id'] for item in policy['platforms']}:
            raise ValueError('Final provenance requires a supported platform ID')
        directory = ROOT / 'release'
        files = sorted(path for path in directory.iterdir() if path.is_file() and path.name.endswith(('.exe', '.dmg', '.zip', '.AppImage', '.tar.gz')))
        if not files:
            raise ValueError('No platform packages were produced')
        (directory / f'SHA256SUMS-{platform_id}').write_text(''.join(f'{sha(path)}  {path.name}\n' for path in files), encoding='ascii')
        info = json.loads((ROOT / 'build/runtime/BUILD-INFO.json').read_text(encoding='utf-8'))
        info['artifacts'] = [{'name': path.name, 'sha256': sha(path), 'bytes': path.stat().st_size} for path in files]
        info['platform'] = platform_id
        (directory / f'BUILD-PROVENANCE-{platform_id}.json').write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')
    else:
        directory = ROOT / 'build/runtime'
        reason_source = verify(ROOT)
        info = {
            'schema': 'avalokita-build/1', 'version': policy['version'],
            'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'reason_upstream_revision': policy['reason']['revision'],
            'reason_fork_revision': reason_source['fork_revision'],
            'reason_fork_tree': reason_source['fork_tree'],
            'reason_source_lock_sha256': reason_source['source_lock_sha256'],
            'reason_patches': policy['reason']['patches'],
            'declared_platform_signature_status': policy['signing']['platform_status'],
            'python': json.loads((directory / 'PYTHON-RUNTIME.json').read_text(encoding='utf-8')),
            'tools': {'python': sys.version.split()[0], 'node': subprocess.check_output(['node', '--version'], text=True).strip(), 'rust': subprocess.check_output(['rustc', '--version'], text=True).strip()},
            'locks': {name: sha(ROOT / name) for name in ('package-lock.json', 'outputs/avalokita/app/package-lock.json', 'vendor/reason-network/Cargo.lock', 'packaging/python-runtime-lock.json', 'packaging/reason-source-lock.json')},
            'native_delivery_verified_by_build': False,
        }
        (directory / 'BUILD-INFO.json').write_text(json.dumps(info, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--final', action='store_true')
    parser.add_argument('--platform')
    args = parser.parse_args()
    main(args.final, args.platform)
