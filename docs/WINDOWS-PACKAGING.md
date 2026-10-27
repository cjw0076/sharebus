# Windows portable bundle and backend contract

Target version: `0.1.0a1`, GitHub tag `v0.1.0-alpha.1`. This is a local packaging candidate until the final source, built archive and fresh-profile GUI behavior pass independent review.

The lightweight candidate uses existing local CPython 3.11.9 material with `pythonw`, the standard library, `_tkinter`, Tcl/Tk and their notices. It includes only the exact installed MCP 1.30.0 dependency closure recorded from package metadata. It never copies a user profile, virtual environment wholesale, global site-packages directory, authentication files or compiled Python caches. The selected runtime's version and security maintenance must be assessed before distribution; finding files is not a clean-machine launch result.

`lock_windows_runtime.py` produces a proposed finite file/hash list without copying material. Review its package closure and notices, then approve the exact lock bytes. `build_windows_bundle.py` verifies each byte hash, uses only listed files, preserves runtime/dependency licenses and emits a relative-path bundle manifest. A local C# compiler builds a small `winexe` bootstrap. The ZIP is portable: extract all files and open `ShareBus.exe`; no global Python or PATH setup is required by design. This is separate from an MSI installer or code signing.

```text
ShareBus/
  ShareBus.exe
  PYTHON-RUNTIME.json
  BUNDLE-MANIFEST.json
  RUNTIME-LICENSES.json
  LICENSE
  python/pythonw.exe
  python/python.exe
  python/python311._pth
  python/Lib/...
  python/DLLs/...
  python/tcl/...
  sharebus/launcher.py
  sharebus/<reviewed runtime, public web, templates and MIT notices>
```

The isolated `_pth` permits only bundled standard-library, dependency and ShareBus script paths. The startup path cannot import user site packages. The `PYTHON-RUNTIME.json` schema retains the existing install helper's compatibility name; its explicit product is ShareBus, not an Avalokita integration claim.

## Launcher handoff

The bootstrap launches the exact bundled `pythonw.exe -I -B sharebus/launcher.py` with no user arguments, using the bundle directory as working directory. It does not invoke a shell, select accounts or send a model input. The runtime launcher owns the visible folder/app picker.

- `launcher.py` with no arguments opens the GUI. `--help` remains a read-only contributor command. `sharebus gui` must dispatch to the same launcher.
- Opening the GUI performs no writes, broker start, model input or host reconfiguration. The explicit **연결** action prepares/applies additive integration only for selected providers, enables room auto participation while preserving existing pauses/suspensions, starts with `agents=none`, and opens the authenticated loopback console.
- File, room and app selection use pickers. No token, private path, session ID or file ID entry is part of the user flow.
- Configuration installation preserves user edits, verifies its baseline and retains private backups outside bundled resources. Host trust/reload steps remain visible when required.
- The native guide session is future work and is not the launcher or an automatic managed model session. Managed approval forwarding remains an alpha limitation.

## Required tests

Use the copied runtime for metadata and tkinter import probes, then actual MCP stdio initialization/tool listing in an isolated disposable workspace. Verify `pythonw` and a real folder picker in a fresh user profile separately; import success does not prove GUI behavior. Test spaces/Korean paths, cancel/no writes, selected-provider install, authenticated console opening, preserved pause/stop, repeated launch and missing-material errors. No native AI input is required for packaging acceptance.

The build scripts keep compiler stdout/stderr and exact manifest hashes locally. Runtime and source archives require separate privacy/license review. Do not publish raw preparation logs.

Primary contracts: [CPython Windows module search and application-local distributions](https://docs.python.org/3.11/using/windows.html), [Microsoft compiler output types](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/compiler-options/output).
