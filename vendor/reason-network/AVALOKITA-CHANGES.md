# Avalokita local delivery additions

Base: Reason Network 0.12.11, commit `b3557ff454cd26a04c2a18804ba83ae5db8b119d`.
Original source, LICENSE-MIT, LICENSE-APACHE and NOTICE are retained unchanged.

Changes made on 2026-10-24:

- Added the private `avalokita-deliver` stdio command before migration/setup/daemon initialization.
- Reused Claude Code's native socket framing behind a pre-input authorization callback.
- Added exact selected Claude registry / Codex session metadata and executable-pin checks.
- Added a durable, one-attempt native boundary with no queue/hook/network fallback or automatic replay.
- Kept native input submission separate from a verified model response or completed goal.

This command does not use or claim integration of the upstream team directory,
network daemon, MCP server, remote transport, installer, sign-in, or auto-updater.
