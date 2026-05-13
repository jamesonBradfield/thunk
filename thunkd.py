#!/usr/bin/env python3
"""thunkd.py — Headless automaton: the v4 bash-worker harness.

The LLM is a stateless CPU. State lives on disk as JSON task files.
Workers are bash-wielding grunts: they explore, modify, and validate via Execute_Bash.

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
  THUNK_SHELL        Shell executable for Execute_Bash (default: system shell)
  THUNK_BASH_TIMEOUT Per-command timeout in seconds (default: 60)

Drop a JSON file into .thunk/ matching the TaskFile schema to dispatch a task.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
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
BASH_TIMEOUT = int(os.getenv("THUNK_BASH_TIMEOUT", "120"))
SHELL_EXE = os.getenv("THUNK_SHELL", "C:/Users/jamie/scoop/apps/msys2/current/usr/bin/bash.exe")
# Intent strings longer than this in a Create_Thunk call get collapsed to a commit hash pointer.
INTENT_COLLAPSE_THRESHOLD = int(os.getenv("THUNK_INTENT_COLLAPSE", "500"))

# Tools whose presence implies the agent must do actual work.
# If allowed but never called, the Anti-Yapping guardrail fails the task.
_MUTATING_TOOLS = frozenset({"Execute_Bash"})

# ---------------------------------------------------------------------------
# Execute_Bash implementation
# ---------------------------------------------------------------------------


def _execute_bash(command: str) -> str:
    """Run command via bash; raise on non-zero exit (Instant Death trigger)."""
    if not command:
        raise RuntimeError("Execute_Bash requires a 'command' parameter.")
    try:
        proc = subprocess.run(
            [SHELL_EXE, "-c", command],
            capture_output=True,
            text=True,
            timeout=BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Bash command timed out after {BASH_TIMEOUT} seconds.")
    except Exception as e:
        raise RuntimeError(f"Execute_Bash failed: {e}")

    output = proc.stdout.strip()

    if proc.returncode != 0:
        error_msg = proc.stderr.strip() or output
        raise RuntimeError(f"Bash exit code {proc.returncode}:\n{error_msg}")

    if proc.stderr.strip():
        output += f"\n--- STDERR (Warnings) ---\n{proc.stderr.strip()}"

    return output or "Command executed successfully with no output."


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
    "Execute_Bash": {
        "type": "function",
        "function": {
            "name": "Execute_Bash",
            "description": (
                "Execute a shell command and return its stdout. "
                "Use for exploration (cat, grep, find, ls), "
                "modification (sed, awk, patch, writing files), "
                "and validation (pytest, flake8, npm run build). "
                "A non-zero exit code terminates the task immediately — no retries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute.",
                    },
                },
                "required": ["command"],
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
                            "Crucial constraints the child must know "
                            "(e.g. 'I already tried X, do Y instead')."
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

_LOCAL_FNS: dict[str, Any] = {
    "Execute_Bash": lambda a: _execute_bash(**a),
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
    except (ValidationError, Exception) as exc:
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


# Matches a full 40-char hash, or the word "commit" followed by a short/full hash.
_COMMIT_HASH_RE = re.compile(
    r"\b([0-9a-f]{40})\b|[Cc]ommit[:\s]+([0-9a-f]{7,40})\b"
)


def _extract_commit_hash(text: str) -> str | None:
    """Return the first git commit hash found in text, or None."""
    m = _COMMIT_HASH_RE.search(text)
    if m:
        return m.group(1) or m.group(2)
    return None


def _collapse_intent_in_tape(
    messages: list[dict],
    tool_call_id: str,
    commit_hash: str,
) -> list[dict]:
    """Replace oversized Create_Thunk intent args in the parent tape with a commit hash pointer.

    Keeps the parent's context window lean after a child commits a large artefact.
    Only collapses intents longer than INTENT_COLLAPSE_THRESHOLD chars.
    """
    result = []
    for msg in messages:
        if msg.get("role") == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                new_tcs = []
                mutated = False
                for tc in tool_calls:
                    if (
                        tc.get("id") == tool_call_id
                        and tc.get("function", {}).get("name") == "Create_Thunk"
                    ):
                        try:
                            args = json.loads(tc["function"]["arguments"])
                            if len(args.get("intent", "")) > INTENT_COLLAPSE_THRESHOLD:
                                args["intent"] = (
                                    f"[COLLAPSED — refer to git commit {commit_hash}]"
                                )
                                tc = {
                                    **tc,
                                    "function": {
                                        **tc["function"],
                                        "arguments": json.dumps(args),
                                    },
                                }
                                mutated = True
                        except (json.JSONDecodeError, KeyError):
                            pass
                    new_tcs.append(tc)
                if mutated:
                    msg = {**msg, "tool_calls": new_tcs}
        result.append(msg)
    return result


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
        "You are an isolated, stateless bash worker running in a Linux environment. "
        "Use Execute_Bash to explore the codebase (cat, grep, find, ls), "
        "modify files (sed, awk, patch, or write via shell redirection), "
        "and validate your work (use python3, pytest, flake8, npm run build, etc.). "
        "You have no memory of prior conversations. "
        "A failed command (non-zero exit) terminates you immediately — "
        "check your syntax before running. "
        "If you modified any files, you MUST commit your changes before declaring success. "
        "Stage only your target files, then commit if anything is staged — one Execute_Bash call: "
        f"`git add {' '.join(task.target_files) if task.target_files else '-A'}; "
        "git diff --cached --quiet || git commit -m '<terse one-line description>'; git rev-parse HEAD` "
        "and include the printed 40-character commit hash in your final answer. "
        "When the task is complete, reply with a terse final answer."
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

            # Retry on transient server errors (model loading, rate limits).
            for _attempt in range(5):
                try:
                    response = litellm.completion(**kwargs)
                    break
                except (
                    litellm.exceptions.ServiceUnavailableError,
                    litellm.exceptions.RateLimitError,
                ) as _e:
                    wait = 5 * (_attempt + 1)
                    print(f"[thunkd] [{task.id}] transient error ({_e.__class__.__name__}), retry in {wait}s…")
                    time.sleep(wait)
            else:
                raise RuntimeError("LLM unavailable after 5 retries")
            msg = response.choices[0].message
            messages.append(_msg_to_dict(msg))

            tool_calls = getattr(msg, "tool_calls", None) or []

            if not tool_calls:
                final = (msg.content or "").strip()
                # Anti-Yapping: if agent was given a mutating tool and never called one, fail.
                mutating_allowed = set(task.allowed_tools) & _MUTATING_TOOLS
                if mutating_allowed and not (called_tool_names & _MUTATING_TOOLS):
                    err = (
                        "[Harness Error] Agent declared completion without "
                        f"ever calling its tools (allowed: {sorted(mutating_allowed)})."
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

        # Context collapse: if the child committed something, shrink the
        # Create_Thunk call that spawned it down to a hash pointer so the
        # parent's tape doesn't balloon with inline code.
        if child.status == "success" and parent.pending_tool_call_id:
            commit_hash = _extract_commit_hash(child_output)
            if commit_hash:
                messages = _collapse_intent_in_tape(
                    messages, parent.pending_tool_call_id, commit_hash
                )
                print(
                    f"[thunkd] [{parent.id}] collapsed Create_Thunk intent → {commit_hash}"
                )

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


def _recover_stale_running() -> None:
    """On startup, reset any tasks stuck at 'running' from a previous crash."""
    for path in sorted(THUNK_DIR.glob("*.json")):
        task = _read_task(path)
        if task and task.status == "running":
            _patch_task(path, status="pending")
            print(f"[thunkd] recovered stale 'running' task '{task.id}' → pending")


def run() -> None:
    THUNK_DIR.mkdir(exist_ok=True)
    shell_desc = SHELL_EXE or "system default"
    print(f"[thunkd] watching {THUNK_DIR.resolve()}  model={MODEL}  api_base={API_BASE}")
    print(f"[thunkd] bash worker  shell={shell_desc}  timeout={BASH_TIMEOUT}s")
    _recover_stale_running()
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
