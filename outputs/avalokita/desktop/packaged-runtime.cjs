'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function contained(root, value) {
  const relative = path.relative(root, value);
  return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

function ownedFile(root, relative) {
  const value = path.join(root, relative);
  const info = fs.lstatSync(value);
  const actual = fs.realpathSync(value);
  if (!info.isFile() || info.isSymbolicLink() || !contained(root, actual)) {
    throw new Error('Packaged resource is missing, linked, or outside its bundle');
  }
  return actual;
}

function packagedRuntime(resourcesPath, platform = process.platform) {
  if (typeof resourcesPath !== 'string' || !path.isAbsolute(resourcesPath)) throw new Error('Resources path must be absolute');
  if (!['win32', 'darwin', 'linux'].includes(platform)) throw new Error('Unsupported platform');
  const resources = fs.realpathSync(resourcesPath);
  const metadata = JSON.parse(fs.readFileSync(ownedFile(resources, 'PYTHON-RUNTIME.json'), 'utf8'));
  const executable = platform === 'win32' ? /^python\.exe$/ : /^bin\/python3\.(11|12|13|14)$/;
  if (!metadata || Array.isArray(metadata) || metadata.schema !== 'avalokita-python-runtime/1'
      || typeof metadata.executable !== 'string' || !executable.test(metadata.executable)) {
    throw new Error('Bundled Python metadata is invalid');
  }
  const pythonRoot = fs.realpathSync(path.join(resources, 'python'));
  if (!contained(resources, pythonRoot)) throw new Error('Bundled Python escapes its resources');
  const python = ownedFile(pythonRoot, metadata.executable);
  const reason = ownedFile(resources, path.join('reason', platform === 'win32' ? 'reason-network.exe' : 'reason-network'));
  const server = ownedFile(resources, path.join('avalokita', 'server.py'));
  const index = ownedFile(resources, path.join('avalokita', 'app', 'dist', 'index.html'));
  const reasonSha256 = crypto.createHash('sha256').update(fs.readFileSync(reason)).digest('hex');
  return Object.freeze({ python, reason, reasonSha256, server, assets: path.dirname(index), resources });
}

function packagedBackendEnvironment(runtime, userData, inherited = process.env) {
  if (typeof userData !== 'string' || !path.isAbsolute(userData)) throw new Error('User data path must be absolute');
  const dataRoot = fs.existsSync(userData) ? fs.realpathSync(userData) : path.resolve(userData);
  if (contained(runtime.resources, dataRoot)) throw new Error('Private state must be outside packaged resources');
  const environment = { ...inherited };
  for (const key of Object.keys(environment)) {
    if (key.toUpperCase().startsWith('PYTHON') || ['AVALOKITA_PYTHON', 'AVALOKITA_SERVER', 'AVALOKITA_PACKAGED_RUNTIME',
      'AVALOKITA_RESOURCES_ROOT', 'AVALOKITA_BUNDLED_REASON_PATH', 'AVALOKITA_BUNDLED_REASON_SHA256',
      'SHAREBUS_INSTALL_BACKUP_ROOT'].includes(key.toUpperCase())) delete environment[key];
  }
  return { ...environment, PYTHONUNBUFFERED: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1',
    AVALOKITA_PACKAGED_RUNTIME: '1', AVALOKITA_RESOURCES_ROOT: runtime.resources,
    AVALOKITA_BUNDLED_REASON_PATH: runtime.reason, AVALOKITA_BUNDLED_REASON_SHA256: runtime.reasonSha256,
    SHAREBUS_INSTALL_BACKUP_ROOT: path.join(dataRoot, 'private', 'sharebus-install') };
}

module.exports = { packagedRuntime, packagedBackendEnvironment };
