# Existing native peer verification

This operator runner uses the product `reason_sessions` helper and the pinned
Reason adapter. It is an **isolated native QA controller**, not the canonical
production Harmony/Sharebus goal service or evidence of composer integration.
Importing it and running `prepare`, `inspect`, or `verify` makes zero native
inputs and zero model calls. `seed` explicitly requests one human-origin input.
Later A → B → A sends must come from the original, identity-verified native hosts.

The parent operator owns all live invocations. Do not run installers, login,
daemon, general Reason mail/MCP fallback, or a second native route for this run.
Keep native approvals; an inbound hold is not permission to bypass them.

## Private operator configuration

Save a bounded JSON spec outside public source. Use the current **existing**
native IDs and metadata, not a child agent's `CODEX_THREAD_ID`. No real IDs,
tokens, private paths, transcripts, or native outputs are hard-coded here.

```json
{
  "workspace": "ABSOLUTE_REAL_WORKSPACE",
  "engine_path": "ABSOLUTE_PINNED_ENGINE",
  "engine_sha256": "64_LOWERCASE_HEX_DIGITS",
  "python_path": "ABSOLUTE_PYTHON_EXECUTABLE",
  "delivery_timeout": 60,
  "deadline_seconds": 900,
  "run_key": "EXPLICIT_ONE_RUN_KEY",
  "initiator": "claude-ceo",
  "reviewer": "codex-ceo",
  "participants": [
    {
      "actor_id": "claude-ceo",
      "role": "CEO",
      "allowed_targets": ["codex-ceo"],
      "process_started_at": "PINNED_NATIVE_PROCESS_CREATION_STAMP",
      "binding": {
        "provider": "claude-code",
        "native_id": "EXISTING_CLAUDE_ID",
        "workspace": "ABSOLUTE_REAL_WORKSPACE",
        "registry_path": "ABSOLUTE_UNLINKED_REGISTRY_FILE",
        "pid": 123,
        "started_at_ms": 1234567890000,
        "socket_path": "EXACT_SELECTED_NATIVE_SOCKET"
      }
    },
    {
      "actor_id": "codex-ceo",
      "role": "CEO",
      "allowed_targets": ["claude-ceo"],
      "binding": {
        "provider": "codex",
        "native_id": "EXISTING_CODEX_THREAD_ID",
        "workspace": "ABSOLUTE_REAL_WORKSPACE",
        "rollout_path": "ABSOLUTE_PHYSICAL_SELECTED_ROLLOUT_FILE",
        "executable": "ABSOLUTE_PINNED_CODEX_EXECUTABLE",
        "executable_sha256": "64_LOWERCASE_HEX_DIGITS"
      }
    }
  ]
}
```

`started_at_ms` is Claude registry `startedAt`. `process_started_at` is the
actual process creation stamp; on Windows it is the FILETIME integer string
matching selected registry `procStart`. These are different fields. The helper
requires the native PID to be in its own ancestor chain, exact current cwd and
registry session/PID/start/socket. Codex requires exact cwd and
`CODEX_THREAD_ID`. These are cooperative host checks, not an OS sandbox against
the same user deliberately running arbitrary code or forging an environment.

The selected Codex rollout is passed only as identity metadata to the engine;
the helper does not read its transcript. Physical unlinked paths are required.
The native send adapter owns the final exact recipient validation.

## Operator commands

```text
python work/reason-native-qa/runner.py prepare --spec PRIVATE_SPEC_JSON
python work/reason-native-qa/runner.py seed --manifest RETURNED_PRIVATE_MANIFEST
python work/reason-native-qa/runner.py inspect --manifest RETURNED_PRIVATE_MANIFEST
python work/reason-native-qa/runner.py verify --manifest RETURNED_PRIVATE_MANIFEST
```

`prepare` writes the QA root, immutable roster and sealed peer permits into
`WORKSPACE/.avalokita/private`. No human-root creation or actor override exists
in the peer CLI. The native prompt carries only its own permit **path**, never
the operator key or another participant's permit. Do not print their contents.

Native peer commands use the exact Python/script/permit paths in their input:

```text
PYTHON SCRIPT --permit OWN_PERMIT get-status
PYTHON SCRIPT --permit OWN_PERMIT send --parent EXACT_INPUT --to REGISTERED_ACTOR --key ONCE_KEY --body SELECTED_JSON
PYTHON SCRIPT --permit OWN_PERMIT reply --parent EXACT_INPUT --key ONCE_KEY --body SELECTED_JSON
PYTHON SCRIPT --permit OWN_PERMIT final --parent EXACT_REPLY_INPUT --key ONCE_KEY --body SELECTED_JSON
```

Use `--body-file ABSOLUTE_SELECTED_FILE` for quoting convenience, with a small
explicitly selected UTF-8 QA file. Private capability/config directories and
credential-shaped text are rejected. Bodies are bounded at 12 KiB; the complete
native envelope is limited to 24,000 UTF-16 units for Windows command-line space.

The reviewer must generate new information (its own nonce and collision
counterexample). The initiator must verify that reply and change its final
policy to the complete SHA-256 digest. Queue/socket acceptance alone fails the
verification. Raw bodies remain private; `verify` exports hashes and booleans.
This demonstrates selected native cooperation, not general product utility.

## Failure, lifetime and production seam

Each root has an immutable deadline and three native-input reservations. No
retry, replacement session, manual operator relay or API-key fallback exists.
Same-key repeats return the original receipt; fresh keys cannot replay an
already answered input. Uncertain native writes block another input until an
exact native response proves processing. Pause/stop prevents new sends; late
selected outcomes remain evidence without renewing authority.

The current host's send/reply observes its original input as `input_processed`;
only a selected final uses `result_completed`. The adapter settles the exact
original Python/Rust attempt with a live identity/causality callback. A fast
native response may precede the outer send's return; a prepared product row
alone is insufficient without the exact durable native-attempt receipt.

The only locally terminated child belongs to `ReasonEngineAdapter`, not the
existing Claude/Codex conversation. Idle inspection invokes no model.

`dispatch_reserved_continuation` is the production integration seam. A future
authenticated `NativeDispatcher` path must supply an **already reserved**
canonical goal/root/job/event/human/parent pair, original roster and current
pause/deadline/budget gate. The gate runs again at the engine's prewrite point.
The QA controller does not replace the canonical service, remove its 403
boundary, or certify GUI integration. Existing canonical completion/callback
logic should own selected final results when that route is wired.

Local verification:

```text
python -m unittest discover -s outputs/avalokita/tests -p test_reason_sessions.py -v
python -m py_compile outputs/avalokita/reason_sessions.py work/reason-native-qa/runner.py
```

These tests use disposable directories, fake adapters and injected host
identity fixtures. Native inputs and model calls are zero.
