'use strict';

const { app, BrowserWindow, Tray, Menu, nativeImage, session, dialog, ipcMain } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { setTimeout: delay } = require('node:timers/promises');
const { performance } = require('node:perf_hooks');
const { validateReady, hostMatches, isLoopbackAddress, samePath, validateWorkspaceSender } = require('./runtime-contract.cjs');
const { WorkspaceRegistry } = require('./workspace-registry.cjs');
const { packagedRuntime, packagedBackendEnvironment } = require('./packaged-runtime.cjs');

const APP_ROOT = path.resolve(__dirname, '..');
const RUN_ID = crypto.randomUUID();
const runtimes = new Map();
let registry = null;
let active = { id: null, generation: 0 };
let switchQueue = Promise.resolve();
let tray = null;
let intentionalQuit = false;
let quitAllowed = false;
let startupStage = 'electron-ready';
const smokeMode = process.argv.includes('--smoke');
const uiTestMode = process.argv.includes('--ui-test');
let smokeReceiptFile = null;
let pythonExecutable = null;
let serverScript = null;
let bundledRuntime = null;
// Opening a folder is a selection request. The latest unconsumed request wins;
// it cannot create native work or bypass the active renderer's draft flush.
let pendingWorkspaceRequest = null;
let startupWorkspaceRequest = null;
let inFlightWorkspaceRequest = null;

// Test profiles change storage only; renderer privileges and API auth stay fixed.
const testProfileIndex = process.argv.indexOf('--ui-test-profile');
if (testProfileIndex >= 0) {
  const testProfile = process.argv[testProfileIndex + 1];
  if ((!smokeMode && !process.argv.includes('--ui-test')) || !testProfile || !path.isAbsolute(testProfile)) {
    throw new Error('--ui-test-profile requires --ui-test or --smoke and an absolute directory');
  }
  fs.mkdirSync(testProfile, { recursive: true });
  app.setPath('userData', path.resolve(testProfile));
  app.setPath('sessionData', path.resolve(testProfile));
}

function cliOption(flag, argv = process.argv.slice(1)) {
  const index = argv.indexOf(flag);
  if (index < 0) return null;
  if (!argv[index + 1] || argv[index + 1].startsWith('--')) throw new Error(`${flag} requires a path`);
  const value = argv[index + 1];
  // Preserve lexical workspace components until the link/reparse check.
  if (flag === '--workspace') return path.isAbsolute(value) ? value : `${process.cwd()}${path.sep}${value}`;
  return path.resolve(value);
}

function absoluteOption(flag) {
  const index = process.argv.indexOf(flag);
  if (index >= 0 && !path.isAbsolute(process.argv[index + 1] || '')) throw new Error(`${flag} path must be absolute`);
  return cliOption(flag);
}

function resolveBackend() {
  if (app.isPackaged) {
    return resolveBundledRuntime().server;
  }
  const candidates = [process.env.AVALOKITA_SERVER,
    process.resourcesPath && path.join(process.resourcesPath, 'avalokita', 'server.py'),
    path.join(APP_ROOT, 'server.py'), path.resolve(process.cwd(), 'outputs', 'avalokita', 'server.py'),
  ].filter(Boolean).map(value => path.resolve(value));
  return candidates.find(value => fs.existsSync(value)) || candidates[0];
}

function resolveBundledRuntime() {
  try {
    bundledRuntime = bundledRuntime || packagedRuntime(process.resourcesPath);
    return bundledRuntime;
  } catch {
    const error = new Error('Avalokita could not load its installed components. Reinstall Avalokita using the installer for your operating system.');
    error.code = 'AVALOKITA_BUNDLED_RUNTIME_INVALID';
    throw error;
  }
}

