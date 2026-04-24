#!/usr/bin/env python3
"""thunkd.py — Headless, file-based AI agent daemon.

Environment variables:
  THUNK_MODEL        LiteLLM model string (default: openai/local)
  THUNK_API_BASE     Base URL for the LLM API (default: http://localhost:8080/v1)
  THUNK_POLL         Watcher poll interval in seconds (default: 1.0)
  THUNK_CHILD_POLL   Child-task poll interval in seconds (default: 0.5)
  OPENAI_API_KEY     API key — not required for llama-server (uses dummy value)
  THUNK_LSP_CMD      Executable for the LSP-MCP server (optional; enables real QueryLSP)
  THUNK_LSP_ARGS     Space-separated args for the LSP-MCP server (optional)

Drop a JSON file into .thunk/ with this shape and the daemon picks it up:
  {"id": "task_1", "intent": "...", "allowed_tools": [...], "status": "pending"}
"""

from __future__ import annotations

import importlib
import json
import os
import sys
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

THUNK_DIR = Path(".thunk")
MODEL = os.getenv("THUNK_MODEL", "openai/local")
API_BASE = os.getenv("THUNK_API_BASE", "http://localhost:8080/v1")
POLL_INTERVAL = float(os.getenv("THUNK_POLL", "1.0"))
CHILD_POLL_INTERVAL = float(os.getenv("THUNK_CHILD_POLL", "0.5"))

# ---------------------------------------------------------------------------
# Tool loader
# ---------------------------------------------------------------------------

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".rs": "rust",
    ".js": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".c": "c",
    ".cpp": "cpp",
}


def _load_tools() -> dict[str, Any]:
    """Import ../ariadne primitives and return a name→callable map.

    Returns an empty dict (triggering mock fallback) if the package is absent
    or any required dependency is missing.
    """
    ariadne_root = Path(__file__).resolve().parent.parent / "ariadne"
    if not ariadne_root.exists():
        return {}
    pkg_dir = str(ariadne_root)
    if pkg_dir not in sys.path:
        sys.path.insert(0, pkg_dir)

    try:
        from ariadne.primitives import ASTSplice as _ASTSplice
        from ariadne.primitives import ExtractAST as _ExtractAST
        from ariadne.lsp import LSPManager as _LSPManager
    except ImportError as exc:
        print(f"[thunkd] WARN: ariadne import failed — {exc}")
        return {}

    # Cache ExtractAST instances by language (parser init is expensive)
    _ast_cache: dict[str, _ExtractAST] = {}

    def _get_extractor(lang: str) -> Optional[_ExtractAST]:
        if lang in _ast_cache:
            return _ast_cache[lang]
        try:
            pkg = importlib.import_module(f"tree_sitter_{lang}")
            lang_ptr = pkg.language() if hasattr(pkg, "language") else getattr(pkg, lang)()
            inst = _ExtractAST(lang_ptr)
            _ast_cache[lang] = inst
            return inst
        except Exception as exc:
            print(f"[thunkd] WARN: cannot load tree-sitter-{lang}: {exc}")
            return None

    def extract_ast(
        filepath: str, query_string: str, capture_name: str = "node"
    ) -> str:
        ext = Path(filepath).suffix.lower()
        lang = _EXT_TO_LANG.get(ext)
        if not lang:
            return f"[error] unsupported extension '{ext}'"
        extractor = _get_extractor(lang)
        if extractor is None:
            return f"[error] tree-sitter-{lang} not installed"
        status, results = extractor.tick(
            {"filepath": filepath, "query_string": query_string, "capture_name": capture_name},
            None,
        )
        return f"[{status}]\n" + "\n---\n".join(results)

    _splice = _ASTSplice()

    def ast_splice(filepath: str, edits: list) -> str:
        status, out = _splice.tick({"filepath": filepath, "edits": edits}, None)
        return f"[{status}] {out}"

    lsp_cmd = os.getenv("THUNK_LSP_CMD")
    _lsp: Optional[_LSPManager] = None
    if lsp_cmd:
        lsp_args = os.getenv("THUNK_LSP_ARGS", "").split()
        _lsp = _LSPManager(lsp_cmd, lsp_args)

    def query_lsp(filepath: str, line: int, character: int) -> str:
        if _lsp is None:
            return (
                f"[mock LSP] hover at {filepath}:{line}:{character}"
                " — set THUNK_LSP_CMD to enable real LSP queries"
            )
        return _lsp.get_hover(filepath, line, character)

    return {
        "ExtractAST": extract_ast,
        "ASTSplice": ast_splice,
        "QueryLSP": query_lsp,
    }

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
# Mock stubs — used only when ../ariadne cannot be imported
# Signatures match the real tool interfaces so they document the expected API.
# ---------------------------------------------------------------------------


def _extract_ast(filepath: str, query_string: str, capture_name: str = "node") -> str:
    return f"[mock AST] {filepath} query={query_string!r} capture={capture_name!r}"


