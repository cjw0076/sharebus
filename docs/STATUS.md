# Support and evidence

Status date: 2026-10-28. Version `0.1.0a1` is public **development alpha source**, first published at commit `c38c2a08a62adbe3f9358cd486ade21e0e56b566`. No tag, GitHub release or end-user installer has been published. The dedicated native support guide is not implemented.

| Area | Observed evidence | Still required |
| --- | --- | --- |
| Public source | Finite source manifest, preserved notices and selected source/privacy checks; exact reviewed tree published | Review and pin each subsequent change; source publication is not package acceptance |
| Durable local bus, controls, selected artifacts | Existing source and historical source/API tests | Re-run on the selected public revision |
| Compact native message formatting | Previously accepted scoped tests: file-wise 170 passes / 2 Windows symlink skips; independent 35 passes | Historical proof for three fixed files, not this whole release |
| Owned Windows Claude/Codex CLI round trip | Actual subscribed native task from Claude to Codex, answer back to Claude and selected final result; three initial jobs completed | Existing Desktop/business conversations, visible GUI continuation and inherited host tools |
| Same-conversation resume | Both owned providers resumed their exact native conversation after clean broker close/restart and completed follow-up turns | Other hosts, operating systems and packaged execution |
| Blind content retention | A separate four-turn seed/restart/recall test returned a private random value exactly for both providers, with no hint and no tool action | Do not generalize this owned CLI result to existing Desktop sessions or tool inheritance |
| Codex existing conversation | Historical authenticated bus job/result evidence | Current existing-Desktop lifecycle, visible continuation and preserved tools |
| Claude business conversation | Historical receive/claim evidence; the host required direct human approval | Approval, authenticated result and original role/context/GUI continuity remain incomplete; this hold was not bypassed |
| Antigravity | Adapter source and synthetic tests | Existing-app native delivery unverified |
| Policy preferences | Editable team settings and pure planning | Actual model/effort/tool inheritance unverified; plan is not enforcement |
| Developer source/wheel | R2/R3 distributions built; historical R3 wheel passed fresh offline installed CLI/GUI help, disabled status, resource assertions and MCP initialize/list-eight-tools/clean exit in 86.094 seconds | Earlier R2 MCP45s timeout remains preserved; final integrated CI-source package acceptance is still pending |
| Copied-server MCP diagnostic | Independent existing-runtime probe initialized, listed eight tools and exited in 8.657 seconds, without tool execution or model input | This is not a fresh installed-wheel result; the failed R2 environment was deleted before its timeout boundary could be diagnosed |
| Console and launcher | Folder/provider selection source, scoped tests and partial browser interaction/render evidence | Complete UI interaction and fresh-profile packaged setup acceptance |
| Windows portable bundle | A private R3 bundle was built; its bundled Python/MCP/Tk component created, mapped and destroyed a test window | Exact final-source package, physical user interaction, installer/uninstall and release checksums |
| macOS/Linux | Actual first hosted source/unit CI jobs ran and failed | Passing source/wheel CI, native execution and packaged GUI each need their own evidence |
| Dedicated guide | Integration specification only | Creation/reopen of the same subscribed native support conversation and bounded setup/repair |

Historical counts above are reported from the inspected development state, not a fresh test run. A queue acceptance, MCP registration, synthetic agent, unit pass or CI matrix does not prove visible native conversation delivery.

## Native QA: two separate scenarios

The round-trip scenario used two newly owned Windows subscription CLI conversations with the default 120-second startup cap. Claude sent one task to Codex; Codex returned an answer; Claude produced the selected final result. After clean close/restart, both providers resumed their exact stored native conversation and completed a follow-up turn. This run completed five actual managed turns in 148.391 seconds. Its first three jobs formed the peer round trip.

That scenario's recall question supplied the expected answer. Its content-memory claim was corrected: a matching answer was insufficient evidence of memory. The original raw records and failed earlier 40-second-cap attempt remain preserved.

A later **blind memory scenario** continued only the same two owned conversations. Each received a private random value, completed the seed turn and closed cleanly. After restart, each was asked to return the prior value without the value, answer or hint in the question. Both returned the exact value. All four turns completed with zero observed tool actions; 21 post-run checks passed, including exact native identity, preserved earlier records and owned-resource cleanup. The private value, native identifiers and raw transcripts are not public report material.

These are development-runtime results, not installed-package or existing-Desktop acceptance. No original business CEO conversation or its approval hold was used for these scenarios. Product native RPC/MCP policy and subscription guards were retained; a queue receipt was not counted as completion.

## Actual CI status

The first [three-OS workflow run](https://github.com/cjw0076/sharebus/actions/runs/40509040282) executed on the initial published commit. All source/license/privacy steps passed. Unit failures prevented the later build and installed-wheel smoke steps from running.

| Runner | First unit result |
| --- | --- |
| Ubuntu | 5 failed, 768 passed, 15 skipped, 3 deselected |
| Windows | 27 failed, 760 passed, 1 skipped, 3 deselected |
| macOS | 43 failed, 730 passed, 15 skipped, 3 deselected |

The three deselected cases concern the separately documented foreign-product packaging functions. Fixes for stale test contracts and OS-specific fixtures are under review. This document does not claim an integrated passing rerun. Separate local focused runs and historical test groups must not be added into one full-suite pass count.

Current public-only guard and collector/CLI tests passed 23 cases. A full-source attempt stopped after 164 passes and one Windows symlink skip at the baseline test's reparse-mock mismatch. Its approved one-case correction passed separately and now tests the actual `lstat` branch before store construction. Remaining file-group regression results are tracked separately. Do not add these runs into a single full-suite pass count.

## Account and control limits

The intended native mode uses users' existing supported subscribed apps. ShareBus does not guarantee access to provider features, combine subscriptions, bypass quotas, change an app's model or inherit tools into a newly created managed worker. Managed CLI execution is a separate explicitly selected feature.

The future guide should expose useful states in ordinary language: **not connected → permission/reload needed → connected → awaiting reply → replied**, with **paused**, **stopped**, **needs attention** and **unknown** shown when observed. It must not turn installed-app detection into a connected status.
