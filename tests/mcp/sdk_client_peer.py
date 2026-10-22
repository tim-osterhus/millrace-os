"""Independent installed official SDK client; receives fixture jobs on stdin."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

import httpx
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from mcp import ClientSession, StdioServerParameters


async def run(job):
    async with AsyncExitStack() as stack:
        if job["transport"] == "stdio":
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "millrace_os.cli",
                    "mcp",
                    "serve",
                    "--config",
                    job["config"],
                ],
                env=dict(os.environ),
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers={
                        "Host": "mcp.example",
                        "Authorization": "Bearer " + job["token"],
                    },
                    timeout=10,
                    trust_env=False,
                )
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(job["url"], http_client=http)
            )
        session = await stack.enter_async_context(ClientSession(read, write))
        initialized = await session.initialize()
        tools = await session.list_tools()
        results = []
        for call in job["calls"]:
            result = await session.call_tool(call["name"], call["arguments"])
            results.append(result.model_dump(mode="json", exclude_none=True))
        return {
            "pid": os.getpid(),
            "protocol": initialized.protocolVersion,
            "tools": [
                t.model_dump(mode="json", exclude_none=True) for t in tools.tools
            ],
            "results": results,
        }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(json.load(sys.stdin))), sort_keys=True))