def _ast_splice(filepath: str, edits: list) -> str:
    return f"[mock splice] {filepath}: {len(edits)} edit(s) applied"


def _query_lsp(filepath: str, line: int, character: int) -> str:
    return f"[mock LSP] hover at {filepath}:{line}:{character}"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: dict[str, dict] = {
    "ExtractAST": {
        "type": "function",
        "function": {
            "name": "ExtractAST",
            "description": (
                "Run a tree-sitter query against a source file and return matching nodes. "
                "Use this to extract functions, classes, imports, or any syntactic construct."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the source file to parse.",
                    },
                    "query_string": {
                        "type": "string",
                        "description": (
                            "Tree-sitter S-expression query, e.g. "
                            "'(function_definition name: (identifier) @node)'."
                        ),
                    },
                    "capture_name": {
                        "type": "string",
                        "description": "Capture name to collect (default: 'node').",
                    },
                },
                "required": ["filepath", "query_string"],
            },
        },
    },
    "ASTSplice": {
        "type": "function",
        "function": {
            "name": "ASTSplice",
            "description": (
                "Apply surgical byte-range edits to a source file. "
                "Use ExtractAST first to locate start_byte/end_byte for each target node."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the source file to edit.",
                    },
                    "edits": {
                        "type": "array",
                        "description": "List of edits to apply, sorted bottom-up by start_byte.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_byte": {"type": "integer"},
                                "end_byte": {"type": "integer"},
                                "new_code": {"type": "string"},
                            },
                            "required": ["start_byte", "end_byte", "new_code"],
                        },
                    },
                },
                "required": ["filepath", "edits"],
            },
        },
    },
    "QueryLSP": {
        "type": "function",
        "function": {
            "name": "QueryLSP",
            "description": (
                "Get hover information (type, docs, signature) from the language server "
                "at a specific position in a file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the source file.",
                    },
                    "line": {
                        "type": "integer",
                        "description": "0-indexed line number.",
                    },
                    "character": {
                        "type": "integer",
                        "description": "0-indexed character offset on the line.",
                    },
                },
                "required": ["filepath", "line", "character"],
            },
        },
    },
    "Create_Thunk": {
        "type": "function",
        "function": {
            "name": "Create_Thunk",
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

# Maps tool names to callables (Create_Thunk is handled separately).
# Prefer real ariadne implementations; fall back to mocks if the package is absent.
_ext_fns = _load_tools()
_LOCAL_FNS: dict[str, Any] = (
    {name: (lambda a, fn=fn: fn(**a)) for name, fn in _ext_fns.items()}
    if _ext_fns
    else {
        "ExtractAST": lambda a: _extract_ast(**a),
        "ASTSplice": lambda a: _ast_splice(**a),
        "QueryLSP": lambda a: _query_lsp(**a),
    }
)

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
        print(f"[thunkd] WARN: cannot read {path.name}: {exc}")
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
# Create_Thunk interceptor
# ---------------------------------------------------------------------------


def _create_thunk(intent: str, tools: list[str], parent_id: str) -> str:
    """Write a child task file and block until it reaches a terminal status."""
    child_id = f"child_{parent_id}_{uuid.uuid4().hex[:8]}"
    child_path = THUNK_DIR / f"{child_id}.json"
    _write_task(
        TaskFile(id=child_id, intent=intent, allowed_tools=tools, status="pending"),
        child_path,
    )
    print(f"[thunkd] [{parent_id}] thunk created → '{child_id}': {intent!r}")

    while True:
        time.sleep(CHILD_POLL_INTERVAL)
        child = _read_task(child_path)
        if child is None:
            return "[error] child task file disappeared before completion"
        if child.status in ("success", "failed"):
            icon = "✓" if child.status == "success" else "✗"
            snippet = (child.result or "")[:80]
            print(f"[thunkd] [{parent_id}] child '{child_id}' {icon}: {snippet!r}")
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

    print(f"[thunkd] [{task.id}] starting: {task.intent!r}")

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
                print(f"[thunkd] [{task.id}] success: {final[:80]!r}")
                return

            for tc in tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args: dict = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                if fn_name == "Create_Thunk":
                    result = _create_thunk(
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
                    f"[thunkd] [{task.id}] tool {fn_name}({fn_args}) "
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
        _patch_task(path, status="failed", result=f"[thunkd error]\n{exc}\n\n{tb}")
        print(f"[thunkd] [{task.id}] FAILED: {exc}")


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
    for path in sorted(THUNK_DIR.glob("*.json")):
        task = _read_task(path)
        if task and task.status == "pending":
            pending.append(path)
    return pending


def run() -> None:
    THUNK_DIR.mkdir(exist_ok=True)
    tool_source = "ariadne package" if _ext_fns else "mock stubs"
    print(f"[thunkd] watching {THUNK_DIR.resolve()}  model={MODEL}  api_base={API_BASE}")
    print(f"[thunkd] tools loaded from: {tool_source}  {list(_LOCAL_FNS.keys())}")
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
        print("\n[thunkd] shutting down")


if __name__ == "__main__":
    run()
