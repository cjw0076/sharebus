'use strict';

module.exports = {
  appId: 'org.avalokita.desktop',
  productName: 'Avalokita',
  asar: true,
  // No wildcard over a development outputs directory or app/public.
  files: [
    'outputs/avalokita/desktop/main.cjs',
    'outputs/avalokita/desktop/runtime-contract.cjs',
    'outputs/avalokita/desktop/workspace-registry.cjs',
    'outputs/avalokita/desktop/packaged-runtime.cjs',
    'outputs/avalokita/desktop/preload.cjs',
    'packaging/packaged-runtime.cjs',
    'package.json',
  ],
  extraResources: [
    { from: 'build/runtime/python', to: 'python', filter: ['**/*'] },
    { from: 'build/runtime/PYTHON-RUNTIME.json', to: 'PYTHON-RUNTIME.json' },
    { from: 'packaging/acp-sdk-runtime.json', to: 'ACP-SDK-RUNTIME.json' },
    { from: 'packaging/ACP-SDK-NOTICE.txt', to: 'ACP-SDK-NOTICE.txt' },
    { from: 'packaging/agent-client-protocol.LICENSE', to: 'licenses/agent-client-protocol.LICENSE' },
    { from: 'build/runtime/reason', to: 'reason', filter: ['reason-network', 'reason-network.exe', 'LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE', 'AVALOKITA-CHANGES.md', 'SOURCE-LOCK.json'] },
    { from: 'outputs/avalokita', to: 'avalokita', filter: ['app/dist/**/*', 'server.py', 'bridge.py', 'hosted.py', 'device_discovery.py', 'inventory_runtime.py', 'inventory_sources.py', 'inventory_vault.py', 'preference_learning.py', 'judgment.py', 'THIRD_PARTY.md', 'adapters/REA-LICENSE.txt', 'harmony.py', 'native_dispatch.py', 'avalokita.py', 'mcp_server.py', 'reason_sessions.py', 'adapters/__init__.py', 'adapters/canonical_reason.py', 'adapters/codex_app_tools.py', 'adapters/reason_engine.py', 'HARMONY.md'] },
    { from: 'outputs/sharebus', to: 'sharebus', filter: ['__init__.py', 'sharebus.py', 'store.py', 'discovery.py', 'policy.py', 'install.py', 'daemon.py', 'supervisor.py', 'hosted_runtime.py', 'adapters.py', 'mcp_server.py', 'native_bridge.py', 'codex_bridge.py', 'antigravity_bridge.py', 'antigravity_acp.py', 'sidecar_registry.py', 'launcher.py', 'auto_mode.py', 'auto_enroll.py', 'auto_watch.py', 'hooks_bridge.py', 'templates/team.json', 'templates/SHAREBUS.md', 'commands/sharebus/SKILL.md', 'commands/sharebus/native_context.md', 'commands/sharebus/session_hook_forwarder.cjs', 'third_party/NOTICE.md', 'third_party/happy/LICENSE', 'third_party/agmsg/LICENSE', 'third_party/sources.json', 'third_party/happy/session_hook_forwarder.cjs', 'third_party/agmsg/watch-once.sh', 'web/index.html', 'web/assets/PretendardVariable.woff2', 'web/assets/Pretendard-LICENSE.txt', 'web/assets/favicon.svg', 'web/assets/characters/sharebus-original-artwork.json', 'web/assets/characters/sharebus-codex.svg', 'web/assets/characters/sharebus-claude.svg', 'web/assets/characters/sharebus-gemini.svg'] },
    { from: 'build/runtime/BUILD-INFO.json', to: 'BUILD-INFO.json' },
    { from: 'NOTICE', to: 'NOTICE' },
    { from: 'LICENSE', to: 'LICENSE' },
  ],
  directories: { output: 'release', buildResources: 'packaging/resources' },
  artifactName: 'Avalokita-${version}-${os}-${arch}.${ext}',
  win: { target: ['portable', 'nsis'] },
  portable: { artifactName: 'Avalokita-${version}-win-${arch}-portable.${ext}' },
  nsis: { artifactName: 'Avalokita-${version}-win-${arch}-setup.${ext}', oneClick: false, perMachine: false, allowToChangeInstallationDirectory: true },
  mac: { target: ['dmg', 'zip'], category: 'public.app-category.developer-tools', hardenedRuntime: true },
  linux: { target: ['AppImage', 'tar.gz'], category: 'Development', executableName: 'avalokita' },
  // Candidate builds cannot publish through electron-builder. The separate release
  // promotion job consumes reviewed files after signature/native evidence gates.
  publish: null,
};
