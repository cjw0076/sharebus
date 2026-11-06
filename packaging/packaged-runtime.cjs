'use strict';

const fs = require('node:fs');
const path = require('node:path');

function packagedRuntime(resourcesPath, platform = process.platform) {
  if (!path.isAbsolute(resourcesPath)) throw new Error('Resources path must be absolute');
  if (!['win32', 'darwin', 'linux'].includes(platform)) throw new Error('Unsupported platform');
  const resourcesRoot = fs.realpathSync(resourcesPath);
  const metadata = JSON.parse(fs.readFileSync(path.join(resourcesPath, 'PYTHON-RUNTIME.json'), 'utf8'));
  const validExecutable = platform === 'win32' ? /^python\.exe$/ : /^bin\/python3\.(11|12|13|14)$/;
  if (metadata.schema !== 'avalokita-python-runtime/1' || !validExecutable.test(metadata.executable)) throw new Error('Bundled Python metadata is invalid');
  const reasonName = platform === 'win32' ? 'reason-network.exe' : 'reason-network';
  const pythonRoot = fs.realpathSync(path.join(resourcesPath, 'python'));
  const python = fs.realpathSync(path.join(pythonRoot, metadata.executable));
  const pythonRelative = path.relative(pythonRoot, python);
  if (pythonRelative.startsWith(`..${path.sep}`) || pythonRelative === '..' || path.isAbsolute(pythonRelative)) throw new Error('Bundled Python escapes its runtime');
  const reason = path.join(resourcesPath, 'reason', reasonName);
  const server = path.join(resourcesPath, 'avalokita', 'server.py');
  const assets = path.join(resourcesPath, 'avalokita', 'app', 'dist');
  for (const value of [python, server, reason, path.join(assets, 'index.html')]) {
    const relative = path.relative(resourcesRoot, fs.realpathSync(value));
    if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) throw new Error('Packaged resource escapes the bundle');
    const info = fs.lstatSync(value);
    if (!info.isFile() || info.isSymbolicLink()) throw new Error('Packaged resource is missing or linked');
  }
  return Object.freeze({ python, reason, server, assets });
}

module.exports = { packagedRuntime };
