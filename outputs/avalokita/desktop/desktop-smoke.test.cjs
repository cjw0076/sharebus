'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const { EventEmitter } = require('node:events');
const { validateReady, hostMatches, isLoopbackAddress } = require('./runtime-contract.cjs');
const rootPackage = require('../../../package.json');

const workspace = path.resolve('C:/Avalokita Test Workspace');
const identity = { nonce: 'instance-0123456789abcdef', pid: 31415, workspace };
const ready = { url: 'http://127.0.0.1:43127', pid: identity.pid, workspace, instance_id: identity.nonce,
  token: 'x'.repeat(48), owner_token: 'y'.repeat(48) };

test('accepts only a ready record bound to this nonce, child pid, workspace, and loopback port', () => {
  assert.equal(validateReady(ready, identity).url, ready.url);
  for (const changed of [
    { ...ready, instance_id: 'other-instance' },
    { ...ready, pid: identity.pid + 1 },
    { ...ready, workspace: 'C:/Other Workspace' },
    { ...ready, url: 'http://192.168.1.10:43127' },
    { ...ready, url: 'http://127.0.0.1:43127/private?token=leak' },
    { ...ready, token: '' },
  ]) assert.throws(() => validateReady(changed, identity));
});

test('accepts only the exact local Host and loopback peer addresses', () => {
  assert.equal(hostMatches('127.0.0.1:43127', ready.url), true);
  assert.equal(hostMatches('127.0.0.1:43128', ready.url), false);
  assert.equal(hostMatches('evil.example', ready.url), false);
  assert.equal(isLoopbackAddress('127.0.0.1'), true);
  assert.equal(isLoopbackAddress('::ffff:127.0.0.1'), true);
  assert.equal(isLoopbackAddress('192.168.1.10'), false);
});

