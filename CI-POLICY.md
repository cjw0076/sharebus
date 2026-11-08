# CI provenance availability

Artifact attestation is a required package step. GitHub Free, Pro and Team
support it for public repositories; private and internal repositories require
GitHub Enterprise Cloud. Legacy Bronze, Silver and Gold plans do not support it.
See the [official availability rules](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations#who-can-use-this-feature).

An unsupported private repository must fail that step and cannot promote a
release. Local checksum and provenance files do not replace this requirement.
Do not silently skip attestation based on repository visibility. Repository
ownership and plan eligibility remain separate checks. The selected repository
is public `cjw0076/sharebus`; runner and account availability still require an
actual workflow run.

## Candidate package checks

`packages.yml` reuses the native Windows x64, macOS arm64/x64 and Linux x64
runners. Manual and same-repository pull-request runs validate the proposed version,
then require real archive
hash checks, hash-locked wheels, FastMCP and ACP 0.12.1 imports, the patched
Reason build, Electron packaging, and actual packaged backend/GUI smoke.
Tagged builds and release promotion keep the original evidence gates. No gate
is marked accepted merely because a manual candidate artifact was produced.

CPython 3.13.17 archives are exact URL/SHA inputs. The POSIX archives are the
official Astral PBS `20261021` install-only releases; all runtime and dependency
license files remain in the bundle. The four requirements files share one
universal, hash-locked CPython 3.13 resolution constrained to the working
Windows dependency versions plus ACP. A matching native runner must still
select and import its actual wheels; resolving hashes is not an OS test.

Smoke uses the desktop's environment contract, an empty private profile and
the shipped interpreter's `-B -I` FastMCP/ACP imports. It starts the real
backend with Electron's argv/cwd, checks exact readiness/health and the frontend
index, and requires an acknowledged owner shutdown before the GUI test. It
never sends an agent prompt, logs in, exposes ready-file credentials, or
replaces `subprocess.Popen` globally. Package smoke does not prove provider
collaboration, real installation, signing/notarization, or useful user work.

## Pull-request candidate trigger

GitHub requires a `workflow_dispatch` workflow to exist on the default branch.
The `pull_request` trigger targeting `main` allows this new workflow to validate
the proposed integration before a default-branch merge. It uses the PR merge
commit and the same package job, including real per-platform assembly, Reason
build, artifact attestation and packaged backend/GUI smoke. Candidate mode uses
the validate guard, disables automatic signing discovery and keeps publishing
disabled. The tag-only promotion job retains its build and publication gates.

The package job accepts same-repository PRs. Fork PR tokens are read-only and
cannot satisfy required artifact-attestation writes; their source-validation
workflow remains available, and a trusted same-repository candidate is needed
for platform packages. No attestation or smoke step is silently skipped inside
an accepted package job. A scheduled, queued or running job is not a passing
platform result.

References: [workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch),
[PR merge commits and fork permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request),
and [artifact attestation availability](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations).
