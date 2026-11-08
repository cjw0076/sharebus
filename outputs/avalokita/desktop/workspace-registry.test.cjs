'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { WorkspaceRegistry, canonicalDirectory } = require('./workspace-registry.cjs');
const { validateWorkspaceSender } = require('./runtime-contract.cjs');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'avalokita-desktop-unit-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  for (const name of ['profile', 'alpha', 'beta']) fs.mkdirSync(path.join(directory, name));
  return { directory, profile: path.join(directory, 'profile'), alpha: path.join(directory, 'alpha'), beta: path.join(directory, 'beta') };
}

test('registry persists opaque directory identities and deduplicates real aliases', t => {
  const { profile, alpha, beta } = fixture(t);
  const registry = new WorkspaceRegistry(profile);
  const a = registry.register(alpha);
  assert.equal(registry.register(`${alpha}${path.sep}`).id, a.id);
  if (process.platform === 'win32') assert.equal(registry.register(alpha.toUpperCase()).id, a.id);
  const b = registry.register(beta);
  assert.notEqual(a.id, b.id);
  registry.activate(a.id);
  registry.activate(b.id);
  const reopened = new WorkspaceRegistry(profile);
  assert.equal(reopened.recent().id, b.id);
  assert.equal(reopened.list().length, 2);
  assert.equal(reopened.list().every(entry => entry.availability === 'available'), true);
  assert.equal(fs.readdirSync(profile).filter(name => name.endsWith('.tmp')).length, 0);
  assert.equal(JSON.stringify(reopened.list()).includes('token'), false);
});

test('nested directories and copied collaboration identifiers remain separate namespaces', t => {
  const { profile, alpha, beta } = fixture(t);
  const nested = path.join(alpha, 'nested');
  fs.mkdirSync(nested);
  for (const directory of [alpha, beta]) fs.writeFileSync(path.join(directory, 'copied-workspace-id.json'), '{"workspace_id":"same-copied-value"}');
  const registry = new WorkspaceRegistry(profile);
  const ids = [alpha, beta, nested].map(directory => registry.register(directory).id);
  assert.equal(new Set(ids).size, 3);
});

test('junctions and lexical link traversal fail before canonicalization', t => {
  const { directory, profile, alpha } = fixture(t);
  const link = path.join(directory, 'alias');
  fs.symlinkSync(alpha, link, process.platform === 'win32' ? 'junction' : 'dir');
  const registry = new WorkspaceRegistry(profile);
  assert.throws(() => registry.register(link), /Linked|reparse/);
  assert.throws(() => canonicalDirectory(`${link}${path.sep}..${path.sep}alpha`), /dot segments/);
  assert.equal(registry.list().length, 0);
});

test('Windows attribute helper ignores an executable planted on PATH', { skip: process.platform !== 'win32' }, t => {
  const { directory, alpha } = fixture(t);
  fs.writeFileSync(path.join(directory, 'powershell.exe'), 'not a system executable');
  const previous = process.env.PATH;
  process.env.PATH = directory;
  try { assert.equal(canonicalDirectory(alpha).path.toLowerCase(), fs.realpathSync.native(alpha).toLowerCase()); }
  finally { process.env.PATH = previous; }
});

test('missing workspace is retained but skipped for startup and cannot activate', t => {
  const { profile, alpha, beta } = fixture(t);
  const registry = new WorkspaceRegistry(profile);
  const a = registry.register(alpha);
  const b = registry.register(beta);
  registry.activate(a.id);
  registry.activate(b.id);
  fs.rmdirSync(beta);
  assert.equal(registry.list().find(entry => entry.id === b.id).availability, 'missing');
  assert.equal(registry.recent().id, a.id);
  assert.throws(() => registry.activate(b.id));
  assert.equal(registry.lastActiveId, b.id);
});

test('hardlinked registry and invalid JSON cannot be adopted or overwritten', t => {
  const { directory, profile, alpha } = fixture(t);
  const registry = new WorkspaceRegistry(profile);
  registry.register(alpha);
  const before = fs.readFileSync(registry.file, 'utf8');
  fs.linkSync(registry.file, path.join(directory, 'registry-copy.json'));
  assert.throws(() => new WorkspaceRegistry(profile), /ordinary file/);
  assert.throws(() => registry.save(), /ordinary file/);
  assert.equal(fs.readFileSync(registry.file, 'utf8'), before);
  fs.unlinkSync(path.join(directory, 'registry-copy.json'));
  fs.writeFileSync(registry.file, '{invalid');
  assert.throws(() => new WorkspaceRegistry(profile));
});

test('a replaced directory cannot reuse its old workspace identity or session partition', t => {
  const { directory, profile, alpha } = fixture(t);
  const registry = new WorkspaceRegistry(profile);
  const original = registry.register(alpha);
  fs.renameSync(alpha, path.join(directory, 'original-alpha'));
  fs.mkdirSync(alpha);
  assert.throws(() => registry.resolve(original.id), /directory changed/);
  assert.equal(registry.list().find(entry => entry.id === original.id).availability, 'unavailable');
  assert.notEqual(registry.register(alpha).id, original.id);
});

test('failed durable activation rolls back in-memory recent selection', t => {
  const { profile, alpha, beta } = fixture(t);
  const registry = new WorkspaceRegistry(profile);
  const a = registry.register(alpha);
  const b = registry.register(beta);
  registry.activate(a.id);
  const before = fs.readFileSync(registry.file, 'utf8');
  const original = fs.renameSync;
  fs.renameSync = () => { throw new Error('simulated rename failure'); };
  try { assert.throws(() => registry.activate(b.id), /simulated rename/); } finally { fs.renameSync = original; }
  assert.equal(registry.lastActiveId, a.id);
  assert.equal(fs.readFileSync(registry.file, 'utf8'), before);
  assert.equal(fs.readdirSync(profile).filter(name => name.endsWith('.tmp')).length, 0);
});

function senderFixture() {
  const frame = { url: 'http://127.0.0.1:45678/' };
  const contents = { mainFrame: frame, isDestroyed: () => false, getURL: () => frame.url };
  const runtime = { entry: { id: 'workspace-a' }, url: 'http://127.0.0.1:45678', window: { isDestroyed: () => false, webContents: contents } };
  return { event: { sender: contents, senderFrame: frame }, runtime, active: { id: 'workspace-a', generation: 7 }, expected: { activeId: 'workspace-a', generation: 7 } };
}

test('IPC admits only the owned exact-origin main frame', () => {
  const { event, runtime, active, expected } = senderFixture();
  assert.equal(validateWorkspaceSender(event, runtime, active, expected), runtime);
  assert.throws(() => validateWorkspaceSender({ ...event, senderFrame: { url: event.senderFrame.url } }, runtime, active), /owned main frame/);
  assert.throws(() => validateWorkspaceSender({ ...event, sender: { ...event.sender } }, runtime, active), /owned main frame/);
  event.senderFrame.url = 'http://127.0.0.1:45679/';
  assert.throws(() => validateWorkspaceSender(event, runtime, active), /origin/);
});

test('IPC switch rejects stale generation, inactive sender and extra command fields', () => {
  const { event, runtime, active, expected } = senderFixture();
  for (const changed of [{ ...expected, generation: 6 }, { ...expected, activeId: 'workspace-b' }, { ...expected, path: 'C:/other' }, [], 'workspace-a']) {
    assert.throws(() => validateWorkspaceSender(event, runtime, active, changed), /stale/);
  }
  assert.throws(() => validateWorkspaceSender(event, runtime, { id: 'workspace-b', generation: 7 }, { activeId: 'workspace-b', generation: 7 }), /stale/);
});