function resolvePython() {
  if (app.isPackaged) {
    return resolveBundledRuntime().python;
  }
  let basePython = null;
  try {
    const config = fs.readFileSync(path.resolve(process.cwd(), 'work', 'relay-runtime', 'pyvenv.cfg'), 'utf8');
    const executable = config.match(/^executable\s*=\s*(.+)$/mi)?.[1]?.trim();
    if (executable && fs.existsSync(executable)) basePython = executable;
  } catch {}
  let installed = [];
  try {
    const directory = path.join(process.env.LOCALAPPDATA, 'Programs', 'Python');
    installed = fs.readdirSync(directory).filter(name => /^Python\d+$/i.test(name)).sort().reverse().map(name => path.join(directory, name, 'python.exe'));
  } catch {}
  for (const candidate of [process.env.AVALOKITA_PYTHON, basePython,
    path.resolve(process.cwd(), 'work', 'relay-runtime', 'Scripts', 'python.exe'),
    path.resolve(process.cwd(), 'work', 'relay-runtime', 'bin', 'python'), ...installed, 'python', 'python3'].filter(Boolean)) {
    const probe = spawnSync(candidate, ['-I', '-c', 'import sys; print(sys.executable)'], { encoding: 'utf8', timeout: 5000, windowsHide: true, shell: false });
    const executable = probe.status === 0 ? probe.stdout.trim().split(/\r?\n/).at(-1) : '';
    if (path.isAbsolute(executable) && fs.existsSync(executable)) return executable;
  }
  return null;
}

function writeSmokeReceipt(ok, details = {}) {
  if (!smokeReceiptFile) return;
  const runtime = runtimes.get(active.id);
  try { fs.writeFileSync(smokeReceiptFile, JSON.stringify({ ok, rendererLoaded: ok, pid: process.pid, instance_id: runtime?.nonce || RUN_ID,
    workspace: runtime?.entry.path || null, ...details }), { flag: 'wx' }); } catch {}
}

async function stopOwnedBackend(child, owner, graceDeadline) {
  const result = { requestAttempted: false, acknowledged: false, killAttempted: false };
  if (!child) return { ...result, exitObserved: false, outcome: 'no_child' };
  const hasExited = () => child.exitCode != null || child.signalCode != null;
  if (hasExited()) return { ...result, exitObserved: true, outcome: 'already_exited' };
  let exitObserved = false;
  let resolveExit;
  const exited = new Promise(resolve => { resolveExit = resolve; });
  const onExit = () => { exitObserved = true; resolveExit(); };
  child.once('exit', onExit);
  if (hasExited()) onExit();
  const waitUntil = async deadline => {
    if (exitObserved || performance.now() >= deadline) return;
    let timer;
    try { await Promise.race([exited, new Promise(resolve => { timer = setTimeout(resolve, deadline - performance.now()); })]); }
    finally { clearTimeout(timer); }
    if (hasExited()) onExit();
  };
  try {
    if (!exitObserved && owner?.child === child && owner.pid === child.pid && performance.now() < graceDeadline) {
      const body = JSON.stringify({ contract: 'avalokita-owner-shutdown/1', instance_id: owner.instance_id,
        pid: child.pid, workspace: owner.workspace });
      if (Buffer.byteLength(body, 'utf8') <= 4096) {
        const controller = new AbortController();
        let timer;
        result.requestAttempted = true;
        try {
          const request = fetch(`${owner.url}/api/owner/shutdown`, {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Avalokita-Request': '1',
              'X-Avalokita-Instance': owner.instance_id, Authorization: `Bearer ${owner.owner_token}` },
            body, redirect: 'error', credentials: 'omit', signal: controller.signal,
          }).then(response => { result.acknowledged = response.status === 202; }).catch(() => {});
          await Promise.race([request, exited, new Promise(resolve => {
            timer = setTimeout(resolve, Math.min(1000, Math.max(0, graceDeadline - performance.now())));
          })]);
        } catch { /* A failed owner request is not exit proof; retain the same child. */ }
        finally { clearTimeout(timer); controller.abort(); }
      }
    }
    await waitUntil(graceDeadline);
    if (hasExited()) onExit();
    if (!exitObserved) {
      result.killAttempted = true;
      // Use only this retained ChildProcess, never a PID lookup or another generation.
      try { child.kill(); } catch {}
      if (hasExited()) onExit();
      await waitUntil(performance.now() + 1000);
    }
    return { ...result, exitObserved, outcome: exitObserved ? 'exited' : 'exit_unobserved' };
  } finally { child.removeListener('exit', onExit); }
}

function cleanupRuntime(runtime) {
  if (!runtime) return Promise.resolve({ exitObserved: false, outcome: 'no_runtime' });
  if (runtime.cleanupPromise) return runtime.cleanupPromise;
  runtime.cleaning = true;
  const child = runtime.child;
  const owner = runtime.ownerRef;
  const graceDeadline = performance.now() + 4000;
  // Store the Promise before destroying a window can re-enter cleanup.
  runtime.cleanupPromise = Promise.resolve().then(async () => {
    try { if (runtime.window && !runtime.window.isDestroyed()) runtime.window.destroy(); } catch {}
    runtime.cleanupOutcome = await stopOwnedBackend(child, owner, graceDeadline);
    // Backend guarded cleanup owns ready-file deletion, including after retirement.
    if (runtimes.get(runtime.entry.id) === runtime) runtimes.delete(runtime.entry.id);
    return runtime.cleanupOutcome;
  });
  return runtime.cleanupPromise;
}

