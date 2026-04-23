# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

`ariadne-headless` is a headless, file-based AI agent orchestrator. The entire system lives in `harness.py` (~410 lines). It polls a `.ariadne/` directory for pending JSON task files, spins up worker threads, drives an LLM agentic loop with tool calling, and writes results back to those files.

## Running the Harness

```bash
python harness.py
```

Dependencies are managed in `.venv/`. Activate with:
```bash
.venv/Scripts/activate   # Windows
```

Install deps if needed:
```bash
pip install litellm pydantic python-dotenv
```

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `ARIADNE_MODEL` | `openai/local` | LiteLLM model string |
| `ARIADNE_API_BASE` | `http://localhost:8080/v1` | LLM server base URL |
| `ARIADNE_POLL` | `1.0` | Watcher poll interval (seconds) |
| `ARIADNE_CHILD_POLL` | `0.5` | Child-task poll interval (seconds) |
| `OPENAI_API_KEY` | *(not required for llama-server)* | API key |

Designed to work against a local `llama-server` or any OpenAI-compatible endpoint.

## Architecture

### Task lifecycle

Tasks are JSON files in `.ariadne/` matching the `TaskFile` Pydantic schema:
- `id`: unique string identifier
- `intent`: instruction string for the agent
- `allowed_tools`: list of tool names the agent may call
- `status`: `"pending"` → `"running"` → `"success"` / `"failed"`
- `result`: final output string (written on completion)

### Core execution flow

1. **Watcher** (`run()`) — tight loop scanning `.ariadne/` for `status == "pending"` files
2. **Claim** (`_claim()` / `_release()`) — in-memory set prevents double-dispatch across threads
3. **Worker** (`_worker()` / `_execute()`) — per-task thread:
   - Builds system + user messages
   - Calls `litellm.completion()` with the allowed tool schemas
   - Agentic loop: tool calls → invoke → append result → repeat until the model emits plain text
   - Patches the task file to `success` or `failed`
4. **Delegation** (`_delegate()`) — tool that writes a child task file and blocks until it reaches a terminal state (polls `ARIADNE_CHILD_POLL`)

### Tool registry

Four mock tools are defined with JSON schemas; their dispatch logic is in `_call_tool()`:
- `ExtractAST` — parse a source file, return AST
- `ASTSplice` — replace an AST node with new code
- `QueryLSP` — query language server for symbol info
- `Delegate_Task` — spawn a child agent task

Extending the system means adding an entry to `TOOLS` (schema) and a branch in `_call_tool()`.

### File I/O conventions

- Writes are atomic: temp file + rename (`_write_task()`)
- Reads are wrapped in try/except for concurrent safety (`_read_task()`)
- Partial updates use `_patch_task()` (read-modify-write)

## Code quality tools available in `.venv`

```bash
black harness.py       # format
ruff check harness.py  # lint
mypy harness.py        # type check
pytest                 # run tests (none exist yet)
```
