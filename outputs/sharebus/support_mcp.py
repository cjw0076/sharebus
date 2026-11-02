"""One argument-free, current-human-job diagnostic capability for native help."""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import stat
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


# Native MCP uses Python -I, which intentionally excludes the script directory.
# Load only this shipped sibling by absolute path, never from the target cwd.
_auth_spec = importlib.util.spec_from_file_location("sharebus_support_auth", Path(__file__).with_name("support_auth.py"))
_auth = importlib.util.module_from_spec(_auth_spec)
_auth_spec.loader.exec_module(_auth)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("The help connection could not be verified.")


def read_context(path: Path) -> dict:
    """The controller passes this private file; models cannot choose a target."""
    if not path.is_absolute():
        raise RuntimeError("The help connection could not be verified.")
    try:
        for part in (path, *path.parents):
            info = part.lstat()
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or opened.st_nlink != 1:
                raise ValueError
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ValueError
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"runtime", "job_id", "token", "deadline"}
                or not isinstance(value["token"], str) or not 32 <= len(value["token"]) <= 128
                or not value["token"].isascii() or not isinstance(value["job_id"], str)
                or len(value["job_id"]) != 32 or any(ch not in "0123456789abcdef" for ch in value["job_id"])
                or type(value["deadline"]) not in {int, float} or not math.isfinite(value["deadline"])
                or value["deadline"] <= time.time()):
            raise ValueError
        runtime = value["runtime"]
        if (not isinstance(runtime, dict) or set(runtime) != set(_auth.RUNTIME_FIELDS)
                or runtime["service"] != _auth.SERVICE or type(runtime["pid"]) is not int or runtime["pid"] <= 0
                or not isinstance(runtime["key"], str) or len(runtime["key"]) != 64
                or any(ch not in "0123456789abcdef" for ch in runtime["key"])
                or not isinstance(runtime["nonce"], str) or not 16 <= len(runtime["nonce"]) <= 128):
            raise ValueError
        parsed = urlsplit(runtime["url"])
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
                or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError("The help connection could not be verified.") from None


def create_app(context_file: Path):
    from mcp.server.fastmcp import FastMCP

    class GuideMCP(FastMCP):
        async def list_tools(self):
            tools = await super().list_tools()
            for tool in tools:
                tool.inputSchema["additionalProperties"] = False
            return tools

        async def call_tool(self, name, arguments):
            # FastMCP's default Pydantic argument model ignores extra fields.
            # Enforce the published no-argument capability before tool dispatch.
            if name != "guide_diagnose" or arguments != {}:
                raise ValueError("This help tool accepts no target or other arguments.")
            return await super().call_tool(name, arguments)

    app = GuideMCP("sharebus-guide")
    # Snapshot this job's credentials once. A surviving old MCP process must not
    # gain a later human turn merely because another context file was written.
    context = read_context(context_file)
    opener = build_opener(ProxyHandler({}), NoRedirect())

    @app.tool()
    def guide_diagnose() -> dict:
        """Check this selected workroom's connection and counts, without changing it."""
        if context["deadline"] <= time.time():
            raise RuntimeError("This help request has expired.")
        try:
            status, raw = _auth.signed_exchange(opener, context["runtime"], _auth.token_key(context["token"]),
                "diagnostic:" + context["job_id"], "/api/internal/diagnose", {}, timeout=5, maximum=8192)
            if status != 200:
                raise ValueError
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError, RuntimeError):
            raise RuntimeError("This help request could not check the workroom. No changes were made.") from None
    return app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Private ShareBus help diagnostic bridge")
    parser.add_argument("--context-file", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.getLogger("mcp").setLevel(logging.CRITICAL)
    try:
        app = create_app(args.context_file)
        app.run(transport="stdio")
        return 0
    except (OSError, RuntimeError, ValueError):
        # Never print credential-bearing parser values, HTTP errors or contexts.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
