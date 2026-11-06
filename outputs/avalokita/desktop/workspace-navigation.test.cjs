'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const os = require('node:os');
const { WorkspaceRegistry } = require('./workspace-registry.cjs');

function navigationFixture({ argv = [], lock = false, cwd = path.resolve('launcher-directory'), userData, programFiles } = {}) {
  const app = new EventEmitter();
  let lockData;
  app.requestSingleInstanceLock = data => { lockData = data; return lock; };
  app.quit = () => {};
  let readyHandler;
  app.whenReady = () => ({ then(handler) { readyHandler = handler; return { catch() {} }; } });
  app.getPath = name => { assert.equal(name, 'userData'); return userData; };
  const handlers = new Map();
  const ipcMain = { on: (name, handler) => handlers.set(name, handler), handle: () => {} };
  const context = { require: name => name === 'electron' ? { app, ipcMain } : require(name),
    module: { exports: {} }, __dirname, process: { argv, cwd: () => cwd, platform: process.platform, env: { ProgramFiles: programFiles } }, console, URL, setTimeout, clearTimeout };
  // Exercise the actual main handler without starting Electron or a backend.
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8') + `
    installWorkspaceIpc();
    globalThis.navigationHarness = {
      set(next) { active = next.active; pendingWorkspaceRequest = next.pending; inFlightWorkspaceRequest = next.delivered; runtimes.clear(); for (const runtime of next.runtimes) runtimes.set(runtime.entry.id, runtime); },
      setRegistry(next) { registry = next; },
      get() { return { active, pending: pendingWorkspaceRequest, delivered: inFlightWorkspaceRequest }; }, deliver: deliverWorkspaceRequest,
      async startup(failActivation = false) {
        resolveBackend = () => ${JSON.stringify(__filename)};
        resolvePython = () => ${JSON.stringify(process.execPath)};
        activateEntry = async id => { if (failActivation) throw new Error('backend unavailable'); registry.activate(id); return registry.resolve(id); };
        installTray = () => {};
      },
      registry() { return registry; },
      async settled() { await switchQueue; }
    };`, context);
  const sent = [];
  const makeRuntime = id => {
    const frame = { url: `http://127.0.0.1:${id === 'A' ? 41111 : 42222}/` };
    const contents = { mainFrame: frame, getURL: () => frame.url, isDestroyed: () => false,
      send: (channel, value) => sent.push({ workspace: id, channel, value }) };
    return { entry: { id }, url: new URL(frame.url).origin, workspaceListenerReady: true,
      window: { webContents: contents, isDestroyed: () => false, isMinimized: () => false, show() {}, focus() {} } };
  };
  const a = makeRuntime('A');
  const b = makeRuntime('B');
  return { harness: context.navigationHarness, sent, a, b, app, lockData,
    async startup(failActivation = false) { await context.navigationHarness.startup(failActivation); await readyHandler(); return context.navigationHarness.registry(); },
    complete(runtime, result) { handlers.get('avalokita:workspace-request-complete')({ sender: runtime.window.webContents, senderFrame: runtime.window.webContents.mainFrame }, result); },
    ready(runtime) { handlers.get('avalokita:workspace-listener-ready')({ sender: runtime.window.webContents, senderFrame: runtime.window.webContents.mainFrame }, true); } };
}

function startupFixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'avalokita-startup-unit-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const profile = path.join(directory, 'profile');
  const project = path.join(directory, 'project');
  const programFiles = path.join(directory, 'Program Files');
  const protectedHost = path.join(programFiles, 'WindowsApps', 'OpenAI.Codex_test', 'app');
  fs.mkdirSync(profile);
  fs.mkdirSync(project);
  fs.mkdirSync(protectedHost, { recursive: true });
  return { profile, project, programFiles, protectedHost };
}

test('first launch uses an owned persistent workspace instead of the launcher directory', async t => {
  const { profile, programFiles, protectedHost } = startupFixture(t);
  const fixture = navigationFixture({ lock: true, userData: profile, programFiles, cwd: protectedHost });
  const registry = await fixture.startup();
  assert.equal(registry.recent().path, fs.realpathSync.native(path.join(profile, 'workspace')));
  assert.equal(registry.list().length, 1);
  assert.deepEqual(fs.readdirSync(protectedHost), []);
});

