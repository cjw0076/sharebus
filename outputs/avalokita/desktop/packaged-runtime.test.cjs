'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const { packagedRuntime, packagedBackendEnvironment } = require('./packaged-runtime.cjs');

function fixture(t, platform = process.platform) {
  const temporary = fs.realpathSync(os.tmpdir());
  const root = fs.mkdtempSync(path.join(temporary, 'avalokita-runtime-fixture-'));
  t.after(() => {
    const relative = path.relative(temporary, fs.realpathSync(root));
    assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative));
    fs.rmSync(root, { recursive: true, force: true });
  });
  const resources = path.join(root, 'resources');
  const executable = platform === 'win32' ? 'python.exe' : 'bin/python3.13';
  const files = {
    'PYTHON-RUNTIME.json': JSON.stringify({ schema: 'avalokita-python-runtime/1', executable }),
    [`python/${executable}`]: 'fixture Python, never executed',
    [`reason/${platform === 'win32' ? 'reason-network.exe' : 'reason-network'}`]: 'fixture Reason, never executed',
    'avalokita/server.py': '# fixture server, never executed',
    'avalokita/app/dist/index.html': '<html>fixture</html>',
  };
  for (const [relative, content] of Object.entries(files)) {
    const file = path.join(resources, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, content);
  }
  const userData = path.join(root, 'user-data');
  fs.mkdirSync(userData);
  return { root, resources, userData, files };
}

