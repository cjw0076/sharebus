'use strict';

// Run explicitly with node. The harness owns disposable directories and only
// stubs Electron's native picker; production exposes no test-only IPC.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { createRequire } = require('node:module');
const { setTimeout: delay } = require('node:timers/promises');

const root = path.resolve(__dirname, '../../..');
const runtimeModules = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules');
const { _electron } = createRequire(path.join(runtimeModules, '__avalokita_desktop_qa__.cjs'))('playwright');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'avalokita-ade-desktop-'));
const profile = path.join(directory, 'profile');
const alpha = path.join(directory, 'alpha');
const beta = path.join(directory, 'beta');
for (const item of [profile, alpha, beta]) fs.mkdirSync(item);
const environment = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^(CODEX_|AVALOKITA_|ELECTRON_RUN_AS_NODE$)/i.test(name)));
environment.AVALOKITA_PYTHON = fs.readFileSync(path.join(root, 'work/relay-runtime/pyvenv.cfg'), 'utf8').match(/^executable\s*=\s*(.+)$/mi)[1].trim();
const checks = [];
const timings = {};
let electron = null;

async function launch(workspace = null) {
  electron = await _electron.launch({ executablePath: path.join(root, 'node_modules/electron/dist/electron.exe'),
    args: [root, '--ui-test', '--ui-test-profile', profile, ...(workspace ? ['--workspace', workspace] : [])], cwd: root, env: environment, timeout: 60000 });
  const logs = fs.createWriteStream(path.join(directory, `electron-${Date.now()}.log`));
  electron.process().stderr?.on('data', chunk => logs.write(chunk));
  electron.process().once('exit', () => logs.end());
  let page;
  try { page = await electron.firstWindow({ timeout: 60000 }); } catch (error) {
    const diagnostic = await electron.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map(window => ({
      url: window.webContents.getURL(), loading: window.webContents.isLoadingMainFrame(), visible: window.isVisible(), destroyed: window.isDestroyed(),
    }))).catch(failure => ({ error: String(failure) }));
    fs.writeFileSync(path.join(directory, 'window-diagnostic.json'), JSON.stringify(diagnostic, null, 2));
    throw error;
  }
  await page.waitForFunction(() => Boolean(window.avalokitaDesktop));
  const deadline = Date.now() + 30000;
  while (true) {
    const state = await page.evaluate(() => window.avalokitaDesktop.listWorkspaces());
    if (state.activeId) break;
    if (Date.now() >= deadline) throw new Error('Initial workspace did not commit');
    await delay(200);
  }
  return page;
}

async function close() {
  if (electron) { const current = electron; electron = null; await current.close(); }
}

async function request(page, endpoint, body) {
  return page.evaluate(async ({ endpoint, body }) => {
    const response = await fetch(endpoint, { headers: { 'X-Avalokita-Request': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) },
      method: body ? 'POST' : 'GET', body: body ? JSON.stringify(body) : undefined });
    return { status: response.status, body: await response.json() };
  }, { endpoint, body });
}

async function pick(page, value) {
  await electron.evaluate(({ dialog }, selection) => { dialog.showOpenDialog = async () => selection; }, value);
  return page.evaluate(() => window.avalokitaDesktop.chooseWorkspace());
}

