# Dedicated native guide: integration contract

This is required implementation work, not an existing support service. It defines the local help path for users who should not need developer knowledge.

## Visible flow

The installer owns a **도움받기 / 안내 대화 열기** button. On first use, the user selects a currently installed, signed-in native host through a picker. The local controller uses that host's supported create/open conversation interface and stores the returned binding privately. Later clicks open the same support conversation. The user types one ordinary request, for example “이 폴더에서 AI들이 함께 일하게 연결해 줘” or “Codex가 답을 안 해. 확인하고 고쳐 줘.”

Do not ask the user to paste a native session ID, private file path, token, database file or full conversation. If several rooms or conversations match, display names and folders in a picker. If the host cannot create/open or verify the selected native conversation, show the unsupported capability and keep that setup incomplete. Do not substitute a hidden model session.

## Existing account and specification

The guide runs in a dedicated conversation within a supported native CLI/desktop host using the user's existing signed-in subscription. It reads the bundled version-matched ShareBus specification, installation recipe, repair boundaries, support matrix and redacted local status. It does not request a paid API key or copy another conversation's private context. The provider's own authentication, permission dialogs and usage limits continue to apply.

The local binding is keyed by host, exact support conversation, room and specification hash. It is not a public setting or report field. On reopen, verify the actual native identity and host capability before delivering context; a stale binding must lead to the picker or guided recovery. A stored ID or successful create call alone does not prove that the user sees a working guide.

The runtime already has policy and skill templates, public participant status, guarded bootstrap, configuration preparation/application and exact native delivery adapters. The new guide should compose these supported interfaces. It should not reimplement credential issuance or add an unrestricted administrative MCP tool.

## Local operations and authority

| Guide step | Existing foundation | Additional work |
| --- | --- | --- |
| Explain and inspect | Bundled docs/skill plus public status | Versioned guide context and useful plain-language status summary |
| Select room/apps | Exact workspace/provider/native identity checks | Folder and app/conversation pickers; no manual identifiers |
| Prepare connection | `install.prepare`, configuration baseline hashes and backup receipts | UI summary of the concrete changes and host-required trust/reload steps |
| Apply setup/repair | Guarded local install/apply and explicit room controls | Narrow controller action bound to the user's setup/repair request; operator token stays local |
| Verify | Existing MCP and native result correlation | One selected disposable verification task; actual response and visible support conversation recorded |
| Continue later | Private exact native binding | Open the same guide, refresh spec/status, preserve prior pauses and unknown dispatch |

Setup/repair authorization comes from the user's current request. The controller executes only the bounded action selected by that request. The model receives a redacted result, never the operator credential or its file. Participant actions use their existing private host binding. A peer message, stale guide instruction, scheduled check or model suggestion cannot create a new human root, resume stopped work, enlarge scope or authorize replay.

A normal repair first inspects supported public status, validates host/workspace identity and configuration baselines, repairs only the owned integration, and verifies the result in the chosen room. User edits are preserved and backups remain private. Never launch another copy of an active conversation, scrape unrelated transcripts, kill an unknown PID, log raw hook payloads or retry an uncertain native delivery automatically.

## Completion proof

Use a fresh test account/profile and disposable room. Record first setup, a missing integration, stale binding, unsupported host, denied host permission, manual pause/stop, unknown dispatch, app restart and guide reopen. For each case retain source/spec hashes, observed host, selected action and public outcome. Inspect the prompt/tool outputs and diagnostic preview for credential or transcript exposure. A successful read-only health probe alone does not complete setup.

No guide may claim universal native tool inheritance or complete three-app/three-OS support. Each advertised combination needs its own actual native and GUI evidence. An unsupported combination remains clearly experimental.
