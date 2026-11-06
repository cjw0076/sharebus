'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function loadPreload(mainFrame = true) {
  let exposed = null;
  const listeners = new Map();
  const sent = [];
  const ipcRenderer = {
    invoke: async (...args) => args,
    send: (...args) => sent.push(structuredClone(args)),
    on: (channel, listener) => { const current = listeners.get(channel) || new Set(); current.add(listener); listeners.set(channel, current); },
    removeListener: (channel, listener) => listeners.get(channel)?.delete(listener),
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'preload.cjs'), 'utf8'), {
    process: { isMainFrame: mainFrame },
    require: name => { assert.equal(name, 'electron'); return { ipcRenderer, contextBridge: { exposeInMainWorld: (name, value) => { assert.equal(name, 'avalokitaDesktop'); exposed = value; } } }; },
  });
  return { get exposed() { return exposed; }, sent, emit: async value => {
    await Promise.all([...(listeners.get('avalokita:workspace-request') || [])].map(listener => listener({ privateIpcEvent: true }, value)));
  } };
}

test('preload exposes fixed workspace operations and no generic IPC proxy', () => {
  const loaded = loadPreload();
  assert.deepEqual(Object.keys(loaded.exposed).sort(), ['activateWorkspace', 'chooseWorkspace', 'listWorkspaces', 'onWorkspaceActivationRequested']);
  assert.equal(loadPreload(false).exposed, null);
});

test('workspace request subscription forwards only opaque IDs and unregisters exactly once', async () => {
  const loaded = loadPreload();
  const received = [];
  const unsubscribe = loaded.exposed.onWorkspaceActivationRequested((...args) => received.push(args));
  const id = 'b995d97e-45f7-47e4-b7f6-8fbe147e1fb4';
  const requestId = 'cd69b1c4-6f41-4f2c-ae58-143c278b15f8';
  await loaded.emit('C:/arbitrary-folder');
  await loaded.emit({ id });
  await loaded.emit({ id, requestId });
  assert.deepEqual(received, [[id]]);
  assert.deepEqual(loaded.sent, [
    ['avalokita:workspace-listener-ready', true], ['avalokita:workspace-listener-ready', false],
    ['avalokita:workspace-request-complete', { id, requestId, handled: true }], ['avalokita:workspace-listener-ready', true],
  ]);
  unsubscribe();
  unsubscribe();
  await loaded.emit({ id, requestId });
  assert.equal(received.length, 1);
  assert.deepEqual(loaded.sent.at(-1), ['avalokita:workspace-request-complete', { id, requestId, handled: false }]);
});

test('request completion waits for the returned renderer Promise before rearming', async () => {
  const loaded = loadPreload();
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  loaded.exposed.onWorkspaceActivationRequested(() => pending);
  const request = { id: 'b995d97e-45f7-47e4-b7f6-8fbe147e1fb4', requestId: 'cd69b1c4-6f41-4f2c-ae58-143c278b15f8' };
  const completed = loaded.emit(request);
  await Promise.resolve();
  assert.equal(loaded.sent.filter(([channel]) => channel === 'avalokita:workspace-request-complete').length, 0);
  assert.deepEqual(loaded.sent.at(-1), ['avalokita:workspace-listener-ready', false]);
  release();
  await completed;
  assert.deepEqual(loaded.sent.at(-1), ['avalokita:workspace-listener-ready', true]);
  assert.equal(loaded.sent.filter(([channel]) => channel === 'avalokita:workspace-request-complete').length, 1);
});

test('multiple request listeners keep readiness until the last unsubscribe', () => {
  const loaded = loadPreload();
  const first = loaded.exposed.onWorkspaceActivationRequested(() => {});
  const second = loaded.exposed.onWorkspaceActivationRequested(() => {});
  first();
  assert.equal(loaded.sent.length, 1);
  second();
  assert.deepEqual(loaded.sent.at(-1), ['avalokita:workspace-listener-ready', false]);
  assert.throws(() => loaded.exposed.onWorkspaceActivationRequested('command'), /function/);
});