test('normal recent workspace remains selected without creating a default workspace', async t => {
  const { profile, project, programFiles, protectedHost } = startupFixture(t);
  const prior = new WorkspaceRegistry(profile);
  const selected = prior.register(project);
  prior.activate(selected.id);
  const fixture = navigationFixture({ lock: true, userData: profile, programFiles, cwd: protectedHost });
  const registry = await fixture.startup();
  assert.equal(registry.recent().id, selected.id);
  assert.equal(fs.existsSync(path.join(profile, 'workspace')), false);
});

test('implicit WindowsApps recent entry is retained while startup recovers to an owned workspace', { skip: process.platform !== 'win32' }, async t => {
  const { profile, programFiles, protectedHost } = startupFixture(t);
  const prior = new WorkspaceRegistry(profile);
  const selected = prior.register(protectedHost);
  prior.activate(selected.id);
  const original = prior.entries.find(entry => entry.id === selected.id);
  const fixture = navigationFixture({ lock: true, userData: profile, programFiles, cwd: protectedHost });
  const registry = await fixture.startup();
  assert.notEqual(registry.recent().id, selected.id);
  assert.deepEqual(registry.entries.find(entry => entry.id === selected.id), original);
  assert.equal(registry.list().length, 2);
  assert.deepEqual(fs.readdirSync(protectedHost), []);
});

test('explicit workspace failure is surfaced without creating a fallback workspace', async t => {
  const { profile, programFiles, protectedHost } = startupFixture(t);
  const fixture = navigationFixture({ lock: true, userData: profile, programFiles, cwd: protectedHost,
    argv: ['Avalokita', '--workspace', protectedHost] });
  await assert.rejects(fixture.startup(true), /backend unavailable/);
  const registry = fixture.harness.registry();
  assert.equal(registry.list().length, 1);
  assert.equal(registry.entries[0].path, fs.realpathSync.native(protectedHost));
  assert.equal(fs.existsSync(path.join(profile, 'workspace')), false);
});

test('linked default workspace is rejected before startup activation', async t => {
  const { profile, project, programFiles, protectedHost } = startupFixture(t);
  fs.symlinkSync(project, path.join(profile, 'workspace'), process.platform === 'win32' ? 'junction' : 'dir');
  const fixture = navigationFixture({ lock: true, userData: profile, programFiles, cwd: protectedHost });
  await assert.rejects(fixture.startup(), /Linked|reparse/);
  assert.equal(fixture.harness.registry().list().length, 0);
});

test('a request waiting on A user switch reroutes only to current B', () => {
  const { harness, sent, a, b, complete } = navigationFixture();
  const pending = { id: 'C', requestId: 'request-c' };
  harness.set({ active: { id: 'B', generation: 2 }, pending, delivered: { ...pending, sourceId: 'A' }, runtimes: [a, b] });
  complete(a, { ...pending, handled: true });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].workspace, 'B');
  assert.equal(sent[0].value.id, 'C');
  assert.equal(harness.get().pending.requestId, 'request-c');
  assert.equal(harness.get().delivered.sourceId, 'B');
});

test('single-instance lock carries the original absolute workspace before Electron reorders argv', () => {
  const absolute = path.resolve('workspace-b');
  const selected = navigationFixture({ argv: ['Avalokita', '--workspace', absolute] });
  assert.equal(selected.lockData.contract, 'avalokita-open/1');
  assert.equal(selected.lockData.workspace, absolute);
  const cwd = path.resolve('secondary-launcher');
  const relative = navigationFixture({ argv: ['Avalokita', '--workspace', 'workspace-b'], cwd });
  assert.equal(relative.lockData.workspace, `${cwd}${path.sep}workspace-b`);
  assert.equal(navigationFixture().lockData.workspace, null);
});

