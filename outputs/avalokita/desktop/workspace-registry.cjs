'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const ID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function lexicalParts(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || value.includes('\0')) throw new Error('Choose an absolute workspace directory');
  if (process.platform === 'win32' && (/^\\\\[?.]\\/.test(value) || value.split(/[\\/]/).some(part => part.includes(':') && !/^[a-z]:$/i.test(part)))) {
    throw new Error('Device paths and alternate streams are unsupported');
  }
  const root = path.parse(value).root;
  const parts = value.slice(root.length).split(/[\\/]/).filter(Boolean);
  if (parts.some(part => part === '.' || part === '..')) throw new Error('Workspace paths cannot contain dot segments');
  const result = [root];
  for (const part of parts) result.push(path.join(result.at(-1), part));
  return result;
}

function rejectLinkedPath(value) {
  const parts = lexicalParts(value);
  for (const part of parts) {
    const stat = fs.lstatSync(part);
    if (stat.isSymbolicLink()) throw new Error('Linked or reparse workspace paths are unsupported');
  }
  if (process.platform === 'win32') {
    const windowsDirectory = process.env.SystemRoot;
    if (!windowsDirectory || !path.isAbsolute(windowsDirectory)) throw new Error('Windows system directory could not be verified');
    const powershell = path.join(windowsDirectory, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    // Node does not expose FILE_ATTRIBUTE_REPARSE_POINT for every reparse type.
    // Pass paths as stdin data, never as executable PowerShell source.
    const result = spawnSync(powershell, ['-NoProfile', '-NonInteractive', '-Command',
      "$ErrorActionPreference='Stop'; $paths = [Console]::In.ReadToEnd() | ConvertFrom-Json; foreach ($candidate in $paths) { if (([IO.File]::GetAttributes($candidate) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { exit 3 } }"],
    { input: JSON.stringify(parts), encoding: 'utf8', timeout: 15000, windowsHide: true, shell: false });
    if (result.error?.code === 'ETIMEDOUT') throw new Error('Workspace path verification timed out; retry when the device is ready');
    if (result.status === 3) throw new Error('Linked or reparse workspace paths are unsupported');
    if (result.error || result.status !== 0) throw new Error('Workspace path attributes could not be verified');
  }
}

function canonicalDirectory(value) {
  rejectLinkedPath(value);
  const canonical = fs.realpathSync.native(value);
  const stat = fs.statSync(canonical, { bigint: true });
  if (!stat.isDirectory()) throw new Error('Workspace must be an existing directory');
  return { path: canonical, identity: `${stat.dev}:${stat.ino}` };
}

function pathKey(value) { return process.platform === 'win32' ? value.toLowerCase() : value; }

function assertConfigFile(file) {
  try {
    const stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) throw new Error('Workspace registry must be a private ordinary file');
    rejectLinkedPath(file);
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
}

class WorkspaceRegistry {
  constructor(directory) {
    this.directory = canonicalDirectory(directory).path;
    this.file = path.join(this.directory, 'workspaces.json');
    this.entries = [];
    this.lastActiveId = null;
    assertConfigFile(this.file);
    if (!fs.existsSync(this.file)) return;
    if (fs.statSync(this.file).size > 256 * 1024) throw new Error('Workspace registry is too large');
    const saved = JSON.parse(fs.readFileSync(this.file, 'utf8'));
    if (!saved || saved.version !== 1 || !Array.isArray(saved.entries) || saved.entries.length > 100) throw new Error('Invalid workspace registry');
    const ids = new Set();
    for (const entry of saved.entries) {
      if (!entry || !ID.test(entry.id) || ids.has(entry.id) || typeof entry.path !== 'string' || !path.isAbsolute(entry.path)
          || typeof entry.lastOpenedAt !== 'number' || !Number.isFinite(entry.lastOpenedAt) || entry.lastOpenedAt < 0
          || typeof entry.directoryIdentity !== 'string' || !/^\d+:\d+$/.test(entry.directoryIdentity)) throw new Error('Invalid workspace entry');
      ids.add(entry.id);
      this.entries.push({ id: entry.id, path: entry.path, name: path.basename(entry.path) || entry.path, lastOpenedAt: entry.lastOpenedAt, directoryIdentity: entry.directoryIdentity });
    }
    if (saved.lastActiveId !== null && !ids.has(saved.lastActiveId)) throw new Error('Invalid recent workspace');
    this.lastActiveId = saved.lastActiveId;
  }

  save() {
    if (fs.existsSync(this.file)) assertConfigFile(this.file);
    else rejectLinkedPath(this.directory);
    const temporary = path.join(this.directory, `.workspaces-${crypto.randomUUID()}.tmp`);
    const descriptor = fs.openSync(temporary, 'wx', 0o600);
    try {
      fs.writeFileSync(descriptor, JSON.stringify({ version: 1, lastActiveId: this.lastActiveId, entries: this.entries }, null, 2));
      fs.fsyncSync(descriptor);
    } finally { fs.closeSync(descriptor); }
    try {
      assertConfigFile(this.file);
      fs.renameSync(temporary, this.file);
    } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  }

  register(value) {
    const canonical = canonicalDirectory(value);
    let entry = this.entries.find(candidate => {
      if (candidate.directoryIdentity !== canonical.identity) return false;
      try {
        const existing = canonicalDirectory(candidate.path);
        return existing.identity === candidate.directoryIdentity && (pathKey(existing.path) === pathKey(canonical.path) || existing.identity === canonical.identity);
      } catch { return false; }
    });
    if (entry) return { ...entry };
    if (this.entries.length >= 100) throw new Error('The recent workspace limit was reached');
    entry = { id: crypto.randomUUID(), path: canonical.path, name: path.basename(canonical.path) || canonical.path, lastOpenedAt: 0, directoryIdentity: canonical.identity };
    this.entries.push(entry);
    try { this.save(); } catch (error) { this.entries.pop(); throw error; }
    return { ...entry };
  }

  resolve(id) {
    if (!ID.test(id)) throw new Error('Unknown workspace');
    const entry = this.entries.find(candidate => candidate.id === id);
    if (!entry) throw new Error('Unknown workspace');
    const current = canonicalDirectory(entry.path);
    if (pathKey(current.path) !== pathKey(entry.path) || current.identity !== entry.directoryIdentity) throw new Error('Workspace directory changed; choose it again');
    return { ...entry };
  }

  activate(id) {
    const entry = this.resolve(id);
    const previous = this.entries;
    const previousActiveId = this.lastActiveId;
    this.entries = this.entries.map(candidate => candidate.id === id ? { ...entry, lastOpenedAt: Date.now() } : candidate);
    this.lastActiveId = id;
    try { this.save(); } catch (error) { this.entries = previous; this.lastActiveId = previousActiveId; throw error; }
  }

  recent() {
    const sorted = [...this.entries].sort((a, b) => (b.id === this.lastActiveId) - (a.id === this.lastActiveId) || b.lastOpenedAt - a.lastOpenedAt);
    for (const entry of sorted) { try { return this.resolve(entry.id); } catch {} }
    return null;
  }

  list() {
    return [...this.entries].sort((a, b) => b.lastOpenedAt - a.lastOpenedAt).map(entry => {
      let availability = 'available';
      // This list is a cheap directory observation, never activation authority.
      // Activation rechecks all Windows reparse attributes before using an ID.
      try {
        for (const part of lexicalParts(entry.path)) if (fs.lstatSync(part).isSymbolicLink()) throw new Error('Linked directory');
        const canonical = fs.realpathSync.native(entry.path);
        const stat = fs.statSync(canonical, { bigint: true });
        if (!stat.isDirectory() || pathKey(canonical) !== pathKey(entry.path) || `${stat.dev}:${stat.ino}` !== entry.directoryIdentity) throw new Error('Directory identity changed');
      } catch (error) { availability = error.code === 'ENOENT' ? 'missing' : 'unavailable'; }
      return { id: entry.id, name: entry.name, path: entry.path, lastOpenedAt: entry.lastOpenedAt, availability };
    });
  }
}

module.exports = { WorkspaceRegistry, canonicalDirectory, rejectLinkedPath };
