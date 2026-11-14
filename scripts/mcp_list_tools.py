#!/usr/bin/env python3
"""Print tools/list from a fresh stdio server using this interpreter and current environment.

Default: readable names in server order. --json: the full MCP response, including schemas.
Only initialize and tools/list are sent; no camera tool or model is called.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def list_tools():
    server = StdioServerParameters(command=sys.executable, args=["server.py"],
                                  cwd=str(Path(__file__).resolve().parents[1]), env=dict(os.environ))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.list_tools()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the complete tools/list JSON response")
    args = parser.parse_args()
    result = asyncio.run(list_tools())
    if args.json:
        print(result.model_dump_json(by_alias=True, indent=2, exclude_none=True))
    else:
        print(f"tools/list ({len(result.tools)} tools)")
        for tool in result.tools:
            print(tool.name)


if __name__ == "__main__":
    main()
