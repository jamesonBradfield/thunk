# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

`thunk` is a **headless, amnesic UNIX job queue** — a sub-processor for large external LLMs (see `ARCHITECTURE_V4.md`). The system has two parts:

- **`thunkd.py`** — the daemon. Polls `.thunk/` for pending JSON task files, spins up worker threads, drives a local 9B LLM agentic loop with `Execute_Bash`, writes results back to those files. Hierarchical delegation (parent → child) is implemented by suspending and killing the parent's thread while the child runs.
- **`mcp_server.py`** — the MCP interface. Exposes one tool (`Dispatch_Thunk`) so a large external LLM (the Brain) can hand off atomic tasks. The Brain never touches the codebase; it only calls this tool.

## Running

```bash
# Start the daemon
python thunkd.py

# Start the MCP server (configure as MCP stdio server in your LLM client)
python mcp_server.py
```

Dependencies are managed in `.venv/`. Activate with:
```bash
.venv/Scripts/activate   # Windows
```

Install deps if needed:
```bash
pip install litellm pydantic python-dotenv mcp
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `THUNK_MODEL` | `openai/local` | LiteLLM model string for workers |
| `THUNK_API_BASE` | `http://localhost:8080/v1` | LLM server base URL |
| `THUNK_POLL` | `1.0` | Watcher poll interval (seconds) |
| `OPENAI_API_KEY` | *(not required for llama-server)* | API key |
| `THUNK_SHELL` | `msys2/current/usr/bin/bash.exe` | Shell for `Execute_Bash` — always bash, never cmd.exe |
| `THUNK_BASH_TIMEOUT` | `120` | Per-command timeout in seconds |
| `THUNK_DISPATCH_TIMEOUT` | `300` | Max seconds `mcp_server.py` waits per task |

Designed to work against a local `llama-server` or any OpenAI-compatible endpoint.

## Architecture

### Task lifecycle

Tasks are JSON files in `.thunk/` matching the `TaskFile` Pydantic schema:
- `id`: unique string identifier
- `intent`: instruction string for the agent
- `target_files`: explicit file boundaries — enforced as non-empty for children spawned via `Create_Thunk`
- `parent_context`: crucial constraints from the parent (e.g. "I already tried X, do Y instead") — required for children
- `parent_id`: link from child back to spawning parent
- `allowed_tools`: list of tool names the agent may call (dependency injection)
- `status`: `"pending"` → `"running"` → `"success"` / `"failed"`, or `"suspended"` while awaiting a child
- `result`: final output string (written on completion)
- `messages`, `pending_child_id`, `pending_tool_call_id`: suspension tape — persisted when the parent parks to wait for a child; cleared on resume

### Core execution flow

1. **Watcher** (`run()`) — tight loop scanning `.thunk/`. Each tick it first calls `_resume_ready_suspended()` to wake parents whose children are done, then scans for `status == "pending"` files to dispatch.
2. **Claim** (`_claim()` / `_release()`) — in-memory set prevents double-dispatch across threads
3. **Worker** (`_worker()` / `_execute()`) — per-task thread:
   - If the task has a stored `messages` tape, resumes from it; otherwise builds a fresh system + user message from `intent`, `target_files`, and `parent_context`
   - Calls `litellm.completion()` with the allowed tool schemas
   - Agentic loop: tool calls → invoke → append result → repeat until the model emits plain text
   - Terminates the task: `success`, `failed`, or (on `Create_Thunk`) `suspended` with state persisted
4. **Thunk creation** (`_spawn_child()`) — writes a child task file and raises `_SuspendTask`. `_execute()` catches it, persists the parent's message tape + `pending_child_id` + `pending_tool_call_id`, marks the parent `suspended`, and returns — the thread dies cleanly.
5. **Resume** (`_resume_ready_suspended()`) — when a child reaches `success`/`failed`, its output is appended to the parent's tape as a `tool` message keyed to the stored `tool_call_id`, and the parent flips back to `pending` to be picked up by a fresh worker.

### Guardrails

- **Instant Death on Tool Failure**: if a tool raises (bad JSON args, non-zero bash exit, unknown tool name), the task is marked `failed` immediately — the error is *not* fed back to the LLM. The error bubbles up to the parent (as the child's `result`) so it can adjust its delegation strategy.
- **Anti-Yapping**: `_MUTATING_TOOLS = {"Execute_Bash"}`. If an agent has `Execute_Bash` in `allowed_tools` but exits with plain text without ever calling it, the task is marked `failed` with a `[Harness Error]` prefix.
- **Context Starvation Prevention**: `_spawn_child` rejects `Create_Thunk` calls with empty `target_files` or blank `parent_context`.

### Tool registry

Two tools with JSON schemas; dispatch is in `_execute()` via `_LOCAL_FNS`:

| Tool | Implementation | Inputs |
|---|---|---|
| `Execute_Bash` | `subprocess.run` via `_execute_bash()` | `command` (string) |
| `Create_Thunk` | built-in `_spawn_child()` (suspends parent) | `intent`, `target_files[]`, `parent_context`, `tools[]` — all required |

To add a new tool: add an entry to `TOOL_SCHEMAS` (JSON schema) and add a case in `_LOCAL_FNS` (or the `Create_Thunk` branch in `_execute()`). Tool wrappers should **raise** on failure so the Instant-Death guardrail fires — do not return error strings.

### File I/O conventions

- Writes are atomic: temp file + rename (`_write_task()`)
- Reads are wrapped in try/except for concurrent safety (`_read_task()`)
- Partial updates use `_patch_task()` (read-modify-write)

## Code quality tools available in `.venv`

```bash
black thunkd.py mcp_server.py    # format
ruff check thunkd.py             # lint
mypy thunkd.py                   # type check
pytest                           # run tests (none exist yet)
```
