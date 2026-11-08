'use strict';

const path = require('node:path');
const net = require('node:net');

function samePath(left, right) {
  const a = path.resolve(left);
  const b = path.resolve(right);
  return process.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
}

function validateReady(value, { nonce, pid, workspace }) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid readiness document');
  if (typeof nonce !== 'string' || !nonce || value.instance_id !== nonce) throw new Error('Backend instance nonce did not match');
  if (!Number.isSafeInteger(pid) || pid <= 0 || !Number.isSafeInteger(value.pid) || value.pid !== pid) throw new Error('Backend process id did not match');
  if (typeof value.workspace !== 'string' || !path.isAbsolute(value.workspace) || !samePath(value.workspace, workspace)) throw new Error('Backend workspace did not match');
  for (const key of ['token', 'owner_token']) {
    if (typeof value[key] !== 'string' || !/^[A-Za-z0-9_-]{32,512}$/.test(value[key])) throw new Error('Backend token is invalid');
  }
  if (value.owner_token === value.token) throw new Error('Backend owner token must be independent');
  let url;
  try { url = new URL(value.url); } catch { throw new Error('Backend URL is invalid'); }
  if (url.protocol !== 'http:' || !['127.0.0.1', '::1', '[::1]'].includes(url.hostname) || !url.port
      || (value.url !== url.origin && value.url !== `${url.origin}/`)) {
    throw new Error('Backend URL must be a plain loopback HTTP origin');
  }
  return { url: url.origin, token: value.token, owner_token: value.owner_token,
    instance_id: value.instance_id, pid: value.pid, workspace: value.workspace };
}

function hostMatches(requestHost, expectedUrl) {
  try { return requestHost === new URL(expectedUrl).host; } catch { return false; }
}

function isLoopbackAddress(address) {
  return address === '127.0.0.1' || address === '::1' || address === '::ffff:127.0.0.1';
}

function validateWorkspaceSender(event, runtime, active, expected = null) {
  if (!runtime || runtime.window.isDestroyed() || event.sender !== runtime.window.webContents
      || !event.senderFrame || event.senderFrame !== event.sender.mainFrame
      || event.sender.isDestroyed()) throw new Error('Workspace IPC requires its owned main frame');
  let origin;
  let mainOrigin;
  try { origin = new URL(event.senderFrame.url).origin; mainOrigin = new URL(event.sender.getURL()).origin; } catch {}
  if (origin !== runtime.url || mainOrigin !== runtime.url) throw new Error('Workspace IPC origin does not match');
  if (expected !== null && (!expected || typeof expected !== 'object' || Array.isArray(expected)
      || Object.keys(expected).sort().join(',') !== 'activeId,generation'
      || expected.activeId !== active.id || expected.generation !== active.generation
      || runtime.entry.id !== active.id)) throw new Error('Workspace switch context is stale');
  return runtime;
}

module.exports = { validateReady, hostMatches, isLoopbackAddress, samePath, validateWorkspaceSender };