async function cleanupOwnBackends() { await Promise.all([...runtimes.values()].map(cleanupRuntime)); }

function failApp(error) {
  intentionalQuit = true;
  if (error && !smokeMode && !uiTestMode) {
    const support = app.isPackaged && error.code === 'AVALOKITA_BUNDLED_RUNTIME_INVALID' ? `\nSupport code: ${error.code}` : '';
    dialog.showErrorBox('Avalokita', error.message + support);
  }
  cleanupOwnBackends().finally(() => app.exit(1));
}

async function waitBackend(runtime) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    if (intentionalQuit || runtime.cleaning) throw new Error('Avalokita is quitting');
    if (runtime.child.exitCode !== null || runtime.child.signalCode !== null) throw new Error('Local Avalokita service exited');
    try {
      if (!runtime.ownerRef) {
        const ready = JSON.parse(fs.readFileSync(runtime.readyFile, 'utf8'));
        runtime.ownerRef = Object.freeze({ ...validateReady(ready, {
          nonce: runtime.nonce, pid: runtime.child.pid, workspace: runtime.entry.path,
        }), child: runtime.child });
      }
      const valid = runtime.ownerRef;
      const response = await fetch(`${valid.url}/health`, { signal: AbortSignal.timeout(1200), headers: { 'X-Avalokita-Instance': runtime.nonce } });
      if (response.ok) {
        const health = await response.json();
        if (health.instance_id !== runtime.nonce || health.pid !== runtime.child.pid || !samePath(health.workspace, runtime.entry.path) || health.ok !== true) throw new Error('Local service identity check failed');
        return valid;
      }
    } catch (error) {
      if (error.message.includes('did not match') || error.message.includes('identity check')) throw error;
    }
    await delay(150);
  }
  throw new Error('Timed out waiting for the local Avalokita service');
}

function focusRuntime(runtime) {
  if (!runtime?.window || runtime.window.isDestroyed()) return;
  if (runtime.window.isMinimized()) runtime.window.restore();
  runtime.window.show();
  runtime.window.focus();
}

function denyWindowCapabilities(window) {
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.on('will-frame-navigate', event => event.preventDefault());
  window.webContents.on('will-attach-webview', event => event.preventDefault());
}

