"""Verify the already patched vendored source before any Cargo build."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import re

ROOT = Path(__file__).resolve().parents[1]
RUST_TRIVIA = r'(?:\s|//[^\n]*(?:\n|$)|/\*.*?\*/)*'
RUST_TOKENS = re.compile(r'''//[^\n]*|/\*.*?\*/|'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\r\n])'|r(?P<hashes>\#*)".*?"(?P=hashes)|"(?:\\.|[^"\\])*"|(?P<include>\binclude(?:_bytes|_str)?\b)''', re.DOTALL)
RUST_INCLUDE = re.compile(RUST_TRIVIA + r'!' + RUST_TRIVIA + r'([\(\[{])' + RUST_TRIVIA + r'(?:"([^"\\\r\n]*)"|r(?P<hashes>\#*)"(?P<raw>.*?)"(?P=hashes))' + RUST_TRIVIA + r',?' + RUST_TRIVIA + r'([\)\]}])', re.DOTALL)


def verify_rust_includes(files):
    """Check all cfg branches; unrecognized include syntax requires review."""
    references = []
    for name, data in sorted(files.items()):
        if not name.endswith('.rs'):
            continue
        source = data.decode('utf-8')
        for token in RUST_TOKENS.finditer(source):
            if token.group().startswith('/*') and '/*' in token.group()[2:-2]:
                raise ValueError('Nested Rust comments require an explicit include review: ' + name)
            if not token.group('include'):
                continue
            argument = RUST_INCLUDE.match(source, token.end())
            if not argument or (argument.group(1), argument.group(5)) not in {('(', ')'), ('[', ']'), ('{', '}')}:
                raise ValueError('Nonliteral Rust include requires a reviewed generator: ' + name)
            value = argument.group(2) if argument.group(2) is not None else argument.group('raw')
            if not value or '\\' in value or ':' in value or PurePosixPath(value).is_absolute():
                raise ValueError('Rust include path escaped its vendored source: ' + name)
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), value))
            if target == '..' or target.startswith('../'):
                raise ValueError('Rust include path escaped its vendored source: ' + name)
            if target not in files:
                raise ValueError('Missing literal Rust include: ' + name + ' -> ' + target)
            references.append({'source': name, 'target': target})
    return {'literal_includes': len(references), 'references': references, 'nonliteral_includes': 0,
            'generated_input_policy': 'reject-without-reviewed-generator', 'cfg_branches': 'all'}


def verify(root=ROOT):
    policy = json.loads((root / 'release-policy.json').read_text(encoding='utf-8'))['reason']
    lock_path = root / 'packaging/reason-source-lock.json'
    lock = json.loads(lock_path.read_text(encoding='utf-8'))
    if (lock.get('schema') != 'avalokita-reason-source/1'
            or lock.get('patch_application') != 'applied-once-during-source-export'
            or lock.get('upstream_revision') != policy['revision']
            or lock.get('fork_revision') != policy['fork_revision']
            or lock.get('fork_tree') != policy['fork_tree']
            or lock.get('patches') != policy['patches']):
        raise ValueError('Reason source lock does not match the frozen upstream/fork/patch policy')
    for patch in lock['patches']:
        path = Path(patch['path'])
        if path.is_absolute() or '..' in path.parts or not re.fullmatch(r'[0-9a-f]{64}', patch['sha256']):
            raise ValueError('Invalid pinned patch metadata')
        data = (root / path).read_bytes()
        if b'\r' in data or hashlib.sha256(data).hexdigest() != patch['sha256']:
            raise ValueError('Reason patch hash or LF format changed')
    vendor = root / 'vendor/reason-network'
    expected = set()
    payload = {}
    for entry in lock['files']:
        path = Path(entry['path'])
        if path.is_absolute() or '..' in path.parts or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
            raise ValueError('Invalid pinned source metadata')
        file = vendor / path
        if file.is_symlink():
            raise ValueError('Vendored Reason source is linked')
        data = file.read_bytes()
        if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Vendored Reason source differs from the frozen fork: ' + path.as_posix())
        if path.suffix in {'.rs', '.md', '.toml', '.lock'} and b'\r\n' in data:
            raise ValueError('Vendored Reason source was converted to CRLF')
        expected.add(path.as_posix())
        payload[path.as_posix()] = data
    actual = set()
    for file in vendor.rglob('*'):
        name = file.relative_to(vendor)
        if name.parts[0] in {'target', '.git'}:
            continue
        if file.is_symlink():
            raise ValueError('Vendored Reason tree contains a linked path')
        if file.is_file():
            actual.add(name.as_posix())
    if actual != expected:
        raise ValueError('Vendored Reason tree contains missing or unexpected source files')
    for name, digest in lock['protected_notices'].items():
        if hashlib.sha256((vendor / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Upstream Reason license or NOTICE changed')
    includes = verify_rust_includes(payload)
    return {'ok': True, 'source_files': len(expected), 'upstream_revision': lock['upstream_revision'],
            'fork_revision': lock['fork_revision'], 'fork_tree': lock['fork_tree'],
            'patches': lock['patches'], 'source_lock_sha256': hashlib.sha256(lock_path.read_bytes()).hexdigest(),
            'rust_includes': includes,
            'native_inputs': 0, 'native_roundtrip_verified': False}


if __name__ == '__main__':
    try:
        print(json.dumps(verify()))
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