(async () => {
  try {
    const a = await launch(alpha);
    const initial = await a.evaluate(() => window.avalokitaDesktop.listWorkspaces());
    const aId = initial.activeId;
    assert.ok(aId);
    assert.equal(initial.entries.length, 1);
    assert.equal(initial.entries[0].path.toLowerCase(), fs.realpathSync.native(alpha).toLowerCase());
    const aUrl = a.url();
    const aDraft = { text: '알파 작업 공간의 한국어 초안', reply: null, pending: null };
    assert.equal((await request(a, '/api/draft', aDraft)).status, 200);
    const waiting = await request(a, '/api/prompt', { text: '격리 시험의 알파 목표', idempotency_key: crypto.randomUUID() });
    assert.equal(waiting.status, 200);
    assert.equal(waiting.body.dispatch.state, 'waiting');
    checks.push('A accepts a durable waiting goal and private draft without native work');

    assert.deepEqual(await pick(a, { canceled: true, filePaths: [] }), { cancelled: true });
    assert.equal((await a.evaluate(() => window.avalokitaDesktop.listWorkspaces())).activeId, aId);
    checks.push('cancelled native picker preserves A');

    let rejectedMissing = false;
    try { await pick(a, { canceled: false, filePaths: [path.join(directory, 'missing')] }); } catch { rejectedMissing = true; }
    assert.equal(rejectedMissing, true);
    assert.equal((await a.evaluate(() => window.avalokitaDesktop.listWorkspaces())).activeId, aId);
    checks.push('missing picker directory fails without replacing active workspace');
    const chooseStarted = Date.now();
    const selected = await pick(a, { canceled: false, filePaths: [beta] });
    timings.chooseBMs = Date.now() - chooseStarted;
    assert.equal(selected.cancelled, false);
    const bId = selected.entry.id;
    const switchStarted = Date.now();
    const switched = await a.evaluate(async ({ id, context }) => window.avalokitaDesktop.activateWorkspace(id, context),
      { id: bId, context: { activeId: aId, generation: initial.generation } });
    timings.activateBMs = Date.now() - switchStarted;
    assert.equal(switched.activeId, bId);
    const pages = electron.windows();
    const b = pages.find(page => page !== a);
    assert.ok(b);
    assert.notEqual(b.url(), aUrl);
    assert.equal((await request(b, '/api/draft')).body.draft, null);
    const bDraft = { text: '베타 작업 공간만의 초안', reply: null, pending: null };
    assert.equal((await request(b, '/api/draft', bDraft)).status, 200);
    const bState = await request(b, '/api/state');
    assert.equal(bState.body.goals.length, 0);
    assert.equal((await request(a, '/api/draft')).body.draft.text, aDraft.text);
    assert.equal((await request(a, '/api/state')).body.goals.some(goal => goal.id === waiting.body.goal.id), true);
    checks.push('actual A/B windows keep separate drafts and goals while A backend stays alive');

    const isolation = await electron.evaluate(async ({ BrowserWindow }) => {
      const windows = BrowserWindow.getAllWindows();
      const first = windows[0];
      const second = windows[1];
      const firstCookies = await first.webContents.session.cookies.get({ name: 'avalokita' });
      const secondCookies = await second.webContents.session.cookies.get({ name: 'avalokita' });
      const cross = await fetch(`${second.webContents.getURL()}api/draft`, { headers: { 'X-Avalokita-Request': '1', Authorization: `Bearer ${firstCookies[0].value}` } });
      return { partitionsDiffer: first.webContents.session !== second.webContents.session,
        capabilitiesDiffer: firstCookies[0].value !== secondCookies[0].value,
        private: firstCookies[0].httpOnly && secondCookies[0].httpOnly, crossStatus: cross.status,
        visibility: windows.map(window => ({ url: window.webContents.getURL(), visible: window.isVisible() })) };
    });
    assert.equal(isolation.partitionsDiffer, true);
    assert.equal(isolation.capabilitiesDiffer, true);
    assert.equal(isolation.private, true);
    assert.equal(isolation.crossStatus, 401);
    assert.equal(isolation.visibility.find(window => window.url === aUrl).visible, false);
    assert.equal(isolation.visibility.find(window => window.url === b.url()).visible, true);
    checks.push('per-workspace partitions and HttpOnly capabilities reject cross-token request');

    const stale = await a.evaluate(async ({ id, generation }) => {
      try { await window.avalokitaDesktop.activateWorkspace(id, { activeId: id, generation }); return false; } catch { return true; }
    }, { id: aId, generation: initial.generation });
    assert.equal(stale, true);
    const missingContext = await b.evaluate(async id => {
      try { await window.avalokitaDesktop.activateWorkspace(id); return false; } catch { return true; }
    }, aId);
    assert.equal(missingContext, true);
    checks.push('inactive sender and omitted context cannot switch');

    const returnStarted = Date.now();
    const back = await b.evaluate(async ({ id, context }) => window.avalokitaDesktop.activateWorkspace(id, context),
      { id: aId, context: { activeId: bId, generation: switched.generation } });
    timings.returnAMs = Date.now() - returnStarted;
    assert.equal(back.activeId, aId);
    assert.equal(a.url(), aUrl);
    assert.equal((await request(a, '/api/draft')).body.draft.text, aDraft.text);
    const beforeRestart = await electron.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map(window => ({ url: window.webContents.getURL() })));
    await close();
    const reopened = await launch();
    const restored = await reopened.evaluate(() => window.avalokitaDesktop.listWorkspaces());
    assert.equal(restored.activeId, aId);
    assert.equal(restored.entries.length, 2);
    assert.equal((await request(reopened, '/api/draft')).body.draft.text, aDraft.text);
    assert.equal((await request(reopened, '/api/state')).body.goals.some(goal => goal.id === waiting.body.goal.id), true);
    checks.push('recent-workspace restart preserves original opaque ID, goal and draft');
    for (const previous of beforeRestart) {
      const response = await fetch(`${previous.url}health`).catch(() => null);
      assert.equal(response, null);
    }
    checks.push('explicit Quit cleaned only the two owned backend children');

    await close();
    const explicitB = await launch(beta);
    assert.equal((await explicitB.evaluate(() => window.avalokitaDesktop.listWorkspaces())).activeId, bId);
    assert.equal((await request(explicitB, '/api/draft')).body.draft.text, bDraft.text);
    checks.push('explicit --workspace wins over most recent selection');
    await close();
    fs.writeFileSync(path.join(directory, 'receipt.json'), JSON.stringify({ ok: true, directory, checks, timings }, null, 2));
    process.stdout.write(JSON.stringify({ ok: true, directory, checks, timings }, null, 2));
  } catch (error) {
    fs.writeFileSync(path.join(directory, 'receipt.json'), JSON.stringify({ ok: false, directory, checks, error: String(error.stack || error) }, null, 2));
    await close().catch(() => {});
    process.stderr.write(`${error.stack || error}\nEvidence: ${directory}\n`);
    process.exitCode = 1;
  }
})();
