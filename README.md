# Avalokita

Avalokita is a shared desktop workspace for existing native AI assistants.
Each assistant keeps its own provider subscription, conversation and tools.
Participants exchange selected messages, propose roles, coordinate edits and
review each other's work under the user's goal. The main experience is a chat
composer and a visible council room.

This is a **development candidate**, not a verified three-provider release.
See `release-policy.json` for explicit build and release gates. No Avalokita
provider API key or extra model API billing is required by the intended native
integration. Native assistants still require the user's own supported account.

## Repository

- `outputs/avalokita/`: application source, Electron shell and procedural council.
- `outputs/sharebus/`: local durable collaboration source.
- `vendor/reason-network/`: verified Avalokita native-delivery fork and unchanged upstream licenses.
- `patches/`: two exact LF patches from the upstream pin to the frozen local fork commit.
- `packaging/`: runtime locks and platform package definitions.
- `scripts/`: payload checks, runtime assembly, smoke and provenance.

The existing `outputs/` source paths are retained to preserve Python imports.
The earlier standalone ShareBus CLI, wheel configuration and contributor notes
remain in this repository. Current application checks use `.github/workflows/ci.yml`
and `packages.yml`; the older wheel tools retain their original snapshot-specific
pins and do not establish compatibility with this integrated candidate.
Development recordings, native session IDs, private logs, credentials, extracted
official mascot media and prior build directories are excluded from this tree.

## Development

Use the exact toolchain versions frozen in `release-policy.json`; the present
pins are candidates until compatibility and supported patch levels are reviewed.

```text
npm ci
npm --prefix outputs/avalokita/app ci
npm run test:packaging
npm run build
python scripts/verify_ui_payload.py
```

## Distribution targets

| Target | Candidate formats | Validation status |
| --- | --- | --- |
| Windows x64 | Portable EXE, per-user NSIS installer | New bundled-runtime candidate not yet built |
| macOS Apple Silicon | DMG, ZIP | Native build and original-session delivery unverified |
| macOS Intel | DMG, ZIP | Native build and original-session delivery unverified |
| Linux x64 / GNU libc | AppImage, tar.gz | Native build and original-session delivery unverified |

The installer bundles Python, its locked dependencies and a fixed Reason
sidecar. End users must not install Python. Public CI uses native OS/CPU runners;
it never logs into provider accounts or submits native prompts. Windows ARM,
Linux ARM, Alpine/musl and auto-updates are outside the first package matrix.

## Build and release

1. Freeze runtime archive URLs and SHA256 values in
   `packaging/python-runtime-lock.json`, including its retained license inventory.
2. Generate and review per-platform dependency hash locks. For example, run
   `pip-compile --generate-hashes` against `packaging/requirements-ci.in` on the
   matching OS/CPU and store `packaging/requirements-<platform>.lock`.
3. Integrate the bundled interpreter resolver into the packaged Electron path.
   Source development may still use a development interpreter; a packaged app
   must use its own runtime and must not silently discover a system Python.
4. On a same-repository pull request targeting `main`, the platform workflow
   validates the proposed candidate and runs all native package, attestation and
   smoke steps without publishing. Tagged builds require
   `release_guard.py --mode build`. See `CI-POLICY.md` for the trigger boundary.
5. Independently verify relocated Python/SSL/SQLite/MCP/pywin32 imports, observed
   signature status, the official per-app installation flow, GUI lifecycle and
   the required actual native conversation exchanges. Package startup alone does
   not prove peer delivery. Alpha may be unsigned after these checks; signed
   stable additionally requires Windows publisher signing and macOS notarization.
6. Freeze version, source revision and reviewed evidence. Only a matching `v*`
   tag with completed release gates can reach the promotion job.

Candidate version: `0.2.0-alpha.1`. It is a proposal, not a published release.
Public packages carry SHA256 checksums, source/lock/tool provenance and GitHub
artifact attestations. A source or installer hash is not a claim of bit-for-bit
reproducibility or native delivery. Unsigned alpha must be labelled unsigned;
checksums alone do not provide an operating-system publisher identity. Platform
signature status remains unverified until a real package is inspected.

The source export applies the two pinned patches exactly once, in order, and
compares every vendored byte to local fork commit
`cdc4da74ef522fcaa3cc59c090b401c1fd547762`. The chain starts at upstream
`b3557ff454cd26a04c2a18804ba83ae5db8b119d`, applies the native integration
patch for `837f9892432f8807209d12130dad564de5bcddb0`, then the queue patch for
the final `cdc4da74` fork. Its original macOS icon and all seven literal Rust
include inputs are part of the locked source payload.
CI runs `scripts/verify_reason_source.py` before Cargo and builds that already
patched tree. It never fetches a moving branch or reapplies a fuzzy patch.
See `packaging/reason-source-lock.json` for the upstream/fork/patch pins and all
source hashes. The fork commit is a local provenance identifier; it is not a
claim that a separate fork repository has already been published.

For unsigned alpha, use only the operating system's ordinary per-app decision
flow documented by [Apple](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac)
or [Microsoft](https://learn.microsoft.com/en-us/windows/security/operating-system-security/virus-and-threat-protection/microsoft-defender-smartscreen/).
Release notes must describe the actually tested installer behavior. The app does
not require global protection changes or ship scripts to disable OS safeguards.

## Licenses and contributions

The proposed Avalokita source license is MIT; this choice is still subject to
the release review. Upstream software keeps its own license. See `NOTICE` and
the original retained license texts. Provider names identify compatibility;
the procedural characters are Avalokita artwork, not official endorsements.
