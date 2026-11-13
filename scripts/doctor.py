#!/usr/bin/env python3
"""Check the real MCP transport without submitting a paid model task."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def check(base):
    params = StdioServerParameters(command=str(base / "bin/start-mcp.sh"), args=[])
    async with Client(params) as client:
        listing = await client.list_tools()
        names = {tool.name for tool in listing.tools}
        expected = {"codex_health", "codex_submit", "codex_status", "codex_result",
                    "codex_continue", "codex_cancel", "codex_list_jobs"}
        if not expected.issubset(names):
            raise RuntimeError("Missing MCP tools: " + str(expected - names))
        result = await client.call_tool("codex_health", {})
        if result.is_error:
            raise RuntimeError(str(result.content))
        health = result.structured_content
        print(json.dumps({"mcp_connected": True, "tool_count": len(names), **health},
                         ensure_ascii=False, indent=2))
        return 0 if health["authenticated"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", default=str(Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "antigravity-codex"))
    args = parser.parse_args()
    sys.exit(asyncio.run(asyncio.wait_for(check(Path(args.install_dir).expanduser().resolve()), 60)))
