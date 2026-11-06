"""Copy only the freshly built pinned Reason binary and its upstream notices."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

from verify_reason_source import verify

ROOT = Path(__file__).resolve().parents[1]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    policy = json.loads((ROOT / 'release-policy.json').read_text(encoding='utf-8'))
    source_receipt = verify(ROOT)
    target = next(item for item in policy['platforms'] if item['id'] == args.platform)
    binary = 'reason-network.exe' if target['os'] == 'win32' else 'reason-network'
    source = ROOT / 'vendor/reason-network'
    built = source / 'target' / target['rust_target'] / 'release' / binary
    # Clap help exits before setup/daemon/native input initialization.
    probe = subprocess.run([str(built), 'avalokita-deliver', '--help'], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10, check=False)
    if probe.returncode != 0 or 'avalokita-deliver' not in probe.stdout:
        raise SystemExit('Built Reason binary lacks the pinned Avalokita command contract')
    destination = ROOT / 'build/runtime/reason'
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(built, destination / binary)
    for name in ('LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE', 'AVALOKITA-CHANGES.md'):
        shutil.copy2(source / name, destination / name)
    shutil.copy2(ROOT / 'packaging/reason-source-lock.json', destination / 'SOURCE-LOCK.json')
    print(json.dumps({**source_receipt, 'binary_sha256_before_signing': hashlib.sha256((destination / binary).read_bytes()).hexdigest(), 'native_contract_cli_present': True, 'native_inputs': 0}))
