# Support and evidence

Status date: 2026-10-27. Version `0.1.0a1` is a development alpha source candidate. Packaged end-user installation and a native support guide have separate acceptance work.

| Area | Evidence read during preparation | First-release boundary |
| --- | --- | --- |
| Durable local bus, controls, selected artifacts | Existing source and historical source/API tests | Re-run on the selected public revision |
| Compact native message formatting | Previously accepted scoped tests: file-wise 170 passes / 2 Windows symlink skips; independent 35 passes | Historical proof for three fixed files, not this whole release |
| Codex existing conversation | Historical authenticated bus job/result round trip | One workflow; GUI rendering, restart and all future messages need separate proof |
| Claude | Historical separate-session round trip; business-session receive/claim | Business-session result, preserved role/context and visible continuation incomplete |
| Antigravity | Adapter source and synthetic tests | Existing-app native delivery unverified |
| Policy preferences | Editable team settings and pure planning | Actual model/effort/tool inheritance unverified; plan is not enforcement |
| Developer source/wheel | Finite source and R2/R3 sdist/wheel builds passed; source/archive privacy and notice checks passed | Installed R2 CLI help, GUI help and disabled status passed; its 45-second MCP probe failed, so full package acceptance is pending |
| Copied-server MCP diagnostic | Independent existing-runtime probe initialized, listed eight tools and exited in 8.657 seconds, without tool execution or model input | This is not a fresh installed-wheel result; the failed R2 environment was deleted before its timeout boundary could be diagnosed |
| Windows click launcher | R2 source has folder/provider selection and selected-provider additive setup; focused launcher author tests reported 49 passes; a private R3 portable ZIP was built | Fresh-profile packaged GUI and installed-MCP acceptance remain separate from source publication |
| macOS/Linux | Automatic source/wheel CI candidate | No package, native or GUI support claim until actual run evidence |
| Dedicated guide | Specification in this candidate | Not implemented or connected |

Historical counts above are reported from the inspected development state, not a fresh test run. A queue acceptance, MCP registration, synthetic agent, unit pass or CI matrix does not prove visible native conversation delivery.

Current native alpha QA is separate from those older existing-GUI records. With the default 120-second startup cap, two newly owned Windows subscription CLIs completed a Claude→Codex task, Codex→Claude answer and selected final result. After a clean broker restart, both providers resumed their same native conversation and completed a follow-up task. The controller completed in 148.391 seconds with five managed turns across round-trip and resume, three completed initial jobs and the original root budget preserved. These checks verify exact session identity and completed turns; content recall was not independently verified because the resume prompt supplied the expected answer. The earlier 40-second-cap startup failure remains a separate failed attempt; product RPC/MCP policy was not weakened. This verifies newly owned Windows CLIs. Existing Desktop conversations, their GUI-only tools, Antigravity and other operating systems need their own evidence.

Current public-only guard and collector/CLI tests passed 23 cases. A full-source attempt stopped after 164 passes and one Windows symlink skip at the baseline test's reparse-mock mismatch. Its approved one-case correction passed separately and now tests the actual `lstat` branch before store construction. Remaining file-group regression results are tracked separately. Do not add these runs into a single full-suite pass count.

The intended native mode uses users' existing supported subscribed apps. ShareBus does not guarantee access to provider features, combine subscriptions, bypass quotas, change an app's model or inherit tools into a newly created managed worker. Managed CLI execution is a separate explicitly selected feature.

The future guide should expose useful states in ordinary language: **not connected → permission/reload needed → connected → awaiting reply → replied**, with **paused**, **stopped**, **needs attention** and **unknown** shown when observed. It must not turn installed-app detection into a connected status.
