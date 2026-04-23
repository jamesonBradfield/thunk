#!/usr/bin/env python3
"""harness.py — Headless, file-based AI agent router.

Environment variables:
  ARIADNE_MODEL        LiteLLM model string (default: openai/local)
  ARIADNE_API_BASE     Base URL for the LLM API (default: http://localhost:8080/v1)
  ARIADNE_POLL         Watcher poll interval in seconds (default: 1.0)
  ARIADNE_CHILD_POLL   Child-task poll interval in seconds (default: 0.5)
  OPENAI_API_KEY       API key — not required for llama-server (uses dummy value)

Drop a JSON file into .ariadne/ with this shape and the harness picks it up:
  {"id": "task_1", "intent": "...", "allowed_tools": [...], "status": "pending"}
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Literal, Optional

import litellm
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ARIADNE_DIR = Path(".ariadne")
MODEL = os.getenv("ARIADNE_MODEL", "openai/local")
API_BASE = os.getenv("ARIADNE_API_BASE", "http://localhost:8080/v1")
POLL_INTERVAL = float(os.getenv("ARIADNE_POLL", "1.0"))
CHILD_POLL_INTERVAL = float(os.getenv("ARIADNE_CHILD_POLL", "0.5"))

# ---------------------------------------------------------------------------
# Task schema
# ---------------------------------------------------------------------------


class TaskFile(BaseModel):
    id: str
    intent: str
    allowed_tools: list[str]
    status: Literal["pending", "running", "success", "failed"]
    result: Optional[str] = None


# ---------------------------------------------------------------------------
# Tool stubs  (replace bodies with real logic as needed)
# ---------------------------------------------------------------------------


def _extract_ast(file_path: str) -> str:
    return (
        f"[mock AST] {file_path}: Module > FunctionDef('main') > Return > Constant(0)"
    )


def _ast_splice(node_id: str, replacement: str) -> str:
    return f"[mock splice] node '{node_id}' replaced with: {replacement!r}"


def _query_lsp(symbol: str) -> str:
    return f"[mock LSP] '{symbol}': defined at src/main.py:42, inferred type: str"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict] = {
    "ExtractAST": {
        "type": "function",
        "function": {
            "name": "ExtractAST",
            "description": "Parse a source file and return its AST as a structured string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the source file to parse.",
                    }
                },
                "required": ["file_path"],
            },
        },
    },
    "ASTSplice": {
        "type": "function",
        "function": {
            "name": "ASTSplice",
            "description": "Replace an AST node identified by node_id with a source snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {
                        "type": "string",
                        "description": "Identifier of the AST node to replace.",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Source code snippet to splice in.",
                    },
                },
                "required": ["node_id", "replacement"],
            },
        },
    },
    "QueryLSP": {
        "type": "function",
        "function": {
            "name": "QueryLSP",
            "description": "Query the language server for hover info or definition of a symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Symbol name to look up.",
                    }
                },
                "required": ["symbol"],
            },
        },
    },
    "Delegate_Task": {
        "type": "function",
        "function": {
            "name": "Delegate_Task",
            "description": (
                "Spawn a child agent to handle a sub-task. "
                "Blocks until the child finishes and returns its result. "
                "Use this to decompose complex work into isolated sub-agents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "The goal for the child agent to achieve.",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tool names the child agent is allowed to use.",
                    },
                },
                "required": ["intent", "tools"],
            },
        },
    },
}

# Maps tool names to callable stubs (Delegate_Task is handled separately)
_LOCAL_FNS: dict[str, Any] = {
    "ExtractAST": lambda a: _extract_ast(**a),
    "ASTSplice": lambda a: _ast_splice(**a),
    "QueryLSP": lambda a: _query_lsp(**a),
}

# ---------------------------------------------------------------------------
# In-flight tracker — prevents double-dispatch of the same task ID
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_in_flight: set[str] = set()


def _claim(task_id: str) -> bool:
    with _lock:
        if task_id in _in_flight:
            return False
        _in_flight.add(task_id)
        return True


def _release(task_id: str) -> None:
    with _lock:
        _in_flight.discard(task_id)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _read_task(path: Path) -> Optional[TaskFile]:
    try:
        return TaskFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, json.JSONDecodeError, OSError) as exc:
        print(f"[harness] WARN: cannot read {path.name}: {exc}")
        return None


def _write_task(task: TaskFile, path: Path) -> None:
    # Write to a sibling .tmp then rename so readers never see a partial file
    tmp = path.with_suffix(".tmp")
    tmp.write_text(task.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def _patch_task(path: Path, **updates: Any) -> Optional[TaskFile]:
    task = _read_task(path)
    if task is None:
        return None
    updated = task.model_copy(update=updates)
    _write_task(updated, path)
    return updated


# ---------------------------------------------------------------------------
# Delegate_Task interceptor
# ---------------------------------------------------------------------------


def _delegate(intent: str, tools: list[str], parent_id: str) -> str:
    """Write a child task file and block until it reaches a terminal status."""
    child_id = f"child_{parent_id}_{uuid.uuid4().hex[:8]}"
    child_path = ARIADNE_DIR / f"{child_id}.json"
    _write_task(
        TaskFile(id=child_id, intent=intent, allowed_tools=tools, status="pending"),
        child_path,
    )
    print(f"[harness] [{parent_id}] delegated → '{child_id}': {intent!r}")

    while True:
        time.sleep(CHILD_POLL_INTERVAL)
        child = _read_task(child_path)
        if child is None:
            return "[error] child task file disappeared before completion"
        if child.status in ("success", "failed"):
            icon = "✓" if child.status == "success" else "✗"
            snippet = (child.result or "")[:80]
            print(f"[harness] [{parent_id}] child '{child_id}' {icon}: {snippet!r}")
            return child.result or ""


# ---------------------------------------------------------------------------
# LiteLLM message normaliser
# ---------------------------------------------------------------------------


def _msg_to_dict(msg: Any) -> dict:
    """Flatten a LiteLLM Message object into a plain dict for the messages list."""
    d: dict[str, Any] = {"role": msg.role}
    if msg.content is not None:
        d["content"] = msg.content
    tool_calls = getattr(msg, "tool_calls", None) or []
    if tool_calls:
        d["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return d


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _execute(path: Path) -> None:
    task = _patch_task(path, status="running")
    if task is None:
        return

    print(f"[harness] [{task.id}] starting: {task.intent!r}")

    schemas = [
        TOOL_SCHEMAS[name] for name in task.allowed_tools if name in TOOL_SCHEMAS
    ]

    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "You are an isolated, stateless worker. "
                "Use your tools to achieve the intent, then reply with your final answer as plain text."
            ),
        },
        {
            "role": "user",
            "content": f"Achieve this intent: {task.intent}",
        },
    ]

    try:
        while True:
            kwargs: dict[str, Any] = {
                "model": MODEL,
                "api_base": API_BASE,
                "api_key": os.getenv("OPENAI_API_KEY", "sk-no-key-required"),
                "messages": messages,
            }
            if schemas:
                kwargs["tools"] = schemas
                kwargs["tool_choice"] = "auto"

            response = litellm.completion(**kwargs)
            msg = response.choices[0].message
            messages.append(_msg_to_dict(msg))

            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                final = (msg.content or "").strip()
                _patch_task(path, status="success", result=final)
                print(f"[harness] [{task.id}] success: {final[:80]!r}")
                return

            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args: dict = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name == "Delegate_Task":
                    result = _delegate(
                        intent=fn_args.get("intent", ""),
                        tools=fn_args.get("tools", []),
                        parent_id=task.id,
                    )
                elif fn_name in _LOCAL_FNS:
                    try:
                        result = _LOCAL_FNS[fn_name](fn_args)
                    except Exception as exc:
                        result = f"[tool error] {fn_name}: {exc}"
                else:
                    result = f"[error] unknown tool '{fn_name}'"

                print(
                    f"[harness] [{task.id}] tool {fn_name}({fn_args}) "
                    f"→ {str(result)[:60]!r}"
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": str(result),
                    }
                )

    except Exception as exc:
        tb = traceback.format_exc()
        _patch_task(path, status="failed", result=f"[harness error]\n{exc}\n\n{tb}")
        print(f"[harness] [{task.id}] FAILED: {exc}")


# ---------------------------------------------------------------------------
# Worker thread wrapper
# ---------------------------------------------------------------------------


def _worker(path: Path, task_id: str) -> None:
    try:
        _execute(path)
    finally:
        _release(task_id)


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


def _find_pending() -> list[Path]:
    pending = []
    for path in sorted(ARIADNE_DIR.glob("*.json")):
        task = _read_task(path)
        if task and task.status == "pending":
            pending.append(path)
    return pending


def run() -> None:
    ARIADNE_DIR.mkdir(exist_ok=True)
    print(f"[harness] watching {ARIADNE_DIR.resolve()}  model={MODEL}  api_base={API_BASE}")
    try:
        while True:
            for path in _find_pending():
                task = _read_task(path)
                # Re-read status: another thread may have claimed it since _find_pending
                if task is None or task.status != "pending":
                    continue
                if _claim(task.id):
                    threading.Thread(
                        target=_worker,
                        args=(path, task.id),
                        daemon=True,
                    ).start()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n[harness] shutting down")


if __name__ == "__main__":
    run()
