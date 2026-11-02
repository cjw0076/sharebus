"""Offline installed-wheel smoke: no native clients, broker, or model calls."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import venv
import zipfile


PROBE = r'''
import asyncio
from importlib.metadata import distribution, version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import sys
import time

def phase(name):
    print("MCP_SMOKE " + name + " " + str(round(time.monotonic(), 3)), file=sys.stderr, flush=True)

phase("package-import-start")
import sharebus_cli
import sharebus_runtime
phase("package-import-complete")
phase("sdk-import-start")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
phase("sdk-import-complete")

workspace = Path(sys.argv[1])
runtime = Path(find_spec("sharebus_runtime").origin).parent
assert callable(sharebus_runtime.Client), "Original compatibility import must remain available"
assert version("mcp") == "1.30.0"
metadata = distribution("sharebus-local")
assert metadata.version == sys.argv[2]
assert any(item.startswith("mcp==1.30.0") for item in metadata.requires or [])
assert any(item.startswith("certifi>=2026.10.19") for item in metadata.requires or [])
assert tuple(map(int, version("certifi").split("."))) >= (2026, 10, 19)
required = (
    "sharebus.py", "mcp_server.py", "sidecar_registry.py", "SPEC.md", "web/index.html",
    "support_auth.py", "support_controller.py", "support_store.py", "support_native.py", "support_mcp.py",
    "support_diagnostics.py",
    "support_spec.md", "web/support.html",
    "commands/sharebus/SKILL.md", "commands/sharebus/native_context.md",
    "commands/sharebus/session_hook_forwarder.cjs", "templates/SHAREBUS.md",
    "templates/team.json", "third_party/NOTICE.md", "third_party/sources.json",
    "third_party/happy/LICENSE", "third_party/happy/session_hook_forwarder.cjs",
    "third_party/agmsg/LICENSE", "third_party/agmsg/watch-once.sh",
    "web/assets/favicon.svg", "web/assets/PretendardVariable.woff2", "web/assets/Pretendard-LICENSE.txt",
    "web/assets/characters/sharebus-original-artwork.json", "web/assets/characters/sharebus-codex.svg",
    "web/assets/characters/sharebus-claude.svg", "web/assets/characters/sharebus-gemini.svg",
)
assert all((runtime / relative).is_file() for relative in required), "Missing installed resources"

async def list_tools():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", str(runtime / "mcp_server.py"), "--provider", "codex", "--workspace", str(workspace)],
        env=dict(os.environ),
    )
    phase("stdio-enter-start")
    async with stdio_client(params) as (read, write):
        phase("stdio-enter-complete")
        async with ClientSession(read, write) as session:
            phase("session-enter-complete")
            phase("initialize-send")
            await session.initialize()
            phase("initialize-received")
            phase("tools-send")
            result = await session.list_tools()
            phase("tools-received")
            names = {tool.name for tool in result.tools}
            assert {
                "sharebus_join", "sharebus_status", "sharebus_read", "sharebus_send",
                "sharebus_ask_user", "sharebus_publish", "sharebus_copy",
            } <= names, "Missing MCP tool definitions"
            phase("session-exit-start")
        phase("session-exit-complete")
        phase("stdio-exit-start")
    phase("stdio-exit-complete")
    return sorted(names)

names = asyncio.run(asyncio.wait_for(list_tools(), timeout=20))
assert not (workspace / ".sharebus").exists(), "Metadata smoke created a private room"
print(json.dumps({"distribution": metadata.metadata["Name"], "version": metadata.version,
                  "mcp": version("mcp"), "certifi": version("certifi"), "tools": names, "model_calls": 0,
                  "native_inputs": 0, "private_room_created": False}))
'''


GUIDE_PROBE = r'''
import asyncio
from importlib.util import find_spec
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

def phase(name):
    print("GUIDE_SMOKE " + name + " " + str(round(time.monotonic(), 3)), file=sys.stderr, flush=True)

phase("sdk-import-start")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
phase("sdk-import-complete")

runtime = Path(find_spec("sharebus_runtime").origin).parent
workspace = Path(sys.argv[1]).resolve(strict=True)
context = workspace.parent / "synthetic-guide-context.json"
descriptor = os.open(context, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as target:
    json.dump({"runtime":{"service":"sharebus-support-v1", "key":"a" * 64,
                          "nonce":secrets.token_urlsafe(24), "pid":os.getpid(),
                          "url":"http://127.0.0.1:1"}, "job_id":uuid.uuid4().hex,
               "token":secrets.token_urlsafe(32), "deadline":time.time() + 120}, target)

async def list_tools():
    params = StdioServerParameters(command=sys.executable, args=[
        "-I", "-B", str(runtime / "support_mcp.py"), "--context-file", str(context)],
        env=dict(os.environ))
    phase("stdio-enter-start")
    async with stdio_client(params) as (read, write):
        phase("stdio-enter-complete")
        async with ClientSession(read, write) as session:
            phase("initialize-send")
            await session.initialize()
            phase("initialize-received")
            result = await session.list_tools()
            phase("tools-received")
            assert len(result.tools) == 1 and result.tools[0].name == "guide_diagnose"
            schema = result.tools[0].inputSchema
            assert schema.get("properties") == {} and not schema.get("required")
            assert schema.get("additionalProperties") is False
    phase("stdio-exit-complete")
    return [tool.name for tool in result.tools]

names = asyncio.run(asyncio.wait_for(list_tools(), timeout=20))
assert not (workspace / ".sharebus").exists(), "Guide metadata created a private workroom"
print(json.dumps({"guide_tools":names, "guide_tool_invocations":0, "model_calls":0,
                  "native_inputs":0, "private_room_created":False}))
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True, help="Local wheels including all runtime dependencies")
    parser.add_argument("--work-dir", type=Path, help="Fresh private diagnostic directory; preserved on failure")
    args = parser.parse_args()
    expected_version = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    wheelhouse = args.wheelhouse.resolve(strict=True)
    wheels = sorted(wheelhouse.glob("sharebus_local-*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one ShareBus candidate wheel in the local wheelhouse.")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    forbidden = (".sharebus/", "/evidence/", "codex-spritesheet", "clawd-laptop", "clawd-poster", "antigravity-official-mark")
    if any(any(fragment in name for fragment in forbidden) for name in names):
        raise SystemExit("Wheel contains a forbidden private or unlicensed path.")
    if args.work_dir:
        work = args.work_dir.absolute()
        if work.exists():
            raise SystemExit("Diagnostic directory exists; preserve it")
        work.mkdir(parents=True, exist_ok=False)
        context = nullcontext(str(work))
    else:
        context = tempfile.TemporaryDirectory(prefix="sharebus-wheel-smoke-")
    with context as temporary:
        sandbox = Path(temporary)
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        home = sandbox / "home"
        workspace = sandbox / "작업실 with spaces"
        home.mkdir()
        workspace.mkdir()
        environment.update({"HOME": str(home), "USERPROFILE": str(home),
                            "APPDATA": str(home / "roaming"), "LOCALAPPDATA": str(home / "local"),
                            "XDG_CONFIG_HOME": str(home / "config"), "PYTHONIOENCODING": "utf-8",
                            "PYTHONDONTWRITEBYTECODE": "1"})
        virtual = sandbox / "venv"
        venv.EnvBuilder(with_pip=False).create(virtual)
        binary = virtual / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        command = virtual / ("Scripts/sharebus.exe" if os.name == "nt" else "bin/sharebus")
        subprocess.run([os.sys.executable, "-I", "-m", "pip", "--python", str(binary), "install", "--disable-pip-version-check",
                        "--no-index", "--no-cache-dir", "--find-links", str(wheelhouse), str(wheels[0])],
                       cwd=workspace, env=environment, check=True, capture_output=True, text=True,
                       encoding="utf-8", timeout=300)
        help_result = subprocess.run([str(command), "--help"], cwd=workspace, env=environment,
                                     check=True, capture_output=True, text=True, encoding="utf-8", timeout=20)
        if "usage:" not in help_result.stdout.lower():
            raise RuntimeError("Installed console command did not show its help.")
        subprocess.run([str(command), "gui", "--help"], cwd=workspace, env=environment,
                       check=True, capture_output=True, text=True, encoding="utf-8", timeout=20)
        runtime = next(virtual.glob("Lib/site-packages/sharebus_runtime"), None) if os.name == "nt" else next(
            virtual.glob("lib/python*/site-packages/sharebus_runtime"), None)
        if runtime is None:
            raise RuntimeError("Installed guide runtime was not found.")
        for name in ("support_controller.py", "support_mcp.py"):
            help_result = subprocess.run([str(binary), "-B", str(runtime / name), "--help"],
                cwd=workspace, env=environment, check=True, capture_output=True,
                text=True, encoding="utf-8", timeout=20)
            if "usage:" not in help_result.stdout.lower():
                raise RuntimeError("Installed guide did not show its help.")
        if (workspace / ".sharebus").exists() or (home / ".sharebus").exists():
            raise RuntimeError("Guide help changed the isolated profile or workroom.")
        status_result = subprocess.run([str(command), "--workspace", str(workspace), "auto", "status"],
                                       cwd=workspace, env=environment, check=True, capture_output=True,
                                       text=True, encoding="utf-8", timeout=20)
        status = json.loads(status_result.stdout)
        if status.get("auto", {}).get("enabled") is not False or (workspace / ".sharebus").exists():
            raise RuntimeError("Read-only disabled-room status changed the workspace.")
        results = []
        for name, program in (("probe", PROBE), ("guide-probe", GUIDE_PROBE)):
            timeout_error = None
            try:
                probe = subprocess.run([str(binary), "-I", "-B", "-c", program, str(workspace), expected_version],
                                       cwd=workspace, env=environment, check=False, capture_output=True,
                                       text=True, encoding="utf-8", timeout=45)
                probe_stdout, probe_stderr = probe.stdout, probe.stderr
            except subprocess.TimeoutExpired as error:
                timeout_error = error
                probe_stdout = error.stdout or b""
                probe_stderr = error.stderr or b""
                if isinstance(probe_stdout, bytes):
                    probe_stdout = probe_stdout.decode("utf-8", errors="replace")
                if isinstance(probe_stderr, bytes):
                    probe_stderr = probe_stderr.decode("utf-8", errors="replace")
            (sandbox / (name + ".stdout.txt")).write_text(probe_stdout, encoding="utf-8")
            (sandbox / (name + ".stderr.txt")).write_text(probe_stderr, encoding="utf-8")
            print(probe_stderr, file=os.sys.stderr, end="", flush=True)
            if timeout_error is not None:
                raise RuntimeError("Installed MCP probe exceeded 45 seconds; phase output was preserved") from timeout_error
            if probe.returncode:
                raise RuntimeError("Installed MCP probe failed; phase output was preserved")
            results.append(json.loads(probe_stdout))
        if (workspace / ".sharebus").exists() or (home / ".sharebus").exists():
            raise RuntimeError("MCP metadata changed the isolated profile or workroom.")
        result = results[0]
        result["guide"] = results[1]
        result.update({"wheel": wheels[0].name, "cli_help": True, "disabled_status": True,
                       "guide_help": True, "offline_install": True, "isolated_home": True})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
