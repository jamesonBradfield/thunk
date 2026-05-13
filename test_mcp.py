#!/usr/bin/env python3
"""Smoke-test for mcp_server.py.

Spawns mcp_server.py as a subprocess, calls list_tools() then Dispatch_Thunk,
and prints the result. Requires thunkd to already be running.

Usage:
    python test_mcp.py
"""
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = StdioServerParameters(
    command=".venv/bin/python",
    args=["mcp_server.py"],
    env={
        "THUNK_MODEL": "openai/Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
        "THUNK_API_BASE": "http://localhost:8080/v1",
        "THUNK_SHELL": "/bin/bash",
        "THUNK_INTENT_COLLAPSE": "500",
        "OPENAI_API_KEY": "sk-no-key-required",
    },
)


async def main() -> None:
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("=== Tools ===")
            for t in tools.tools:
                print(f"  {t.name}: {t.description[:80]}…")

            print("\n=== Dispatching task ===")
            result = await session.call_tool(
                "Dispatch_Thunk",
                {
                    "intent": (
                        "Append a power(base, exp) function to fixtures/demo_b.py "
                        "that returns base ** exp. "
                        "Verify by running: cd fixtures && python3 -c "
                        "\"from demo_b import power; print(power(2, 8))\". "
                        "Commit and return the hash."
                    ),
                    "target_files": ["fixtures/demo_b.py"],
                    "allowed_tools": ["Execute_Bash"],
                },
            )
            print("Result:")
            for block in result.content:
                print(" ", block.text)


asyncio.run(main())