test('reordered real second-instance argv uses only validated additionalData and keeps renderer ACK pending', async () => {
  const { app, harness, sent, a } = navigationFixture({ lock: true });
  const selected = path.resolve('workspace-b');
  const registered = [];
  harness.setRegistry({ register(value) { registered.push(value); return { id: 'B' }; } });
  harness.set({ active: { id: 'A', generation: 1 }, pending: null, delivered: null, runtimes: [a] });
  // Reproduce the packaged Windows event: Chromium moved flags before values.
  const reordered = ['Avalokita.exe', '--ui-test', '--ui-test-profile', '--workspace', '--allow-file-access-from-files', path.resolve('profile'), path.resolve('wrong-positional-directory')];
  app.emit('second-instance', {}, reordered, path.resolve('different-launcher'), { contract: 'avalokita-open/1', workspace: selected });
  await harness.settled();
  assert.deepEqual(registered, [selected]);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].workspace, 'A');
  assert.equal(sent[0].value.id, 'B');
  assert.equal(harness.get().active.id, 'A');
  assert.equal(harness.get().pending.id, 'B');
  assert.equal(harness.get().delivered.sourceId, 'A');
});

test('invalid secondary envelopes cannot fall back to reordered argv or register a workspace', async () => {
  const { app, harness, sent, a } = navigationFixture({ lock: true });
  const selected = path.resolve('workspace-b');
  const registered = [];
  harness.setRegistry({ register(value) { registered.push(value); return { id: 'B' }; } });
  harness.set({ active: { id: 'A', generation: 1 }, pending: null, delivered: null, runtimes: [a] });
  const argv = ['Avalokita', '--workspace', selected];
  for (const data of [undefined, null, [], {}, { contract: 'avalokita-open/0', workspace: selected },
    { contract: 'avalokita-open/1', workspace: 'relative' }, { contract: 'avalokita-open/1', workspace: 1 },
    { contract: 'avalokita-open/1', workspace: `${selected}\0` }, { contract: 'avalokita-open/1', workspace: selected, command: 'ignored' }]) {
    app.emit('second-instance', {}, argv, process.cwd(), data);
  }
  await harness.settled();
  assert.deepEqual(registered, []);
  assert.equal(sent.length, 0);
  assert.equal(harness.get().pending, null);
  app.emit('second-instance', {}, argv, process.cwd(), { contract: 'avalokita-open/1', workspace: null });
  await harness.settled();
  assert.deepEqual(registered, []);
  assert.equal(harness.get().pending, null);
});

test('completion preserves a newer pending selection and consumes same-context failure once', () => {
  const { harness, sent, a, b, complete } = navigationFixture();
  const delivered = { id: 'B', requestId: 'request-b', sourceId: 'A' };
  harness.set({ active: { id: 'B', generation: 2 }, pending: { id: 'C', requestId: 'request-c' }, delivered, runtimes: [a, b] });
  complete(a, { id: 'B', requestId: 'request-b', handled: true });
  assert.equal(sent[0].workspace, 'B');
  assert.equal(sent[0].value.id, 'C');
  sent.length = 0;
  harness.set({ active: { id: 'A', generation: 1 }, pending: { id: 'B', requestId: 'failed-b' },
    delivered: { id: 'B', requestId: 'failed-b', sourceId: 'A' }, runtimes: [a, b] });
  complete(a, { id: 'B', requestId: 'failed-b', handled: true });
  assert.equal(harness.get().pending, null);
  assert.equal(harness.get().delivered, null);
  assert.equal(sent.length, 0);
});

test('pending startup selection waits for readiness and forged completion is ignored', () => {
  const { harness, sent, a, b, ready, complete } = navigationFixture();
  a.workspaceListenerReady = false;
  const pending = { id: 'B', requestId: 'request-b' };
  harness.set({ active: { id: 'A', generation: 1 }, pending, delivered: null, runtimes: [a, b] });
  harness.deliver();
  assert.equal(sent.length, 0);
  ready(a);
  assert.equal(sent.length, 1);
  complete(b, { ...pending, handled: true });
  complete(a, { id: 'B', requestId: 'stale', handled: true });
  assert.equal(harness.get().delivered.sourceId, 'A');
  assert.equal(harness.get().pending.requestId, 'request-b');
});
