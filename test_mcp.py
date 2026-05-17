#!/usr/bin/env python3
"""Smoke-test for mcp_server.py.

Spawns mcp_server.py as a subprocess, calls list_tools() then Dispatch_Thunk,
and prints the result. Requires thunkd to already be running.

Usage:
    python test_mcp.py
"""
import asyncio
import re
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER = StdioServerParameters(
    command=".venv/bin/python",
    args=["mcp_server.py"],
    env={
        "THUNK_MODEL": "openai/Qwen3.5-9B-Q6_K.gguf",
        "THUNK_API_BASE": "http://localhost:8080/v1",
        "THUNK_SHELL": "/bin/bash",
        "THUNK_INTENT_COLLAPSE": "500",
        "OPENAI_API_KEY": "sk-no-key-required",
    },
)


FULL_HASH_RE = re.compile(r"\b[0-9a-f]{40}\b")


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
                        "Append a modulo(a, b) function to fixtures/demo_b.py "
                        "that returns a % b. "
                        "Verify by running: cd fixtures && python3 -c "
                        "\"from demo_b import modulo; print(modulo(10, 3))\". "
                        "Commit and return the hash."
                    ),
                    "target_files": ["fixtures/demo_b.py"],
                    "allowed_tools": ["Execute_Bash"],
                },
            )

            print("Result:")
            text = ""
            for block in result.content:
                print(" ", block.text)
                text += block.text

            # Issue #2 regression check: harness must inject a 40-char hash.
            m = FULL_HASH_RE.search(text)
            if m:
                commit = m.group(0)
                print(f"\n[PASS] Full 40-char hash found: {commit}")

                # Issue #1 regression check: only target_files were committed.
                import subprocess
                changed = subprocess.run(
                    ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", commit],
                    capture_output=True, text=True,
                ).stdout.strip().splitlines()
                out_of_scope = [f for f in changed if f != "fixtures/demo_b.py"]
                if out_of_scope:
                    print(f"[FAIL] Commit touched out-of-scope files: {out_of_scope}")
                else:
                    print(f"[PASS] Commit scope clean — only fixtures/demo_b.py touched")
            else:
                print("\n[FAIL] No 40-char hash in result — Issue #2 regression")


asyncio.run(main())
