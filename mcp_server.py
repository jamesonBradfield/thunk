#!/usr/bin/env python3
"""mcp_server.py — MCP interface for Thunk v4.

Exposes one tool: Dispatch_Thunk.
The Brain (external large LLM) calls this tool to hand off atomic tasks
to the local thunkd daemon. The daemon runs cheap local workers that bash
their way to a result; this server polls until done and returns the output.

Run as an MCP stdio server (configure in your MCP client's settings):
  python mcp_server.py

Environment:
  THUNK_DIR              Task directory (default: .thunk, resolved relative to cwd)
  THUNK_POLL             Poll interval in seconds (default: 1.0)
  THUNK_DISPATCH_TIMEOUT Max seconds to wait per task (default: 300)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from thunkd import THUNK_DIR, TaskFile, _write_task

POLL_INTERVAL = float(os.getenv("THUNK_POLL", "1.0"))
DISPATCH_TIMEOUT = float(os.getenv("THUNK_DISPATCH_TIMEOUT", "300"))

server = Server("thunk")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="Dispatch_Thunk",
            description=(
                "Dispatch an atomic task to the local Thunk worker daemon. "
                "Workers use bash to explore, modify, and validate the codebase in isolation. "
                "The Brain never touches the codebase directly — only this tool. "
                "On success the worker returns a terse summary; if files were modified it also "
                "returns the git commit hash so you can reference changes by hash instead of "
                "copying file contents into context. "
                "IMPORTANT: keep 'intent' high-level — describe WHAT to do, not HOW. "
                "Never inline code or file contents in intent; the worker reads the files itself. "
                "Failure and timeout messages are prefixed with [FAILED] or [TIMEOUT]."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "The atomic task for the worker to execute.",
                    },
                    "target_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Files or directories the worker is scoped to. "
                            "Be explicit — the worker has no context beyond what you provide."
                        ),
                    },
                    "allowed_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tools available to the worker. "
                            "Defaults to ['Execute_Bash']. "
                            "Add 'Create_Thunk' to allow the worker to spawn sub-workers."
                        ),
                        "default": ["Execute_Bash"],
                    },
                },
                "required": ["intent", "target_files"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "Dispatch_Thunk":
        raise ValueError(f"Unknown tool: {name}")

    intent: str = arguments["intent"]
    target_files: list[str] = arguments.get("target_files", [])
    allowed_tools: list[str] = arguments.get("allowed_tools", ["Execute_Bash"])

    task_id = f"mcp_{uuid.uuid4().hex[:12]}"
    task_path = THUNK_DIR / f"{task_id}.json"
    THUNK_DIR.mkdir(exist_ok=True)

    _write_task(
        TaskFile(
            id=task_id,
            intent=intent,
            target_files=target_files,
            allowed_tools=allowed_tools,
            status="pending",
        ),
        task_path,
    )

    elapsed = 0.0
    while elapsed < DISPATCH_TIMEOUT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
        try:
            task = TaskFile.model_validate_json(task_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if task.status in ("success", "failed"):
            result = task.result or "(no output)"
            if task.status == "failed":
                result = f"[FAILED] {result}"
            return [types.TextContent(type="text", text=result)]

    return [
        types.TextContent(
            type="text",
            text=f"[TIMEOUT] Task {task_id} did not complete within {DISPATCH_TIMEOUT}s. "
                 "Check thunkd logs.",
        )
    ]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
