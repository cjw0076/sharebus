# Source alpha and packaged-app acceptance

Every claim needs evidence from the selected source. Source alpha publication and distribution of a working consumer package have separate acceptance. Updated 2026-10-28: `0.1.0a1` source is public; `v0.1.0-alpha.1` remains a planned, unpublished tag. No release or end-user installer has been published.

| Item | Acceptance and required proof | Current state |
| --- | --- | --- |
| Source selection | A finite file list and SHA-256 manifest; approved core/compact pins and explicit derivations | Initial 87 inputs plus two manifest files published at `c38c2a08a62adbe3f9358cd486ade21e0e56b566`; subsequent CI/docs changes require a new reviewed manifest |
| License and privacy | Happy/agmsg notices, original SVG MIT notice, font/OFL, final tree and archives exclude private data/app-extracted media | Initial source checks and first three hosted source/license/privacy steps passed; later source and archive deltas still require checks |
| Independent approval | Reviewer checks selected hashes, failed attempts and exact evidence scope | Initial selected source reviewed and published; current CI fixture/docs delta is under independent review |
| Standalone developer wheel | Fresh installation, imports, CLI help/status/resources and actual MCP stdio list | Historical R3 wheel passed fresh offline help/status/resources and MCP8-tools clean exit in 86.094s; earlier R2 timeout preserved; final integrated-source package acceptance remains open |
| Automatic CI | Windows/macOS/Linux source units and wheel smoke run on push/PR; only the documented foreign-product packaging cases are excluded | First real run failed on all three OSes; build/wheel-smoke steps were skipped after unit failures; fixes and integrated rerun pending |
| Original characters | Original SVGs, explicit state and reduced motion; preserved MIT/OFL notices; no excluded media requests | R4 source/asset pins collected; GUI behavior evidence kept separate |
| Windows one-click install | Fresh Windows account with no developer Python: mouse install, folder picker, rollback/uninstall that preserves rooms; final binary checksum | Open; private R3 portable bundle and bundled Tk component probe exist, but a consumer installer and its acceptance do not |
| Three-step user setup | Open the launcher → choose folder/apps and Connect → type work in the authenticated console, without manual IDs/tokens | Launcher source tests exist; fresh-profile packaged GUI acceptance open |
| Dedicated guide | Same native subscribed support conversation opens again, reads matching version/spec, diagnoses and repairs a disposable room with bounded tools; no credential or transcript leakage | Open; integration contract authored |
| Owned Windows CLI behavior | Actual Claude↔Codex task/answer/final, exact same native conversation after restart and independent content retention | Verified in isolated development copies; five-turn round-trip/resume scenario and separate four-turn blind-memory scenario with zero tool actions; not installed-package proof |
| Existing Desktop native behavior | For each advertised app: original conversation identity/role/context/tools, received input, authenticated correlated result and actual GUI continuation | Partial historical evidence; existing business Desktop/CEO, host-only tools and Antigravity acceptance remain incomplete |
| Recovery and controls | Paused/stopped/expired/unknown jobs stay controlled across restart; uncertain dispatch never replays | Standalone accepted baseline selected; later Avalokita admission work excluded; final runtime cases remain separate |
| OS support labels | Windows installer and native results separate from Linux/macOS build/import/unit results; unsupported native cases clearly marked experimental | Open |
| Security reporting | Repository private vulnerability reporting enabled and independently opened without submitting a report | GitHub API setting verified enabled; independent opening/form accessibility remains pending; no report submitted |
| Source publication | Clean repository contains only approved sources/notices/docs; no private preparation artifacts | Completed for the initial reviewed source: [public repository](https://github.com/cjw0076/sharebus), exact commit/tree verified |
| Packaged release | Tag, downloadable archive and checksums match the reviewed revision; downloaded package passes its advertised setup flow | Open; no tag, release or installer uploaded |

The intended packaged flow is **open ShareBus → choose folder/apps and Connect → type work in the console**. The dedicated native guide is future work and does not block publication of clearly scoped development source. Developer commands belong in `CONTRIBUTING.md`.
