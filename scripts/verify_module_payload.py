"""Check literal relative JavaScript module references without loading app code."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
RELATIVE_MODULE = re.compile(r'''(?:\brequire(?:\.resolve)?\s*\(\s*|\bfrom\s+|\bimport\s*(?:\(\s*)?)(['"])(\.[^'"\r\n]+)\1''')
EXTENSIONS = ('.js', '.cjs', '.mjs', '.json', '.node', '.ts', '.tsx')


def verify_javascript_modules(payload):
    checked = references = 0
    for name, data in payload.items():
        if PurePosixPath(name).suffix not in {'.js', '.cjs', '.mjs', '.ts', '.tsx'}:
            continue
        checked += 1
        for _, module in RELATIVE_MODULE.findall(data.decode('utf-8')):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), module))
            candidates = [target, *(target + extension for extension in EXTENSIONS),
                          *(target + '/index' + extension for extension in EXTENSIONS)]
            if not any(candidate in payload for candidate in candidates):
                raise ValueError('Missing literal relative module: ' + name + ' -> ' + module)
            references += 1
    return {'files': checked, 'literal_relative_modules': references}


def verify(root=ROOT):
    manifest = json.loads((root / 'SOURCE-MANIFEST.json').read_text(encoding='utf-8'))
    payload = {}
    for entry in manifest['files']:
        name = PurePosixPath(entry['path'])
        if name.is_absolute() or '..' in name.parts or '\\' in entry['path']:
            raise ValueError('Source manifest path escaped its export')
        data = (root / entry['path']).read_bytes()
        if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Source manifest bytes changed: ' + entry['path'])
        payload[entry['path']] = data
    source = verify_javascript_modules(payload)
    result = subprocess.check_output(['node', '-e', 'process.stdout.write(JSON.stringify(require(process.argv[1]).files))',
        str((root / 'packaging/electron-builder.cjs').resolve())], text=True)
    names = json.loads(result)
    if not isinstance(names, list) or any(not isinstance(name, str) or name not in payload for name in names):
        raise ValueError('Packaged JavaScript files must be explicit exported source paths')
    packaged = verify_javascript_modules({name: payload[name] for name in names})
    return {'ok': True, 'source': source, 'packaged': packaged, 'native_inputs': 0}


if __name__ == '__main__':
    try:
        print(json.dumps(verify()))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
