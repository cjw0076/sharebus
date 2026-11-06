# Avalokita third-party sources and assets

This record distinguishes software used by the app, analysis tools, preserved asset references, and ideas considered during design. A referenced upstream project is not automatically part of the Avalokita runtime. Research receipts are maintained separately from runtime and native-delivery evidence.

## REA

[REA](https://github.com/morluto/rea/tree/c38f96fa693de115a47498d467169b8c69f0a426) was used as a local static-analysis tool. The inspected source revision is `c38f96fa693de115a47498d467169b8c69f0a426` with an [MIT license](https://github.com/morluto/rea/blob/c38f96fa693de115a47498d467169b8c69f0a426/LICENSE). Separately, the workspace-local `rea-agents` 6.7.0 npm package had its tarball SHA-256 and npm integrity checked; its `gitHead` is absent, so equivalence to that source revision is not claimed. The retained [license](adapters/REA-LICENSE.txt), [analysis report](adapters/REA-ANALYSIS.md), and [inspection manifest](adapters/inspection-manifest.json) record provenance and exact scope.

The actual tool command was `analyze-javascript-application`. It parsed selected, inert JavaScript files copied from installed application ASAR archives with source offsets and hashes: Antigravity (25,135 text bytes), Codex (6,909), and Claude (4,536); all three scoped analyses exited 0 with zero parse failures. The full-archive attempts are also retained: direct inspection failed closed on two native companion integrity contradictions and an initial Antigravity argument-encoding error, so scoped copies were analyzed instead. These are selected-file static results, not complete application analyses. The tool did not launch the applications, inspect session databases or credentials, contact native endpoints, invoke an LLM, or send messages. Static observations do not establish runtime reachability, enforcement, same-session continuity, or GUI delivery.

## Orca

[Orca](https://github.com/stablyai/orca/tree/8a4ef49a4db6b73b076c779671235029ae6c4b96) is a design reference for discovering and working with local CLI agents. The inspected revision is `8a4ef49a4db6b73b076c779671235029ae6c4b96`, with an [MIT license](https://github.com/stablyai/orca/blob/8a4ef49a4db6b73b076c779671235029ae6c4b96/LICENSE), copyright 2026 Lovecast Inc.

Its CLI-agent, terminal, and worktree approach is a reference. Avalokita's intended shared-workspace peer collaboration does not establish that Orca supports wrapping existing desktop conversations. No Orca runtime fork or copied implementation is claimed by this document.

## Sharebus

Avalokita reuses the adjacent local [Sharebus implementation](../sharebus/README.md) through a bridge. Its authentication, event/job storage, native adapters, source receipts, and delivery limits remain separate concerns. Existing selected third-party adaptations and retained licenses are listed in Sharebus's [AUTO_SOURCES.md](../sharebus/AUTO_SOURCES.md).

## Visual assets

| Material | Classification and provenance |
| --- | --- |
| [Council reference image](app/public/assets/generated/avalokita-council-reference-v1.png) | One generated concept image. The [generation manifest](design/council-reference-manifest-v1.json) retains its prompt, original path, hash, and limitations. It is not a working WebGL scene or evidence of native messaging. |
| [Character source manifest](app/public/assets/characters/manifest.json) | Preserves the origins and hashes of copied Codex, Clawd, and Antigravity reference assets. A bundled asset is not a new assertion of redistribution rights. |
| Three-dimensional characters | Avalokita interpretations of the reference motifs. The Gemini/Antigravity star companion is an original expression and is not claimed as an official mascot. |
| [Pretendard Variable](app/public/assets/fonts/PretendardVariable.woff2) | Locally bundled font; its [license](app/public/assets/fonts/Pretendard-LICENSE.txt) is retained. |

## Application dependencies

The runtime's actual dependency manifest and lockfile determine packaged versions. Dependency notices and the packaged distribution scope remain to be reconciled after the implementation is fixed. Do not interpret this source inventory as distribution or legal approval.
