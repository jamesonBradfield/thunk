#!/usr/bin/env python3
"""thunkd.py — Headless automaton: the v3 suspended-queue harness.

The LLM is a stateless CPU. State lives on disk as JSON task files.

Three invariants:
  1. True Amnesia       — no chat logs across runs; each task is a single static prompt.
  2. Dependency Injection — only the exact `allowed_tools` are wired up per task.
  3. Illusion of Self-Execution — Create_Thunk spawns children via suspend/resume,
     so the parent's thread dies while the child runs.

Environment:
  THUNK_MODEL        LiteLLM model string (default: openai/local)
  THUNK_API_BASE     Base URL for the LLM API (default: http://localhost:8080/v1)
  THUNK_POLL         Watcher poll interval in seconds (default: 1.0)
  OPENAI_API_KEY     API key — not required for llama-server (uses dummy value)
  THUNK_LSP_CMD      Executable for the LSP-MCP server (optional; enables real QueryLSP)
  THUNK_LSP_ARGS     Space-separated args for the LSP-MCP server (optional)

Drop a JSON file into .thunk/ matching the TaskFile schema to dispatch a task.
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

# Tools whose presence implies the agent must mutate state. If allowed but
# never called, the Anti-Yapping guardrail fails the task.
_MUTATING_TOOLS = frozenset({"ASTSplice"})

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

    Wrappers raise on tool failure so the harness can fail fast (v3 guardrail:
    Instant Death on Tool Failure — no retries, no error feedback to the LLM).
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
    except ImportError as exc:
        print(f"[thunkd] WARN: ariadne primitives import failed — {exc}")
        return {}

    # LSP is optional and pulls in the `mcp` stack; tolerate its absence
    # rather than failing over to mocks for all tools.
    _LSPManager: Optional[Any] = None
    try:
        from ariadne.lsp import LSPManager as _LSPManager  # type: ignore[no-redef]
    except ImportError as exc:
        print(f"[thunkd] WARN: ariadne.lsp unavailable ({exc}) — QueryLSP disabled")

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
            raise RuntimeError(f"ExtractAST: unsupported extension '{ext}'")
        extractor = _get_extractor(lang)
        if extractor is None:
            raise RuntimeError(f"ExtractAST: tree-sitter-{lang} not installed")
        status, results = extractor.tick(
            {"filepath": filepath, "query_string": query_string, "capture_name": capture_name},
            None,
        )
        # Ariadne statuses: "SUCCESS" (matches), "NOT_FOUND" (zero matches — benign), "ERROR".
        if status == "SUCCESS":
            return "\n---\n".join(results)
        if status == "NOT_FOUND":
            return ""
        raise RuntimeError(f"ExtractAST {status}: {' | '.join(results)}")

    _splice = _ASTSplice()

    def ast_splice(filepath: str, edits: list) -> str:
        status, out = _splice.tick({"filepath": filepath, "edits": edits}, None)
        # Ariadne statuses: "SUCCESS" (returns filepath), "REJECTED" (markdown fences), "ERROR".
        if status == "SUCCESS":
            return str(out)
        raise RuntimeError(f"ASTSplice {status}: {out}")

    tools: dict[str, Any] = {
        "ExtractAST": extract_ast,
        "ASTSplice": ast_splice,
    }

    lsp_cmd = os.getenv("THUNK_LSP_CMD")
    if _LSPManager is not None:
        _lsp = _LSPManager(lsp_cmd, os.getenv("THUNK_LSP_ARGS", "").split()) if lsp_cmd else None

        def query_lsp(filepath: str, line: int, character: int) -> str:
            if _lsp is None:
                return (
                    f"[mock LSP] hover at {filepath}:{line}:{character}"
                    " — set THUNK_LSP_CMD to enable real LSP queries"
                )
            return _lsp.get_hover(filepath, line, character)

        tools["QueryLSP"] = query_lsp
    else:
        tools["QueryLSP"] = lambda filepath, line, character: (
            f"[mock LSP] hover at {filepath}:{line}:{character} — ariadne.lsp unavailable"
        )

    return tools


# ---------------------------------------------------------------------------
# Mock stubs — used only when ../ariadne cannot be imported
# ---------------------------------------------------------------------------


def _extract_ast(filepath: str, query_string: str, capture_name: str = "node") -> str:
    return f"[mock AST] {filepath} query={query_string!r} capture={capture_name!r}"


def _ast_splice(filepath: str, edits: list) -> str:
    return f"[mock splice] {filepath}: {len(edits)} edit(s) applied"


def _query_lsp(filepath: str, line: int, character: int) -> str:
    return f"[mock LSP] hover at {filepath}:{line}:{character}"


# ---------------------------------------------------------------------------
# Task schema
# ---------------------------------------------------------------------------


class TaskFile(BaseModel):
    id: str
    intent: str
    target_files: list[str] = []
    parent_context: Optional[str] = None
    parent_id: Optional[str] = None
    allowed_tools: list[str]
    status: Literal["pending", "running", "suspended", "success", "failed"]
    result: Optional[str] = None
    # Suspended-queue state: the conversation tape and the child we're awaiting.
    messages: Optional[list[dict[str, Any]]] = None
    pending_child_id: Optional[str] = None
    pending_tool_call_id: Optional[str] = None


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
                "Spawn a child agent to handle a sub-task with perfect situational awareness. "
                "The child runs in isolation; you receive its final output as this tool's return value."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "The immediate atomic goal for the child agent.",
                    },
                    "target_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Explicit file boundaries for the child. No guessing allowed.",
                    },
                    "parent_context": {
                        "type": "string",
                        "description": (
                            "Crucial constraints the child must know (e.g. 'I already tried X, do Y instead')."
                        ),
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tool names the child agent is allowed to use.",
                    },
                },
                "required": ["intent", "target_files", "parent_context", "tools"],
            },
        },
    },
}

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
# Create_Thunk interceptor — writes child, raises to suspend parent
# ---------------------------------------------------------------------------


class _SuspendTask(Exception):
    """Unwind out of the worker loop to park the task as suspended."""

    def __init__(self, child_id: str, tool_call_id: str) -> None:
        self.child_id = child_id
        self.tool_call_id = tool_call_id


def _spawn_child(
    *,
    intent: str,
    target_files: list[str],
    parent_context: str,
    tools: list[str],
    parent_id: str,
    tool_call_id: str,
) -> None:
    """Write a new child task file; raise to suspend the parent thread."""
    if not target_files:
        raise RuntimeError("Create_Thunk: target_files must be a non-empty array")
    if not parent_context or not parent_context.strip():
        raise RuntimeError("Create_Thunk: parent_context is required")
    if not tools:
        raise RuntimeError("Create_Thunk: tools must be a non-empty array")

    child_id = f"child_{parent_id}_{uuid.uuid4().hex[:8]}"
    child_path = THUNK_DIR / f"{child_id}.json"
    _write_task(
        TaskFile(
            id=child_id,
            intent=intent,
            target_files=target_files,
            parent_context=parent_context,
            parent_id=parent_id,
            allowed_tools=tools,
            status="pending",
        ),
        child_path,
    )
    print(f"[thunkd] [{parent_id}] spawned '{child_id}': {intent!r}")
    raise _SuspendTask(child_id=child_id, tool_call_id=tool_call_id)


# ---------------------------------------------------------------------------
# LiteLLM message normaliser
# ---------------------------------------------------------------------------


def _msg_to_dict(msg: Any) -> dict:
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


def _build_initial_messages(task: TaskFile) -> list[dict]:
    """Construct the single static prompt for a fresh (non-resumed) task."""
    lines = [f"Intent: {task.intent}"]
    if task.target_files:
        lines.append("Target files:")
        lines.extend(f"  - {f}" for f in task.target_files)
    else:
        lines.append("Target files: (none specified)")
    if task.parent_context:
        lines.append("")
        lines.append(f"Parent context: {task.parent_context}")

    system = (
        "You are an isolated, stateless worker. "
        "Use your injected tools to achieve the intent, then reply with a terse final answer. "
        "You have no memory of prior conversations. "
        "Tool failures are fatal — a malformed call terminates you immediately, so get it right the first time."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _execute(path: Path) -> None:
    task = _patch_task(path, status="running")
    if task is None:
        return

    resumed = task.messages is not None
    print(
        f"[thunkd] [{task.id}] {'resuming' if resumed else 'starting'}: {task.intent!r}"
    )

    schemas = [
        TOOL_SCHEMAS[name] for name in task.allowed_tools if name in TOOL_SCHEMAS
    ]
    messages: list[dict] = list(task.messages) if resumed else _build_initial_messages(task)
    called_tool_names: set[str] = set()

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
                # Anti-Yapping: if agent was given a mutating tool and never called one, fail.
                mutating_allowed = set(task.allowed_tools) & _MUTATING_TOOLS
                if mutating_allowed and not (called_tool_names & _MUTATING_TOOLS):
                    err = (
                        "[Harness Error] Child agent hallucinated completion without "
                        f"executing mutating tools (allowed: {sorted(mutating_allowed)})."
                    )
                    _patch_task(path, status="failed", result=err)
                    print(f"[thunkd] [{task.id}] FAILED (anti-yap): {err}")
                    return
                _patch_task(path, status="success", result=final)
                print(f"[thunkd] [{task.id}] success: {final[:80]!r}")
                return

            for tc in tool_calls:
                fn_name = tc.function.name
                called_tool_names.add(fn_name)
                try:
                    fn_args: dict = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    # Instant death: bad JSON in tool call, no retry.
                    err = f"[Harness Error] malformed JSON in tool '{fn_name}' call: {exc}"
                    _patch_task(path, status="failed", result=err)
                    print(f"[thunkd] [{task.id}] FAILED (bad JSON): {err}")
                    return

                if fn_name == "Create_Thunk":
                    # Raises _SuspendTask on success, RuntimeError on bad args.
                    _spawn_child(
                        intent=fn_args.get("intent", ""),
                        target_files=fn_args.get("target_files", []),
                        parent_context=fn_args.get("parent_context", ""),
                        tools=fn_args.get("tools", []),
                        parent_id=task.id,
                        tool_call_id=tc.id,
                    )
                    # unreachable
                    return

                if fn_name not in _LOCAL_FNS:
                    err = f"[Harness Error] unknown tool '{fn_name}'"
                    _patch_task(path, status="failed", result=err)
                    print(f"[thunkd] [{task.id}] FAILED (unknown tool): {err}")
                    return

                try:
                    result = _LOCAL_FNS[fn_name](fn_args)
                except Exception as exc:
                    # Instant Death on Tool Failure — do not feed the error back.
                    err = f"[Harness Error] tool '{fn_name}' raised: {exc}"
                    _patch_task(path, status="failed", result=err)
                    print(f"[thunkd] [{task.id}] FAILED (tool error): {err}")
                    return

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

    except _SuspendTask as sus:
        _patch_task(
            path,
            status="suspended",
            messages=messages,
            pending_child_id=sus.child_id,
            pending_tool_call_id=sus.tool_call_id,
        )
        print(f"[thunkd] [{task.id}] suspended → awaiting '{sus.child_id}'")
        return

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


def _resume_ready_suspended() -> None:
    """Wake any suspended parent whose awaited child has reached a terminal state."""
    for path in sorted(THUNK_DIR.glob("*.json")):
        parent = _read_task(path)
        if parent is None or parent.status != "suspended":
            continue
        if not parent.pending_child_id:
            continue

        child_path = THUNK_DIR / f"{parent.pending_child_id}.json"
        child = _read_task(child_path)
        if child is None or child.status not in ("success", "failed"):
            continue

        child_output = child.result or ""
        injected = (
            f"[child failed] {child_output}"
            if child.status == "failed"
            else child_output
        )
        messages = list(parent.messages or [])
        messages.append(
            {
                "role": "tool",
                "tool_call_id": parent.pending_tool_call_id or "",
                "content": injected,
            }
        )
        _patch_task(
            path,
            status="pending",
            messages=messages,
            pending_child_id=None,
            pending_tool_call_id=None,
        )
        print(
            f"[thunkd] [{parent.id}] resumed (child '{parent.pending_child_id}' {child.status})"
        )


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
            _resume_ready_suspended()
            for path in _find_pending():
                task = _read_task(path)
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