async function prepareRuntime(entry, initial = false) {
  if (intentionalQuit) throw new Error('Avalokita is quitting');
  const existing = runtimes.get(entry.id);
  if (existing) {
    if (existing.cleaning || existing.child.exitCode !== null || existing.child.signalCode !== null || existing.window.isDestroyed()) throw new Error('Workspace runtime stopped; restart Avalokita to reconnect it');
    return { runtime: existing, created: false };
  }
  const nonce = initial ? RUN_ID : crypto.randomUUID();
  const readyFile = (initial && absoluteOption('--ready-file')) || path.join(app.getPath('userData'), `runtime-${nonce}.json`);
  if (fs.existsSync(readyFile)) throw new Error('Ready file path already exists');
  const runtime = { entry, nonce, readyFile, child: null, window: null, url: null, ownerRef: null,
    cleaning: false, cleanupPromise: null, cleanupOutcome: null, partition: `persist:avalokita-workspace-${entry.id}` };
  runtimes.set(entry.id, runtime);
  try {
    startupStage = 'backend-ready';
    if (intentionalQuit) throw new Error('Avalokita is quitting');
    runtime.child = spawn(pythonExecutable, [...(bundledRuntime ? ['-B'] : []), serverScript, '--workspace', entry.path, '--host', '127.0.0.1', '--port', '0',
      '--ready-file', readyFile, '--instance-id', nonce], { cwd: path.dirname(serverScript), windowsHide: true, stdio: 'ignore', shell: false,
      env: bundledRuntime ? packagedBackendEnvironment(bundledRuntime, app.getPath('userData'), process.env) : { ...process.env, PYTHONUNBUFFERED: '1' } });
    runtime.child.once('error', () => {});
    runtime.child.once('exit', () => { if (!intentionalQuit && runtime.window && !runtime.window.isDestroyed()) runtime.window.webContents.send('avalokita:backend-exited'); });
    const valid = await waitBackend(runtime);
    if (intentionalQuit || runtime.cleaning) throw new Error('Avalokita is quitting');
    runtime.url = valid.url;
    startupStage = 'cookie-seed';
    const isolated = session.fromPartition(runtime.partition);
    isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    isolated.setPermissionCheckHandler(() => false);
    isolated.webRequest.onBeforeRequest((details, callback) => {
      let allowed = false;
      try { const address = new URL(details.url); allowed = address.origin === runtime.url || ['data:', 'blob:', 'about:'].includes(address.protocol); } catch {}
      callback({ cancel: !allowed });
    });
    await isolated.cookies.set({ url: runtime.url, name: 'avalokita', value: valid.token, path: '/', httpOnly: true, sameSite: 'strict', secure: false });
    if (intentionalQuit || runtime.cleaning) throw new Error('Avalokita is quitting');
    startupStage = 'window';
    runtime.window = new BrowserWindow({ title: `Avalokita · ${entry.name}`, width: 1440, height: 900, minWidth: 320, minHeight: 480, show: false,
      webPreferences: { partition: runtime.partition, preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true, sandbox: true,
        nodeIntegration: false, nodeIntegrationInSubFrames: false, webSecurity: true } });
    denyWindowCapabilities(runtime.window);
    runtime.window.on('close', event => { if (!intentionalQuit) { event.preventDefault(); runtime.window.hide(); } });
    await runtime.window.loadURL(runtime.url);
    const authenticated = await runtime.window.webContents.executeJavaScript("fetch('/api/session', {headers: {'X-Avalokita-Request': '1'}, credentials: 'same-origin', cache: 'no-store'}).then(response => response.status === 200)");
    if (!authenticated || runtime.child.exitCode !== null || runtime.child.signalCode !== null) throw new Error('Workspace window authentication failed');
    return { runtime, created: true };
  } catch (error) { await cleanupRuntime(runtime); throw error; }
}

function workspaceSummary() { return { activeId: active.id, generation: active.generation, entries: registry.list() }; }

function senderRuntime(event, expected = null) {
  const runtime = [...runtimes.values()].find(candidate => candidate.window?.webContents === event.sender);
  return validateWorkspaceSender(event, runtime, active, expected);
}

function serializeSwitch(operation) {
  const pending = switchQueue.then(() => {
    if (intentionalQuit) throw new Error('Avalokita is quitting');
    return operation();
  });
  switchQueue = pending.catch(() => {});
  return pending;
}

async function activateEntry(id, { event = null, expected = null, initial = false } = {}) {
  if (event) senderRuntime(event, expected);
  const entry = registry.resolve(id);
  if (active.id === id) { focusRuntime(runtimes.get(id)); return workspaceSummary(); }
  const prepared = await prepareRuntime(entry, initial);
  try {
    if (event) senderRuntime(event, expected);
    if (intentionalQuit) throw new Error('Avalokita is quitting');
    registry.activate(id);
  } catch (error) { if (prepared.created) await cleanupRuntime(prepared.runtime); throw error; }
  const previous = runtimes.get(active.id);
  active = { id, generation: active.generation + 1 };
  if (previous?.window && !previous.window.isDestroyed()) previous.window.hide();
  if (!smokeMode) focusRuntime(prepared.runtime);
  deliverWorkspaceRequest();
  return workspaceSummary();
}

function deliverWorkspaceRequest() {
  const runtime = runtimes.get(active.id);
  if (!pendingWorkspaceRequest || inFlightWorkspaceRequest || !runtime?.workspaceListenerReady || runtime.window.isDestroyed()) return;
  try {
    if (new URL(runtime.window.webContents.getURL()).origin !== runtime.url) return;
  } catch { return; }
  inFlightWorkspaceRequest = { ...pendingWorkspaceRequest, sourceId: runtime.entry.id };
  runtime.workspaceListenerReady = false;
  focusRuntime(runtime);
  runtime.window.webContents.send('avalokita:workspace-request', pendingWorkspaceRequest);
}

