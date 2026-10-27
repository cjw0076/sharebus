# Public source, licenses and privacy

Status date: 2026-10-27. The development alpha source has been collected into finite candidates. Source distributions and wheels were built; selected source and archive privacy/license checks passed. An installed R2 wheel passed CLI help, GUI-command help and disabled-room status before its MCP probe timed out at 45 seconds. A private Windows portable ZIP was also built. These results do not complete packaged-app acceptance; development source publication is assessed separately.

## Selection

Collect a finite list of reviewed first-party Python modules, their standalone tests, the public web source, managed policy/skill templates, the small launcher, public documentation and the two preserved MIT adaptations. Capture SHA-256 values after selection. Never copy the development workspace recursively.

Four authority-sensitive runtime modules (`store.py`, `supervisor.py`, `hosted_runtime.py`, `daemon.py`) contain in-progress changes in the development tree. The standalone alpha explicitly selects their accepted earlier revision without modifying those working files. It does not claim the later Avalokita admission changes are fixed or included.

The original initializer and flat script layout are retained inside the wheel. Two reviewed public-only derivations require subscription authentication: the supervisor passes `subscription_only=True` for its managed adapters, and the separate owned Claude delivery proxy passes the same guard to its base adapter. The supervisor's attached-native arguments remain unchanged; the proxy's owned transport is explicitly guarded. Both original and derived hashes are recorded. First-party code uses MIT; the font keeps its separate OFL notice.

One public test derivation replaces a mock of an unused helper with a synthetic `lstat` reparse attribute, exercising the selected baseline's actual ancestor-check branch. It asserts that no store constructor runs after that refusal. This changes one test case and leaves production guards and original working files intact.

## Upstream MIT code and notices

| Upstream | Pinned revision | Actual reuse and retained material |
| --- | --- | --- |
| [Happy Coder](https://github.com/slopus/happy/tree/b0fc933dd423ee562207d1788c65b4b9cc4960d1) | `b0fc933dd423ee562207d1788c65b4b9cc4960d1` | Stdin collection and one-shot lifecycle forwarding structure adapted in `commands/sharebus/session_hook_forwarder.cjs`; preserve original forwarder and [MIT notice](https://github.com/slopus/happy/blob/b0fc933dd423ee562207d1788c65b4b9cc4960d1/LICENSE), Copyright (c) 2026 Happy Coder Contributors |
| [agmsg](https://github.com/fujibee/agmsg/tree/e3a93251628127a2ea2c7e416bd7db86bf29f3ef) | `e3a93251628127a2ea2c7e416bd7db86bf29f3ef` | Deadline-before-bootstrap pattern adapted in `auto_watch.py`; preserve original watcher as provenance and [MIT notice](https://github.com/fujibee/agmsg/blob/e3a93251628127a2ea2c7e416bd7db86bf29f3ef/LICENSE), Copyright (c) 2026 fujibee |

The preserved agmsg Bash file is provenance, not a launched ShareBus component. `third_party/NOTICE.md`, `sources.json`, both original sources and both full license texts are included. All four fixed upstream hashes matched the selected files and archive review. MIT attribution remains with upstream; the top-level code license does not replace those notices.

Projects inspected only as comparisons are not dependencies or integrated code. Do not claim Gas Town, hcom, agent-bridge or cross-agent_mcp implementation was included when it was not. Changes to the dependency set require a fresh license and runtime review.

## Public characters

The current development web page references Codex app-extracted sprites, a Claude app video/frame and an Antigravity official mark. Provenance alone does not prove redistribution permission. Those media and their raw development manifest are excluded from public inputs.

The public R4 web copy uses three ShareBus-authored SVG characters and `sharebus-original-artwork.json`, which contains their MIT notice. The original favicon is included. The unmodified Pretendard font and full `Pretendard-LICENSE.txt` are included under OFL-1.1, with fixed hashes. Wheel distribution metadata therefore declares `MIT AND OFL-1.1`. The legacy character manifest and all app-extracted media are excluded. Original development media remain unchanged.

Optional user-local asset discovery would be a separate feature. It cannot copy extracted files into a published repository, wheel, installer, screenshot or support archive without a separate permission basis. It is not required for first-release setup.

## Exclusions and review

Exclude every room's `.sharebus`, SQLite databases and WAL/SHM files, runtime metadata, operator/participant tokens, private contexts/bindings/watchers, transcripts, jobs, selected private artifacts, credentials, raw debug receipts, screenshots containing private data, developer absolute paths, user home/configuration files, `.omc` records, old evidence and local preparation reports. A `.gitignore` is a backstop; it is not the export allowlist or a secret audit.

The final review covers the assembled source tree, Git index/history if initialized, wheel/sdist and installer independently. Inspect detected patterns without logging matched secrets. Any intentional synthetic credential fixture must be clearly fake and reviewed; do not broadly skip tests or raw data directories to make the scan pass. Publish only selected redacted evidence summaries.

The ordinary guide receives version, supported capability status and redacted diagnostics. It must never expose native/session/file IDs for the user to enter, prompt for private token files, or include complete original conversations in its context.
