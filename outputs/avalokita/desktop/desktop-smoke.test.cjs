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
const packageConfig = require('../../../packaging/electron-builder.cjs');

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

test('desktop package allowlists the application, runtime resources, and notices', () => {
  assert.deepEqual(packageConfig.files, [
    'outputs/avalokita/desktop/main.cjs',
    'outputs/avalokita/desktop/runtime-contract.cjs',
    'outputs/avalokita/desktop/workspace-registry.cjs',
    'outputs/avalokita/desktop/packaged-runtime.cjs',
    'outputs/avalokita/desktop/preload.cjs',
    'packaging/packaged-runtime.cjs',
    'package.json',
  ]);
  assert.deepEqual(packageConfig.extraResources, [
    { from: 'build/runtime/python', to: 'python', filter: ['**/*'] },
    { from: 'build/runtime/PYTHON-RUNTIME.json', to: 'PYTHON-RUNTIME.json' },
    { from: 'packaging/acp-sdk-runtime.json', to: 'ACP-SDK-RUNTIME.json' },
    { from: 'packaging/ACP-SDK-NOTICE.txt', to: 'ACP-SDK-NOTICE.txt' },
    { from: 'packaging/agent-client-protocol.LICENSE', to: 'licenses/agent-client-protocol.LICENSE' },
    { from: 'build/runtime/reason', to: 'reason', filter: [
      'reason-network', 'reason-network.exe', 'LICENSE-MIT', 'LICENSE-APACHE',
      'NOTICE', 'AVALOKITA-CHANGES.md', 'SOURCE-LOCK.json',
    ] },
    { from: 'outputs/avalokita', to: 'avalokita', filter: [
      'app/dist/**/*', 'server.py', 'bridge.py', 'hosted.py', 'device_discovery.py',
      'inventory_runtime.py', 'inventory_sources.py', 'inventory_vault.py', 'preference_learning.py',
      'judgment.py', 'THIRD_PARTY.md', 'adapters/REA-LICENSE.txt', 'harmony.py', 'native_dispatch.py',
      'avalokita.py', 'mcp_server.py', 'reason_sessions.py', 'adapters/__init__.py',
      'adapters/canonical_reason.py', 'adapters/codex_app_tools.py', 'adapters/reason_engine.py', 'HARMONY.md',
    ] },
    { from: 'outputs/sharebus', to: 'sharebus', filter: [
      '__init__.py', 'sharebus.py', 'store.py', 'discovery.py', 'policy.py', 'install.py', 'daemon.py',
      'supervisor.py', 'hosted_runtime.py', 'adapters.py', 'mcp_server.py', 'native_bridge.py',
      'codex_bridge.py', 'antigravity_bridge.py', 'antigravity_acp.py', 'sidecar_registry.py', 'launcher.py',
      'auto_mode.py', 'auto_enroll.py', 'auto_watch.py', 'hooks_bridge.py', 'templates/team.json',
      'templates/SHAREBUS.md', 'commands/sharebus/SKILL.md', 'commands/sharebus/native_context.md',
      'commands/sharebus/session_hook_forwarder.cjs', 'third_party/NOTICE.md', 'third_party/happy/LICENSE',
      'third_party/agmsg/LICENSE', 'third_party/sources.json', 'third_party/happy/session_hook_forwarder.cjs',
      'third_party/agmsg/watch-once.sh', 'web/index.html', 'web/assets/PretendardVariable.woff2',
      'web/assets/Pretendard-LICENSE.txt', 'web/assets/favicon.svg',
      'web/assets/characters/sharebus-original-artwork.json', 'web/assets/characters/sharebus-codex.svg',
      'web/assets/characters/sharebus-claude.svg', 'web/assets/characters/sharebus-gemini.svg',
    ] },
    { from: 'build/runtime/BUILD-INFO.json', to: 'BUILD-INFO.json' },
    { from: 'NOTICE', to: 'NOTICE' },
    { from: 'LICENSE', to: 'LICENSE' },
  ]);
  assert.equal(rootPackage.scripts.package, 'electron-builder --config packaging/electron-builder.cjs --publish never');
  assert.deepEqual(packageConfig.win.target, ['portable', 'nsis']);
  assert.equal(packageConfig.publish, null);
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
  for (const relative of packageConfig.files) {
    assert.ok(!relative.includes('*') && !path.isAbsolute(relative) && !relative.split(/[\\/]/).includes('..'),
      'desktop fixture copies only explicit paths within the package');
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
