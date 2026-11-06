'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { createRequire } = require('node:module');
const { setTimeout: delay } = require('node:timers/promises');

const root = path.resolve(__dirname, '../../..');
const modules = path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules');
const { _electron } = createRequire(path.join(modules, '__avalokita_desktop_qa__.cjs'))('playwright');
const executable = path.join(root, 'node_modules/electron/dist/electron.exe');
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'avalokita-second-instance-'));
const profile = path.join(directory, 'profile');
const alpha = path.join(directory, 'alpha');
const beta = path.join(directory, 'beta');
for (const value of [profile, alpha, beta]) fs.mkdirSync(value);
const environment = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^(CODEX_|AVALOKITA_|ELECTRON_RUN_AS_NODE$)/i.test(name)));
environment.AVALOKITA_PYTHON = fs.readFileSync(path.join(root, 'work/relay-runtime/pyvenv.cfg'), 'utf8').match(/^executable\s*=\s*(.+)$/mi)[1].trim();
const checks = [];
const timings = {};
let electron = null;
let releaseDraft = () => {};

async function until(predicate, description, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await predicate()) return; await delay(100); }
  throw new Error(`Timed out: ${description}`);
}

function secondary(workspace) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [root, '--ui-test', '--ui-test-profile', profile, '--workspace', workspace],
      { cwd: root, env: environment, stdio: ['ignore', 'ignore', 'pipe'], shell: false, windowsHide: true });
    const childLog = fs.createWriteStream(path.join(directory, `secondary-${child.pid}.log`));
    child.stderr.on('data', value => childLog.write(value));
    const timer = setTimeout(() => { child.kill(); reject(new Error(`Owned second instance did not exit (PID ${child.pid})`)); }, 30000);
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', () => { clearTimeout(timer); childLog.end(); resolve(); });
  });
}

(async () => {
  try {
    electron = await _electron.launch({ executablePath: executable, args: [root, '--ui-test', '--ui-test-profile', profile, '--workspace', alpha],
      cwd: root, env: environment, timeout: 60000 });
    const log = fs.createWriteStream(path.join(directory, 'electron.log'));
    electron.process().stderr?.on('data', chunk => log.write(chunk));
    electron.process().once('exit', () => log.end());
    const a = await electron.firstWindow({ timeout: 60000 });
    await a.waitForFunction(() => Boolean(window.avalokitaDesktop?.onWorkspaceActivationRequested));
    let initial;
    await until(async () => { initial = await a.evaluate(() => window.avalokitaDesktop.listWorkspaces()); return initial.activeId; }, 'A initial commit');
    const aId = initial.activeId;
    await a.locator('textarea').waitFor();
    await until(async () => (await a.evaluate(() => document.body.innerText)).includes('alpha'), 'A public workspace data');

    let postStarted = false;
    let postAcknowledged = false;
    const draftGate = new Promise(resolve => { releaseDraft = resolve; });
    await a.route('**/api/draft', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      postStarted = true;
      await draftGate;
      const response = await route.fetch();
      await route.fulfill({ response });
      postAcknowledged = true;
      timings.draftAcknowledgedAt = Date.now();
    });
    const draftA = '아직 저장 응답이 오지 않은 알파의 한국어 초안';
    await a.locator('textarea').fill(draftA);
    const launched = secondary(beta);
    void launched.catch(() => undefined);
    await until(() => postStarted, 'A draft request starts');
    await launched;
    await until(async () => (await a.evaluate(() => window.avalokitaDesktop.listWorkspaces())).entries.length === 2, 'B registered by actual second instance');
    assert.equal((await a.evaluate(() => window.avalokitaDesktop.listWorkspaces())).activeId, aId);
    assert.equal(postAcknowledged, false);
    assert.equal(electron.windows().length, 1);
    releaseDraft();
    await until(async () => (await a.evaluate(() => window.avalokitaDesktop.listWorkspaces())).activeId !== aId, 'second-instance B activation');
    timings.workspaceActivatedAt = Date.now();
    assert.equal(postAcknowledged, true);
    assert.ok(timings.workspaceActivatedAt >= timings.draftAcknowledgedAt);
    const switched = await a.evaluate(() => window.avalokitaDesktop.listWorkspaces());
    const bId = switched.activeId;
    const b = electron.windows().find(page => page !== a);
    assert.ok(b);
    const savedA = await a.evaluate(async () => (await (await fetch('/api/draft', { headers: { 'X-Avalokita-Request': '1' } })).json()).draft);
    assert.equal(savedA.text, draftA);
    checks.push('actual second process requests B through existing renderer and waits for durable A draft ACK');

    await b.locator('textarea').waitFor();
    await until(async () => (await b.evaluate(() => document.body.innerText)).includes('beta'), 'B public workspace data');
    let failures = 0;
    await b.route('**/api/draft', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      failures += 1;
      await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"message":"Owned QA draft save failure"}}' });
    });
    const draftB = '저장이 실패해도 베타 작업실에 유지할 초안';
    await b.locator('textarea').fill(draftB);
    await secondary(alpha);
    await until(async () => (await b.evaluate(() => document.body.innerText)).includes('현재 작업실과 초안을 유지했어요.'), 'failed draft flush notice');
    assert.ok(failures > 0);
    assert.equal((await b.evaluate(() => window.avalokitaDesktop.listWorkspaces())).activeId, bId);
    assert.equal(await b.locator('textarea').inputValue(), draftB);
    assert.equal(electron.windows().length, 2);
    checks.push('failed B draft save rejects actual second-instance A switch and retains B text/runtime');

    await electron.close();
    electron = null;
    const receipt = { ok: true, directory, checks, timings };
    fs.writeFileSync(path.join(directory, 'receipt.json'), JSON.stringify(receipt, null, 2));
    process.stdout.write(JSON.stringify(receipt, null, 2));
  } catch (error) {
    releaseDraft();
    fs.writeFileSync(path.join(directory, 'receipt.json'), JSON.stringify({ ok: false, directory, checks, error: String(error.stack || error) }, null, 2));
    if (electron) await electron.close().catch(() => {});
    process.stderr.write(`${error.stack || error}\nEvidence: ${directory}\n`);
    process.exitCode = 1;
  }
})();
