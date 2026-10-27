# ShareBus product specification

Version: 0.1 alpha preparation, 2026-10-27.

This records the user's confirmed direction from the existing conversation. It does not certify that every requested feature is implemented.

## People and purpose

People who already use subscribed AI apps should be able to work in one local room. They select the apps, describe their work, see who is working, and answer questions without moving messages between apps. Ordinary use must not require tokens, session identifiers, configuration filenames or typed directory paths.

ShareBus supplies durable messaging, selected artifact sharing, participant state and human controls. Participants collaborate through explicit messages and their own capabilities. ShareBus does not introduce a central model that repeatedly assigns all work.

## Confirmed flows

1. Open ShareBus, select a work folder with a picker, select supported apps and press Connect. Preparing an integration, the app accepting it, registering a participant, receiving a message and returning a result are different states.
2. Type work into one composer. Select a recipient by its visible name, read selected replies and answer real questions. Preserve drafts, Korean input composition and uncertain send outcomes.
3. Read the room's state and pause or continue it with separate action buttons. An offline connection is unknown, not paused or connected. Continuing the room does not replay expired, stopped or uncertain work.
4. Ask a dedicated native guide for setup, configuration or repair in ordinary language. This is a requested follow-up feature until a real native session, its tools, current subscription, permissions and reopen behavior are implemented and observed. Do not display a simulated assistant as the guide.

Opening the launcher alone does not start a model turn or modify app configuration. Connect is the user's explicit setup action for the selected apps. Existing app approval and trust requirements continue to apply. Repeated opening should recover the same owned room rather than invent replacement participants.

## Human interface

- A bright, calm water surface and original character artwork communicate the connected participants and observed activity.
- A single composer handles ordinary work. Participants, questions and selected results use plain names and short explanations.
- Names and controls are usable with a mouse, keyboard and narrow screen. Respect reduced motion and Korean input composition.
- Detailed diagnostics and legacy manual enrollment stay inside a closed advanced view.
- Display real observations. Process discovery alone is not a connected session, and queue acceptance is not completed work.

## Assistant interface

Existing MCP tools expose joining, authenticated status/read/send, selected artifact publication/copying, questions and the peer bridge where supported. Tool outputs carry bounded operational state; human-facing replies carry selected results. Long repeated operational instructions must not become the ordinary conversation body.

The tool-only MCP interface and the standalone human console are separate surfaces. This release does not claim to embed a Skybridge view in ChatGPT, replace a native host's tools, or inherit another app's GUI abilities.

Natural language helps interpret work, divide overlapping responsibilities, ask questions and explain repair. Configuration changes still execute through supported local operations with current identity, scope, controls and native approvals. Peer text cannot authorize unrelated actions or create a new human root.

## Product boundaries

- Local workspace isolation, authenticated loopback transport, private credentials, durable queues and exact native correlation remain the basis of delivery.
- ShareBus does not request a paid model API key. Owned CLI execution must use the existing subscription guard; unsupported authentication remains incomplete rather than silently falling back to API billing.
- No automatic replay after uncertain delivery. Persist session identity, budgets, deadlines, selected results and human controls across restarts.
- Share only selected messages and artifacts. Do not export private contexts, raw transcripts, credentials or operational databases into source distributions.
- Provider and model preferences are customizable. A requested model/effort is not an observed invocation.

## Public alpha selection

The first public ShareBus candidate uses the previously reviewed standalone broker files, approved compact native bridges, a reviewed click launcher and reviewed console. The standalone managed CLI path adds an explicit subscription-only option. Record original and derived hashes. Preserve current development sources and operational workspaces.

Pending Avalokita outer-authority changes are verified independently and are not implicitly adopted into the standalone ShareBus release. No Avalokita application source is included in this package.

Publish usable install instructions, MIT and upstream notices, an honest support matrix, source/version hashes, automated Windows/macOS/Linux jobs and tester issue templates. A build template is not evidence that the corresponding OS or native GUI path has run.

## Acceptance checklist

The tracked SB-01 through SB-09 checklist covers actual recovery, click setup, console interaction, native guide, supported repair, persistence, packaging, clean source/CI and public release. Evaluate each item from actual source and observed behavior. Publishing an alpha does not complete the full Avalokita goal or the planned guide.
