"""Optional stdio MCP tools for an already authenticated original native job.

No installer or activation step is needed by the HTTP/CLI path. Set exact job
identity in environment if this optional server is configured by the host.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from avalokita import peer_operation


def execute(operation: str, arguments: dict):
    required = {name: os.environ.get(name) for name in
                ("AVALOKITA_WORKSPACE", "AVALOKITA_ACTOR", "AVALOKITA_NATIVE_SESSION_ID", "AVALOKITA_ROOT_ID")}
    if not all(required.values()):
        raise ValueError("MCP host must supply the exact existing native job identity; no human authority is created")
    return peer_operation(Path(required["AVALOKITA_WORKSPACE"]), actor=required["AVALOKITA_ACTOR"],
                          native_session_id=required["AVALOKITA_NATIVE_SESSION_ID"], root_id=required["AVALOKITA_ROOT_ID"],
                          operation=operation, arguments=arguments, url=os.environ.get("AVALOKITA_URL"))


def main():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise SystemExit("Optional MCP package is unavailable; use the built-in Avalokita CLI helper") from None
    mcp = FastMCP("Avalokita selected peer collaboration")

    @mcp.tool()
    def avalokita_peer(operation: str, arguments_json: str = "{}") -> str:
        """Publish/capture/claim/handoff selected output within this original human root."""
        return json.dumps(execute(operation, json.loads(arguments_json)), ensure_ascii=False)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
