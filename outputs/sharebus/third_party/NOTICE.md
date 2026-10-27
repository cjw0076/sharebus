# Pinned MIT adaptations

## Happy Coder

- Repository: https://github.com/slopus/happy
- Commit: `b0fc933dd423ee562207d1788c65b4b9cc4960d1`
- Source: `packages/happy-cli/scripts/session_hook_forwarder.cjs`.
- Copyright (c) 2026 Happy Coder Contributors. Original MIT text is in `happy/LICENSE`.
- The original forwarder is preserved in `happy/session_hook_forwarder.cjs`.
- Actual adaptation: `../commands/sharebus/session_hook_forwarder.cjs` retains stdin chunk collection and one-shot local lifecycle forwarding. Changes: a 1 MiB limit, selected official metadata fields, a shell-free Python argument vector instead of unauthenticated HTTP forwarding, managed-session no-op, an 8-second bound, and no raw payload logging. `../install.py` uses this adapter for global bootstrap hooks when Node is available; otherwise it invokes the same bounded Python helper directly.

## agmsg

- Repository: https://github.com/fujibee/agmsg
- Commit: `e3a93251628127a2ea2c7e416bd7db86bf29f3ef`
- Source: `scripts/drivers/types/codex/watch-once.sh`.
- Copyright (c) 2026 fujibee. Original MIT text is in `agmsg/LICENSE`.
- The original one-shot watcher is preserved in `agmsg/watch-once.sh` for provenance; Sharebus does not execute that Bash file or load agmsg's storage/bridge.
- Actual pattern adaptation: `../auto_watch.py` captures the lifetime deadline before bootstrap/startup and clips each polling wait to the remaining lifetime. It reads pending metadata without claiming inbox work. Sharebus supplies its own exact native binding, authenticated loopback API, pause/cancel controls, process lock and host-specific continuation output. Its Python implementation does not copy upstream worker launching, bridge rearming or model calls.

`sources.json` records pinned raw URLs and SHA256 values for the preserved source and license files, verified byte-for-byte against those downloads. No Gas Town source is included. mcp_agent_mail and ntm code are not included because their additional provider-specific license conditions were outside this reuse scope.
