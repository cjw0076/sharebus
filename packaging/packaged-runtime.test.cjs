'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { packagedRuntime } = require('./packaged-runtime.cjs');

test('platform resources use a real bundled interpreter and the existing source paths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'avalokita-release-fixture-'));
  try {
    for (const relative of ['python/python.exe', 'python/bin/python3.11', 'reason/reason-network.exe', 'reason/reason-network', 'avalokita/server.py', 'avalokita/app/dist/index.html']) {
      const file = path.join(root, relative);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, 'fixture');
    }
    for (const platform of ['win32', 'darwin', 'linux']) {
      fs.writeFileSync(path.join(root, 'PYTHON-RUNTIME.json'), JSON.stringify({ schema: 'avalokita-python-runtime/1', executable: platform === 'win32' ? 'python.exe' : 'bin/python3.11' }));
      const result = packagedRuntime(root, platform);
      assert.equal(path.basename(result.python), platform === 'win32' ? 'python.exe' : 'python3.11');
      assert.equal(result.server, path.join(root, 'avalokita/server.py'));
      assert.equal(result.assets, path.join(root, 'avalokita/app/dist'));
    }
    fs.writeFileSync(path.join(root, 'PYTHON-RUNTIME.json'), JSON.stringify({ schema: 'avalokita-python-runtime/1', executable: '../private/python.exe' }));
    assert.throws(() => packagedRuntime(root, 'win32'));
  } finally {
    const resolved = fs.realpathSync(root);
    const temporaryRoot = fs.realpathSync(os.tmpdir());
    assert.equal(path.dirname(resolved), temporaryRoot);
    assert.ok(path.basename(resolved).startsWith('avalokita-release-fixture-'));
    fs.rmSync(resolved, { recursive: true, force: true });
  }
});

test('missing resources fail without discovering a system Python', () => {
  assert.throws(() => packagedRuntime('relative'));
  assert.throws(() => packagedRuntime(path.resolve('missing'), 'unsupported'));
  assert.throws(() => packagedRuntime(path.resolve('missing'), 'win32'));
});
