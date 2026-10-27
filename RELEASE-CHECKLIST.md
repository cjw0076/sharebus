# Source alpha and packaged-app acceptance

Every claim needs evidence from the selected source. Source alpha publication and distribution of a working consumer package have separate acceptance. This is the preparation snapshot for `0.1.0a1` / `v0.1.0-alpha.1`.

| Item | Acceptance and required proof | Current state |
| --- | --- | --- |
| Source selection | A finite file list and SHA-256 manifest; approved core/compact pins and explicit derivations | Collected; final documentation/test-correction freeze and hash check required |
| License and privacy | Happy/agmsg notices, original SVG MIT notice, font/OFL, final tree and archives exclude private data/app-extracted media | R3 code/archive/license review passed; final delta still checked |
| Independent approval | Reviewer checks selected hashes, failed attempts and exact evidence scope | Core code/API guards/init/license review closed; latest fixture/docs delta reviewed separately |
| Standalone developer wheel | Fresh installation, imports, CLI help/status/resources and actual MCP stdio list | Builds passed; installed R2 help/status passed; MCP45s probe failed and full package acceptance remains open |
| Automatic CI | Windows/macOS/Linux source units and wheel smoke run on push/PR; only the documented foreign-product packaging cases are excluded | Open; workflow candidate authored |
| Original characters | Original SVGs, explicit state and reduced motion; preserved MIT/OFL notices; no excluded media requests | R4 source/asset pins collected; GUI behavior evidence kept separate |
| Windows one-click install | Fresh Windows account with no developer Python: mouse install, folder picker, rollback/uninstall that preserves rooms; final binary checksum | Open; installer not built |
| Three-step user setup | Open the launcher → choose folder/apps and Connect → type work in the authenticated console, without manual IDs/tokens | Launcher source tests exist; fresh-profile packaged GUI acceptance open |
| Dedicated guide | Same native subscribed support conversation opens again, reads matching version/spec, diagnoses and repairs a disposable room with bounded tools; no credential or transcript leakage | Open; integration contract authored |
| Native behavior | For each advertised app: original conversation identity/role/context/tools, received input, authenticated correlated result and actual GUI continuation | Partial historical evidence; final-source run pending |
| Recovery and controls | Paused/stopped/expired/unknown jobs stay controlled across restart; uncertain dispatch never replays | Standalone accepted baseline selected; later Avalokita admission work excluded; final runtime cases remain separate |
| OS support labels | Windows installer and native results separate from Linux/macOS build/import/unit results; unsupported native cases clearly marked experimental | Open |
| Security reporting | Repository private vulnerability reporting enabled and independently opened without submitting a report | Open; repository not created |
| Publish | Clean repository contains only approved sources/notices/docs; release archive and checksums match reviewed revision; no private preparation artifacts | Open; publication not performed |

The intended packaged flow is **open ShareBus → choose folder/apps and Connect → type work in the console**. The dedicated native guide is future work and does not block publication of clearly scoped development source. Developer commands belong in `CONTRIBUTING.md`.
