# Contributing

This is development alpha source. Its finite reviewed runtime selection, public assets and notices are recorded in `SOURCE-MANIFEST.json`. The commands below are contributor workflows; consumer Windows packaging and its acceptance are separate deliverables.

## Developer layout

The wheel keeps the flat runtime under `sharebus_runtime`, mapped from `outputs/sharebus`. The original initializer and its `Client` compatibility import are preserved byte-for-byte: importing the sibling CLI module loads only the standard library, and its runtime imports are delayed. This compatibility name does not promise a public library SDK. Source package-import cases and installed-wheel imports remain tested.

The small `sharebus_cli` console entry launches the installed `sharebus.py` using the same interpreter and a literal argument vector. Running the file preserves its sibling import resolution and `__file__` resource paths without publishing generic top-level modules such as `store` or `adapters`. No shell is involved. The runtime's existing install helper continues to locate its sibling MCP, hooks, templates and web files.

Python 3.11 is the source-language minimum because the runtime uses `tomllib`. The first CI candidate uses Python 3.13 on each OS; that does not establish coverage for every Python version. MCP is pinned to `1.30.0` because the existing runtime explicitly checks that version. Runtime dependencies are MCP and Pydantic; pytest and build are development-only dependencies. This project adds no paid model API client.

## Local developer commands

Use a virtual environment and an isolated test workspace. Do not point these commands at a business room. These are contributor instructions, not the ordinary user's installation flow.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\sharebus.exe --help
```

On macOS/Linux use `python3.13 -m venv .venv`, then `.venv/bin/python` and `.venv/bin/sharebus` for the corresponding commands. Developer dependency installation needs access to the Python package index or an approved local wheelhouse. Running `sharebus` with no arguments or `sharebus gui` opens the GUI backend; `--help` remains a read-only CLI command. Current Windows build/install and protocol evidence, including the failed installed-wheel probe, is separated in [STATUS](docs/STATUS.md). macOS/Linux execution is not established by the workflow template.

## Checks

`templates/ci.yml` is an executable automatic push/PR workflow candidate. Place it at `.github/workflows/ci.yml` when the reviewed standalone source is collected. It uses hosted runners, read-only repository permissions, no provider credentials and no publishing step. Action versions are pinned to the official v4.2.2 checkout and v5.6.0 setup-python commits.

Run the selected-source check before the unit suite. It validates the three accepted compact-message pins and Happy/agmsg source-license hashes, rejects private paths and extracted media, and reports obvious credential patterns by location without printing matches. It is one part of export review, not a guarantee that no secret exists.

```text
python tools/check_release.py
python -m pytest outputs/sharebus/tests tests --deselect=outputs/sharebus/tests/test_sidecar_registry.py::test_development_and_public_allowlists_include_shared_module --deselect=outputs/sharebus/tests/test_sidecar_registry.py::test_public_packaging_filters_support_isolated_import_and_enable
python -m build
python -m pip wheel --wheel-dir .wheelhouse .
python tools/smoke_wheel.py --wheelhouse .wheelhouse --work-dir build/installed-wheel-smoke
```

Two original test functions check Avalokita's development/public Electron packaging, outside this repository's product. They remain intact in the original development tree. Their three parameter cases are explicitly deselected in standalone CI. Installed-wheel resource and import checks cover this repository's packaging instead.

The six import-order cases in `test_actual_enable_and_product_import_orders_do_not_collide` stay enabled. The root `conftest.py` supplies a small synthetic foreign module namespace for those cases; it tests name collisions, not a real Avalokita app. Other runtime tests remain enabled. Never label these synthetic fixtures as native model or GUI evidence.

The wheel smoke creates a temporary virtual environment, installs only from a local wheelhouse, imports the installed containers, invokes the real console command, verifies read-only disabled-room status, and performs actual MCP stdio initialization and tool listing. It never calls an MCP work tool, starts a broker, launches an AI app, sends a model input or reads a live room. It records the actual tool names rather than assuming the legacy seven-name constant is exhaustive.

## Review and reports

Keep changes scoped. Preserve users' room records, host configuration, policy edits and manual stop decisions. Add a regression for behavior changes where it exposes a real failure. Keep failed attempts and successful evidence tied to the same selected source hashes. Source writing and final approval are separate passes.

Public reports should contain version, OS, a short symptom, a redacted error category and reproduction steps in a fresh workspace. Review attachments before sending them. Do not submit tokens, full transcripts, native session IDs, user paths or private delivery receipts.

Packaging references: [PyPA project metadata](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/), [setuptools package configuration](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html), [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax). The MCP smoke follows the installed `1.30.0` SDK contract used by the runtime tests; current SDK major versions must not be substituted without a compatibility change and verification.
