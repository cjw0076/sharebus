"""Shipped MCP/ACP imports, actual backend lifecycle and empty-profile GUI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

from assemble_runtime import probe_runtime

ROOT = Path(__file__).resolve().parents[1]


def backend_smoke(resources, temporary, environment, source_root=ROOT):
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, url):
            return None

    workspace, profile = temporary / 'backend-workspace', temporary / 'backend-profile'
    workspace.mkdir()
    profile.mkdir()
    metadata = json.loads((resources / 'PYTHON-RUNTIME.json').read_text(encoding='utf-8'))
    relative = Path(metadata['executable'])
    if (metadata.get('schema') != 'avalokita-python-runtime/1' or relative.is_absolute()
            or '..' in relative.parts or '\\' in metadata['executable']):
        raise ValueError('Invalid packaged interpreter metadata')
    python = (resources / 'python' / relative).resolve(strict=True)
    if not python.is_relative_to((resources / 'python').resolve()):
        raise ValueError('Packaged interpreter escaped its bundle')
    node = shutil.which('node')
    if not node:
        raise ValueError('Node is required to reuse the desktop environment contract')
    contract = source_root / 'outputs/avalokita/desktop/packaged-runtime.cjs'
    code = ('const c=require(process.argv[1]);const r=c.packagedRuntime(process.argv[2]);'
            'process.stdout.write(JSON.stringify(c.packagedBackendEnvironment(r,process.argv[3],process.env)));')
    derived = subprocess.run([node, '-e', code, str(contract), str(resources), str(profile)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=15, check=True)
    packaged_environment = json.loads(derived.stdout)
    versions = probe_runtime(python, environment=packaged_environment, cwd=resources / 'avalokita')
    if versions != {'python': metadata['version'], 'mcp': '1.30.0', 'acp': '0.12.1'}:
        raise ValueError('Shipped MCP/ACP versions differ from their locks')
    ready_file, nonce = temporary / 'backend-ready.json', secrets.token_hex(16)
    process = None
    selected = {'fastmcp_import_verified': True, 'acp_import_verified': True, 'versions': versions,
                'health_status': None, 'index_status': None, 'owner_shutdown_status': None,
                'forced_cleanup': False, 'native_inputs': 0}
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        # Match Electron's argv/cwd/env; no --assets workaround and no global Popen patch.
        process = subprocess.Popen([str(python), '-B', str(resources / 'avalokita/server.py'),
            '--workspace', str(workspace), '--host', '127.0.0.1', '--port', '0',
            '--ready-file', str(ready_file), '--instance-id', nonce],
            cwd=resources / 'avalokita', env=packaged_environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        ready = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise ValueError('Packaged backend exited before becoming ready')
            if ready_file.exists():
                info = ready_file.lstat()
                if ready_file.is_symlink() or info.st_nlink != 1 or info.st_size > 65536:
                    raise ValueError('Invalid owned backend ready file')
                try:
                    candidate = json.loads(ready_file.read_text(encoding='utf-8'))
                except json.JSONDecodeError:
                    time.sleep(0.05)
                    continue
                if (candidate.get('pid') != process.pid or candidate.get('instance_id') != nonce
                        or not Path(candidate.get('workspace', '')).samefile(workspace)):
                    raise ValueError('Packaged backend identity mismatch')
                address = urlsplit(candidate.get('url', ''))
                if (address.scheme != 'http' or address.hostname != '127.0.0.1' or not address.port
                        or address.username or address.password or address.path or address.query or address.fragment):
                    raise ValueError('Packaged backend must use its exact loopback URL')
                ready = candidate
                break
            time.sleep(0.05)
        if ready is None:
            raise ValueError('Packaged backend did not become ready')
        with opener.open(ready['url'] + '/health', timeout=5) as response:
            selected['health_status'] = response.status
            health = json.loads(response.read(65537))
        if (health.get('ok') is not True or health.get('pid') != process.pid
                or health.get('instance_id') != nonce or not Path(health.get('workspace', '')).samefile(workspace)):
            raise ValueError('Packaged health did not identify this exact backend')
        with opener.open(ready['url'] + '/', timeout=5) as response:
            selected['index_status'] = response.status
            if b'id="root"' not in response.read(256 * 1024):
                raise ValueError('Packaged frontend index is unavailable')
        request = Request(ready['url'] + '/api/owner/shutdown', method='POST',
            headers={'Content-Type': 'application/json', 'X-Avalokita-Request': '1',
                     'X-Avalokita-Instance': nonce, 'Authorization': 'Bearer ' + ready['owner_token']},
            data=json.dumps({'contract': 'avalokita-owner-shutdown/1', 'instance_id': nonce,
                             'pid': process.pid, 'workspace': str(workspace)}).encode('utf-8'))
        with opener.open(request, timeout=5) as response:
            selected['owner_shutdown_status'] = response.status
        process.wait(timeout=8)
        if process.returncode != 0 or selected['owner_shutdown_status'] != 202 or ready_file.exists():
            raise ValueError('Packaged backend did not finish owned shutdown cleanly')
    finally:
        if process is not None:
            if process.poll() is None:
                selected['forced_cleanup'] = True
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            selected.update(child_terminal=process.poll() is not None, child_exit=process.returncode)
    return selected


def main(platform_id):
    patterns = {'win32': 'win-unpacked/Avalokita.exe', 'darwin': 'mac*/Avalokita.app/Contents/MacOS/Avalokita', 'linux': 'linux-unpacked/avalokita'}
    files = list((ROOT / 'release').glob(patterns[sys.platform]))
    if len(files) != 1:
        raise ValueError('Expected exactly one owned unpacked platform application')
    with tempfile.TemporaryDirectory(prefix='avalokita-ci-smoke-') as temporary:
        temporary = Path(temporary)
        workspace = temporary / 'workspace'
        workspace.mkdir()
        receipt = temporary / 'smoke.json'
        home, scratch = temporary / 'home', temporary / 'tmp'
        home.mkdir()
        scratch.mkdir()
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL', 'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS'}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        environment.update(PATH='', HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'roaming'),
            LOCALAPPDATA=str(home / 'local'), XDG_CONFIG_HOME=str(home / 'config'), XDG_DATA_HOME=str(home / 'data'),
            TEMP=str(scratch), TMP=str(scratch), PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
        resources = files[0].parent.parent / 'Resources' if sys.platform == 'darwin' else files[0].parent / 'resources'
        backend = backend_smoke(resources, temporary, environment)
        # CI runners contain no native logged-in clients. The fixture opens an
        # empty folder, renders and saves UI state, then closes its own process.
        result = subprocess.run([str(files[0]), '--smoke', '--workspace', str(workspace), '--ui-test-profile', str(temporary / 'profile'), '--smoke-receipt', str(receipt)], env=environment, timeout=90, check=False)
        data = json.loads(receipt.read_text(encoding='utf-8'))
        if result.returncode != 0 or data.get('ok') is not True or data.get('rendererLoaded') is not True:
            raise ValueError('Packaged application smoke failed')
        destination = ROOT / f'release/GUI-SMOKE-{platform_id}.json'
        destination.write_text(json.dumps({'schema': 'avalokita-package-smoke/2', 'ok': True, 'platform': sys.platform,
            'backend': backend, 'gui_executed': True, 'native_inputs': 0,
            'original_session_delivery_verified': False}, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=['win-x64', 'mac-arm64', 'mac-x64', 'linux-x64'])
    main(parser.parse_args().platform)