function installWorkspaceIpc() {
  ipcMain.on('avalokita:workspace-request-complete', (event, result) => {
    try {
      const runtime = senderRuntime(event);
      const delivered = inFlightWorkspaceRequest;
      if (!delivered || !result || typeof result !== 'object' || Array.isArray(result)
          || Object.keys(result).sort().join(',') !== 'handled,id,requestId' || typeof result.handled !== 'boolean'
          || runtime.entry.id !== delivered.sourceId || result.id !== delivered.id || result.requestId !== delivered.requestId) return;
      const movedElsewhere = active.id !== delivered.sourceId && active.id !== delivered.id;
      if (result.handled && !movedElsewhere && pendingWorkspaceRequest?.requestId === delivered.requestId) pendingWorkspaceRequest = null;
      inFlightWorkspaceRequest = null;
      deliverWorkspaceRequest();
    } catch { /* Unowned frames cannot complete or reroute a workspace request. */ }
  });
  ipcMain.on('avalokita:workspace-listener-ready', (event, ready) => {
    try {
      const runtime = senderRuntime(event);
      if (typeof ready !== 'boolean') return;
      runtime.workspaceListenerReady = ready;
      if (ready) deliverWorkspaceRequest();
    } catch { /* Unowned frames cannot register a workspace listener. */ }
  });
  ipcMain.handle('avalokita:workspaces:list', event => { senderRuntime(event); return workspaceSummary(); });
  ipcMain.handle('avalokita:workspaces:choose', event => {
    const expected = { activeId: active.id, generation: active.generation };
    senderRuntime(event, expected);
    return serializeSwitch(async () => {
      const origin = senderRuntime(event, expected);
      const selection = await dialog.showOpenDialog(origin.window, { title: 'Avalokita 작업 공간 열기', buttonLabel: '작업 공간 선택', properties: ['openDirectory'] });
      senderRuntime(event, expected);
      if (selection.canceled || !selection.filePaths.length) return { cancelled: true };
      const entry = registry.register(selection.filePaths[0]);
      return { cancelled: false, entry: { id: entry.id, name: entry.name, path: entry.path, lastOpenedAt: entry.lastOpenedAt, availability: 'available' } };
    });
  });
  ipcMain.handle('avalokita:workspaces:activate', (event, id, expected) => {
    if (expected === null || expected === undefined) throw new Error('Workspace switch context is required');
    senderRuntime(event, expected);
    if (typeof id !== 'string') throw new Error('Unknown workspace');
    return serializeSwitch(() => activateEntry(id, { event, expected }));
  });
}

function installTray() {
  const icon = nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAACTSURBVHgBpZKBCYAgEEV/TeAIjuIIbdQIuUGt0CS1gW1iZ2jIVaTnhw+Cvs8/OYDJA4Y8kR3ZR2/kmazxJbpUEfQ/Dm/UG7wVwHkjlQdMFfDdJMFaACebnjJGyDWgcnZu1/lrCrl6NCoEHJBrDwEr5NrT6ko/UV8xdLAC2N49mlc5CylpYh8wCwqrvbBGLoKGvz8Bfq0QPWEUo/EAAAAASUVORK5CYII=');
  if (icon.isEmpty()) throw new Error('Avalokita tray icon could not be loaded');
  tray = new Tray(icon);
  tray.setToolTip('Avalokita');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: '열기', click: () => focusRuntime(runtimes.get(active.id)) },
    { type: 'separator' }, { label: '완전히 종료', click: () => app.quit() },
  ]));
  tray.on('double-click', () => focusRuntime(runtimes.get(active.id)));
}

async function runSmoke() {
  const runtime = runtimes.get(active.id);
  const result = await runtime.window.webContents.executeJavaScript(`(async () => {
    const headers = { 'X-Avalokita-Request': '1' };
    const state = await fetch('/api/state', { headers, credentials: 'same-origin', cache: 'no-store' });
    const teamResponse = await fetch('/api/team', { headers, credentials: 'same-origin', cache: 'no-store' });
    const team = await teamResponse.json();
    const saved = await fetch('/api/team', { method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, credentials: 'same-origin', cache: 'no-store', body: JSON.stringify({ team: team.team, expected_revision: team.revision }) });
    const workspaces = await window.avalokitaDesktop.listWorkspaces();
    return { ready: document.readyState, root: !!document.querySelector('#root'), stateStatus: state.status, teamStatus: teamResponse.status,
      saveStatus: saved.status, workspaceCount: workspaces.entries.length, cookieVisible: document.cookie.includes('avalokita=') };
  })()`);
  if (!result.root || result.ready !== 'complete' || result.stateStatus !== 200 || result.teamStatus !== 200 || result.saveStatus !== 200 || result.workspaceCount !== 1 || result.cookieVisible) throw new Error('Renderer or authenticated workspace smoke check failed');
  runtime.window.show();
  runtime.window.close();
  if (runtime.window.isVisible()) throw new Error('Window close did not hide to tray');
  tray.emit('double-click');
  if (!runtime.window.isVisible()) throw new Error('Window could not be reopened');
  writeSmokeReceipt(true);
  app.quit();
}

