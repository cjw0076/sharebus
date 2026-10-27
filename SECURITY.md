# Security

There is no published supported release yet. This preparation tree has not received a final security approval.

ShareBus stores room data locally and binds its broker to loopback. Session and operator credentials, native connection contexts, message contents, job records and selected artifacts are private data. They must not be committed, attached to public issues, or included in an installer. Loopback and separate agent credentials do not isolate files from another process running as the same OS user.

## Reporting a problem

Before publishing, maintainers must enable this repository's **Security → Report a vulnerability** private reporting channel and verify it is usable. Use that private channel for suspected vulnerabilities. Do not post credentials, raw logs, private workspace paths, conversation contents or reproduction archives in a public issue. A public issue may describe a general symptom without private data.

The local guide should offer a preview of a small, redacted diagnostic report. Nothing is sent to maintainers automatically. If a credential may have been exposed, pause new deliveries and use the affected provider's supported credential or account recovery procedure. Do not try to repair uncertain delivery by replaying the same message.

## Expected boundaries

- Only a real human request grants a new work scope. Peer messages and guide diagnostics cannot create new human authorization.
- Setup and repair preserve existing host settings, conversations, roles, user policies and pause/stop decisions. Unknown delivery remains unresolved until inspected.
- The guide receives public status and bounded participant tools. Operator credentials remain in the local controller; they are never inserted into a model prompt, tool output or support report.
- Shared files are explicit snapshots. Full transcripts, unrelated projects and private reasoning are not setup input.
- Native delivery, model response and visible GUI continuation need separate evidence. A queued request is not a completed job.

See [the source and privacy report](docs/SOURCE-AND-LICENSE.md) for public-export exclusions and [the guide contract](docs/GUIDE-SESSION.md) for repair limits.