function launcher({ resources, userData, root }, { packaged = true, env = {} } = {}) {
  const app = new EventEmitter();
  app.isPackaged = packaged;
  app.getPath = name => { assert.equal(name, 'userData'); return userData; };
  app.requestSingleInstanceLock = () => false;
  app.quit = () => {};
  app.exit = () => {};
  const dialogs = [];
  const dialog = { showErrorBox: (title, message) => dialogs.push({ title, message }) };
  const launches = [];
  const probes = [];
  const childProcess = {
    spawn(command, args, options) {
      launches.push({ command, args, options });
      return { pid: 12345, exitCode: 0, signalCode: null, once() {} };
    },
    spawnSync(command, args) { probes.push({ command, args }); return { status: 0, stdout: command }; },
  };
  const context = { require: name => name === 'electron' ? { app, dialog } : name === 'node:child_process' ? childProcess : require(name),
    __dirname, module: { exports: {} }, process: { argv: ['Avalokita'], cwd: () => root, resourcesPath: resources, env },
    URL, setTimeout, clearTimeout };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8') + `
    globalThis.launcher = {
      resolve() { serverScript = resolveBackend(); pythonExecutable = resolvePython(); return { serverScript, pythonExecutable }; },
      prepare: prepareRuntime, fail: failApp
    };`, context);
  return { ...context.launcher, launches, probes, dialogs };
}

for (const platform of ['win32', 'darwin', 'linux']) {
  test(`bundle resolver reads only owned resources for ${platform}`, t => {
    const { resources } = fixture(t, platform);
    const runtime = packagedRuntime(resources, platform);
    assert.equal(runtime.reasonSha256, crypto.createHash('sha256').update(fs.readFileSync(runtime.reason)).digest('hex'));
    for (const value of [runtime.python, runtime.reason, runtime.server, runtime.assets]) {
      assert.ok(path.relative(resources, value) && !path.relative(resources, value).startsWith('..'));
    }
    assert.ok(Object.isFrozen(runtime));
  });
}

test('packaged environment fixes metadata and private storage without enabling native work', t => {
  const data = fixture(t);
  const runtime = packagedRuntime(data.resources);
  const before = fs.readdirSync(data.root);
  const inherited = { PATH: 'native-cli-path', PYTHONPATH: 'external', pythonhome: 'external', PYTHONOPTIMIZE: '2',
    AVALOKITA_PYTHON: 'external', AVALOKITA_SERVER: 'external', avalokita_packaged_runtime: '0',
    sharebus_install_backup_root: data.resources, MODEL_PREFERENCE: 'original' };
  const environment = packagedBackendEnvironment(runtime, data.userData, inherited);
  assert.equal(environment.PATH, inherited.PATH);
  assert.equal(environment.MODEL_PREFERENCE, 'original');
  assert.equal(environment.AVALOKITA_BUNDLED_REASON_PATH, runtime.reason);
  assert.equal(environment.AVALOKITA_BUNDLED_REASON_SHA256, runtime.reasonSha256);
  assert.equal(environment.AVALOKITA_PACKAGED_RUNTIME, '1');
  assert.equal(environment.PYTHONDONTWRITEBYTECODE, '1');
  assert.equal(environment.SHAREBUS_INSTALL_BACKUP_ROOT, path.join(data.userData, 'private', 'sharebus-install'));
  for (const key of ['PYTHONPATH', 'pythonhome', 'PYTHONOPTIMIZE', 'AVALOKITA_PYTHON', 'AVALOKITA_SERVER', 'avalokita_packaged_runtime', 'sharebus_install_backup_root']) assert.ok(!(key in environment));
  assert.deepEqual(Object.keys(environment).filter(key => /REASON/.test(key)).sort(), ['AVALOKITA_BUNDLED_REASON_PATH', 'AVALOKITA_BUNDLED_REASON_SHA256']);
  assert.deepEqual(Object.keys(environment).filter(key => /ENABLE|AUTHORITY|DELIVERY/.test(key)), []);
  assert.deepEqual(fs.readdirSync(data.root), before);
  assert.deepEqual(fs.readdirSync(data.userData), []);
  assert.throws(() => packagedBackendEnvironment(runtime, data.resources, {}), /outside/);
  assert.throws(() => packagedBackendEnvironment(runtime, 'relative', {}), /absolute/);
});

test('packaged launcher ignores configured Python/server and sends -B to the bundled backend', async t => {
  const data = fixture(t);
  const harness = launcher(data, { env: { AVALOKITA_PYTHON: 'external', AVALOKITA_SERVER: 'external', PATH: 'native-cli-path' } });
  const selected = harness.resolve();
  assert.equal(selected.pythonExecutable, packagedRuntime(data.resources).python);
  assert.equal(selected.serverScript, packagedRuntime(data.resources).server);
  await assert.rejects(harness.prepare({ id: 'fixture', path: data.root }), /exited/);
  assert.equal(harness.probes.length, 0);
  assert.equal(harness.launches.length, 1);
  const launch = harness.launches[0];
  assert.equal(launch.command, selected.pythonExecutable);
  assert.deepEqual(Array.from(launch.args.slice(0, 2)), ['-B', selected.serverScript]);
  assert.equal(launch.options.env.PATH, 'native-cli-path');
  assert.equal(launch.options.env.AVALOKITA_PACKAGED_RUNTIME, '1');
  assert.equal(launch.options.shell, false);
});

test('development launcher retains configured interpreter and backend', t => {
  const data = fixture(t);
  const python = path.join(data.root, 'development-python.exe');
  const server = path.join(data.root, 'development-server.py');
  fs.writeFileSync(python, 'fixture');
  fs.writeFileSync(server, '# fixture');
  const harness = launcher(data, { packaged: false, env: { AVALOKITA_PYTHON: python, AVALOKITA_SERVER: server } });
  const selected = harness.resolve();
  assert.equal(selected.pythonExecutable, python);
  assert.equal(selected.serverScript, server);
  assert.equal(harness.probes.length, 1);
  assert.equal(harness.probes[0].command, python);
});

test('missing packaged resources fail without probing or falling back to system Python', t => {
  const data = fixture(t);
  fs.unlinkSync(path.join(data.resources, 'python', process.platform === 'win32' ? 'python.exe' : 'bin/python3.13'));
  const harness = launcher(data, { env: { AVALOKITA_PYTHON: process.execPath } });
  assert.throws(() => harness.resolve(), error => {
    assert.equal(error.code, 'AVALOKITA_BUNDLED_RUNTIME_INVALID');
    assert.match(error.message, /Reinstall Avalokita/);
    assert.ok(!error.message.includes(data.root) && !error.message.includes('Python') && !error.message.includes('AVALOKITA_PYTHON'));
    return true;
  });
  assert.deepEqual(harness.probes, []);
  assert.deepEqual(harness.launches, []);
});

test('actual packaged error dialog displays only the allowlisted support code', t => {
  const data = fixture(t);
  fs.unlinkSync(path.join(data.resources, 'PYTHON-RUNTIME.json'));
  const harness = launcher(data);
  let failure;
  try { harness.resolve(); } catch (error) { failure = error; }
  assert.ok(failure);
  harness.fail(failure);
  assert.equal(harness.dialogs[0].title, 'Avalokita');
  assert.equal(harness.dialogs[0].message, failure.message + '\nSupport code: AVALOKITA_BUNDLED_RUNTIME_INVALID');
  assert.ok(!harness.dialogs[0].message.includes(data.root) && !harness.dialogs[0].message.includes('PYTHON'));
  const other = new Error('Fixture failure');
  other.code = data.root + '-private-code';
  harness.fail(other);
  assert.equal(harness.dialogs[1].message, 'Fixture failure');
});

test('actual development error dialog retains its original detailed message', t => {
  const data = fixture(t);
  const harness = launcher(data, { packaged: false });
  const failure = new Error('Development fixture detail');
  failure.code = 'AVALOKITA_BUNDLED_RUNTIME_INVALID';
  harness.fail(failure);
  assert.equal(harness.dialogs[0].message, failure.message);
});

test('runtime metadata traversal and linked resource escapes are rejected', t => {
  const data = fixture(t);
  const metadata = path.join(data.resources, 'PYTHON-RUNTIME.json');
  const original = fs.readFileSync(metadata);
  fs.writeFileSync(metadata, JSON.stringify({ schema: 'avalokita-python-runtime/1', executable: '../external.exe' }));
  assert.throws(() => packagedRuntime(data.resources), /metadata/);
  fs.writeFileSync(metadata, original);
  const outside = path.join(data.root, 'outside-reason');
  fs.renameSync(path.join(data.resources, 'reason'), outside);
  fs.symlinkSync(outside, path.join(data.resources, 'reason'), process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => packagedRuntime(data.resources), /outside/);
});