// Electron may reorder second-instance argv; send the original selection as data.
const launchWorkspace = cliOption('--workspace');
const hasInstanceLock = app.requestSingleInstanceLock({ contract: 'avalokita-open/1', workspace: launchWorkspace });
if (!hasInstanceLock) app.quit();
else {
  app.on('second-instance', (_event, _argv, _workingDirectory, additionalData) => {
    if (!additionalData || typeof additionalData !== 'object' || Array.isArray(additionalData)
        || Object.keys(additionalData).sort().join(',') !== 'contract,workspace' || additionalData.contract !== 'avalokita-open/1'
        || (additionalData.workspace !== null && (typeof additionalData.workspace !== 'string'
          || !path.isAbsolute(additionalData.workspace) || additionalData.workspace.includes('\0')))) return;
    const workspace = additionalData.workspace;
    // Ask the active renderer to flush its durable draft before activation.
    // Opaque IDs cross this boundary; paths remain in the owned main process.
    serializeSwitch(async () => {
      if (workspace && registry) {
        pendingWorkspaceRequest = { id: registry.register(workspace).id, requestId: crypto.randomUUID() };
        deliverWorkspaceRequest();
      } else if (workspace) startupWorkspaceRequest = workspace;
      else focusRuntime(runtimes.get(active.id));
    }).catch(error => dialog.showErrorBox('Avalokita', error.message));
  });
  app.whenReady().then(async () => {
    if (smokeMode) {
      smokeReceiptFile = absoluteOption('--smoke-receipt');
      if (smokeReceiptFile && fs.existsSync(smokeReceiptFile)) throw new Error('Smoke receipt path already exists');
    }
    startupStage = 'registry';
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    registry = new WorkspaceRegistry(app.getPath('userData'));
    let initial = launchWorkspace ? registry.register(launchWorkspace) : registry.recent();
    if (!launchWorkspace) {
      const windowsApps = process.platform === 'win32' && process.env.ProgramFiles
        ? path.join(process.env.ProgramFiles, 'WindowsApps') : null;
      const relative = initial && windowsApps ? path.relative(windowsApps, initial.path) : null;
      // Older launches could mistake the host's protected installation for a workspace.
      // Keep that entry, but never use it as an implicit startup selection.
      if (!initial || relative !== null && relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)) {
        const directory = path.join(registry.directory, 'workspace');
        try { fs.mkdirSync(directory, { mode: 0o700 }); }
        catch (error) { if (error.code !== 'EEXIST') throw error; }
        initial = registry.register(directory);
      }
    }
    startupStage = 'server-path';
    serverScript = resolveBackend();
    if (!serverScript || !fs.existsSync(serverScript)) throw new Error('Avalokita server.py was not found');
    startupStage = 'python-discovery';
    pythonExecutable = resolvePython();
    if (!pythonExecutable) throw new Error('Python was not found. Set AVALOKITA_PYTHON to a Python executable.');
    installWorkspaceIpc();
    await serializeSwitch(() => activateEntry(initial.id, { initial: true }));
    if (startupWorkspaceRequest) {
      pendingWorkspaceRequest = { id: registry.register(startupWorkspaceRequest).id, requestId: crypto.randomUUID() };
      startupWorkspaceRequest = null;
      deliverWorkspaceRequest();
    }
    installTray();
    if (smokeMode) await runSmoke();
  }).catch(error => {
    writeSmokeReceipt(false, { failure: 'startup', stage: startupStage, errorName: error?.name || 'Error', errorCode: typeof error?.code === 'string' ? error.code : null });
    console.error(`Avalokita startup failed: ${error.message}`);
    failApp(error);
  });
}

app.on('before-quit', event => {
  if (quitAllowed) return;
  event.preventDefault();
  if (intentionalQuit) return;
  intentionalQuit = true;
  cleanupOwnBackends().finally(() => { quitAllowed = true; app.quit(); });
});
app.on('will-quit', () => { if (tray) tray.destroy(); });
app.on('window-all-closed', () => { /* Closing a workspace hides it; accepted work stays alive. */ });

module.exports = { hostMatches, isLoopbackAddress };