test('desktop source keeps renderer privileges disabled and cleanup scoped to the child handle', () => {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  assert.match(source, /contextIsolation:\s*true/);
  assert.match(source, /sandbox:\s*true/);
  assert.match(source, /nodeIntegration:\s*false/);
  assert.match(source, /webSecurity:\s*true/);
  assert.match(source, /setWindowOpenHandler\(\(\) => \(\{ action: 'deny' \}\)\)/);
  assert.match(source, /will-navigate/);
  assert.match(source, /setPermissionRequestHandler\([^\n]*callback\(false\)/);
  assert.match(source, /new Tray\(icon\)/);
  assert.match(source, /label: '완전히 종료', click: \(\) => app\.quit\(\)/);
  assert.match(source, /runtime\.window\.hide\(\)/);
  assert.match(source, /absoluteOption\('--ready-file'\)/);
  assert.match(source, /Ready file path already exists/);
  assert.match(source, /absoluteOption\('--smoke-receipt'\)/);
  assert.match(source, /writeSmokeReceipt\(true\)/);
  assert.match(source, /pid: process\.pid, instance_id: runtime\?\.nonce \|\| RUN_ID/);
  assert.match(source, /app\.exit\(1\)/);
  assert.match(source, /--ui-test-profile requires --ui-test or --smoke and an absolute directory/);
  assert.ok(source.indexOf("app.setPath('userData'") < source.indexOf('app.requestSingleInstanceLock('));
  assert.ok(source.indexOf("app.setPath('sessionData'") < source.indexOf('app.requestSingleInstanceLock('));
  assert.match(source, /child\.kill\(\)/);
  assert.doesNotMatch(source, /process\.kill\(|taskkill|exec\s*\(/i);
  assert.doesNotMatch(source, /console\.(?:log|info|debug).*token/i);
});

test('desktop package includes only runnable Avalokita assets and pinned Sharebus modules', () => {
  const resources = rootPackage.build.extraResources;
  assert.deepEqual(resources.map(item => item.to), ['avalokita', 'sharebus']);
  assert.deepEqual(resources[0].filter, ['app/dist/**/*', 'server.py', 'bridge.py', 'hosted.py', 'device_discovery.py', 'inventory_vault.py', 'inventory_sources.py', 'preference_learning.py', 'judgment.py', 'inventory_runtime.py', 'harmony.py', 'native_dispatch.py', 'adapters/__init__.py', 'adapters/codex_app_tools.py', 'adapters/canonical_reason.py', 'adapters/reason_engine.py', 'HARMONY.md', 'THIRD_PARTY.md', 'avalokita.py', 'mcp_server.py']);
  assert.ok(rootPackage.build.files.includes('outputs/avalokita/desktop/preload.cjs'));
  assert.ok(rootPackage.build.files.includes('outputs/avalokita/desktop/workspace-registry.cjs'));
  assert.deepEqual(resources[1].filter, ['store.py', 'discovery.py', 'policy.py', 'install.py', 'daemon.py', 'supervisor.py', 'hosted_runtime.py', 'adapters.py', 'mcp_server.py', 'native_bridge.py', 'codex_bridge.py', 'antigravity_bridge.py', 'sidecar_registry.py', 'sharebus.py', 'auto_enroll.py', 'auto_mode.py', 'auto_watch.py', 'hooks_bridge.py', 'templates/team.json', 'templates/SHAREBUS.md', 'commands/sharebus/SKILL.md', 'commands/sharebus/session_hook_forwarder.cjs', 'third_party/NOTICE.md', 'third_party/sources.json', 'third_party/happy/LICENSE', 'third_party/happy/session_hook_forwarder.cjs', 'third_party/agmsg/LICENSE', 'third_party/agmsg/watch-once.sh', 'web/index.html', 'web/assets/PretendardVariable.woff2', 'web/assets/Pretendard-LICENSE.txt', 'web/assets/favicon.svg', 'web/assets/characters/sharebus-original-artwork.json', 'web/assets/characters/sharebus-codex.svg', 'web/assets/characters/sharebus-claude.svg', 'web/assets/characters/sharebus-gemini.svg']);
  assert.ok(!resources.some(item => item.filter?.some(pattern => /(^|\/)(?:node_modules|private|\.sharebus|credentials|logs|tests|workspace|evidence)(?:\/|$)/i.test(pattern))));
  assert.equal(rootPackage.scripts.start, 'npm run build && electron .');
  assert.match(rootPackage.scripts['package:desktop'], /electron-builder --win portable --x64/);
  assert.match(rootPackage.scripts['package:desktop:dir'], /electron-builder --win --x64 --dir/);
});

test('allowlisted desktop files load main and its local dependencies without starting Electron', t => {
  const temporary = fs.realpathSync(os.tmpdir());
  const root = fs.mkdtempSync(path.join(temporary, 'avalokita-desktop-import-'));
  t.after(() => {
    for (const filename of Object.keys(require.cache)) {
      const relative = path.relative(root, filename);
      if (relative && !relative.startsWith('..') && !path.isAbsolute(relative)) delete require.cache[filename];
    }
    const relative = path.relative(temporary, fs.realpathSync(root));
    assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative));
    fs.rmSync(root, { recursive: true, force: true });
  });
  for (const relative of rootPackage.build.files) {
    assert.ok(!relative.includes('*'), 'desktop fixture copies only explicit package files');
    const target = path.join(root, relative);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(path.resolve(__dirname, '../../..', relative), target);
  }
  const main = path.join(root, rootPackage.main);
  const app = new EventEmitter();
  let lockChecks = 0;
  let quits = 0;
  app.requestSingleInstanceLock = () => { lockChecks++; return false; };
  app.quit = () => { quits++; };
  app.whenReady = () => { throw new Error('Import fixture must not start Electron'); };
  const originalLoad = Module._load;
  try {
    Module._load = function (request, parent, isMain) {
      return request === 'electron' && parent?.filename === main
        ? { app } : originalLoad.call(this, request, parent, isMain);
    };
    const loaded = require(main);
    assert.equal(typeof loaded.hostMatches, 'function');
    assert.equal(typeof loaded.isLoopbackAddress, 'function');
    assert.equal(lockChecks, 1);
    assert.equal(quits, 1);
    for (const name of ['runtime-contract.cjs', 'workspace-registry.cjs', 'packaged-runtime.cjs']) {
      assert.ok(require.cache[path.join(path.dirname(main), name)], `${name} loaded from package fixture`);
    }
  } finally {
    Module._load = originalLoad;
  }
});
