# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

`thunk` is a headless, file-based AI agent daemon. The entire system lives in `thunkd.py` (~410 lines). It polls a `.thunk/` directory for pending JSON task files, spins up worker threads, drives an LLM agentic loop with tool calling, and writes results back to those files.

## Running the Daemon

```bash
python thunkd.py
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
| `THUNK_MODEL` | `openai/local` | LiteLLM model string |
| `THUNK_API_BASE` | `http://localhost:8080/v1` | LLM server base URL |
| `THUNK_POLL` | `1.0` | Watcher poll interval (seconds) |
| `THUNK_CHILD_POLL` | `0.5` | Child-task poll interval (seconds) |
| `OPENAI_API_KEY` | *(not required for llama-server)* | API key |
| `THUNK_LSP_CMD` | *(unset)* | LSP-MCP server executable; enables real `QueryLSP` |
| `THUNK_LSP_ARGS` | *(empty)* | Space-separated args for the LSP-MCP server |

Designed to work against a local `llama-server` or any OpenAI-compatible endpoint.

## Architecture

### Task lifecycle

Tasks are JSON files in `.thunk/` matching the `TaskFile` Pydantic schema:
- `id`: unique string identifier
- `intent`: instruction string for the agent
- `allowed_tools`: list of tool names the agent may call
- `status`: `"pending"` → `"running"` → `"success"` / `"failed"`
- `result`: final output string (written on completion)

### Core execution flow

1. **Watcher** (`run()`) — tight loop scanning `.thunk/` for `status == "pending"` files
2. **Claim** (`_claim()` / `_release()`) — in-memory set prevents double-dispatch across threads
3. **Worker** (`_worker()` / `_execute()`) — per-task thread:
   - Builds system + user messages
   - Calls `litellm.completion()` with the allowed tool schemas
   - Agentic loop: tool calls → invoke → append result → repeat until the model emits plain text
   - Patches the task file to `success` or `failed`
4. **Thunk creation** (`_create_thunk()`) — tool that writes a child task file and blocks until it reaches a terminal state (polls `THUNK_CHILD_POLL`)

### Tool registry

Four tools with JSON schemas; dispatch is in `_execute()` via `_LOCAL_FNS`:

| Tool | Real implementation | Inputs |
|---|---|---|
| `ExtractAST` | `ariadne.primitives.ExtractAST` (tree-sitter) | `filepath`, `query_string`, `capture_name` |
| `ASTSplice` | `ariadne.primitives.ASTSplice` (byte-range edits) | `filepath`, `edits[]` (`start_byte`, `end_byte`, `new_code`) |
| `QueryLSP` | `ariadne.lsp.LSPManager.get_hover` (MCP-LSP) | `filepath`, `line`, `character` |
| `Create_Thunk` | built-in `_create_thunk()` | `intent`, `tools[]` |

`_load_tools()` runs at import time and attempts to import from `../ariadne/`. On success, real implementations are used; on failure (missing package or tree-sitter deps), mock stubs fall back gracefully. The startup log line reports which source loaded.

`ExtractAST` detects language from file extension (`.py→python`, `.rs→rust`, `.ts→typescript`, etc.) and caches one parser instance per language. `ASTSplice` edits are applied bottom-up (reverse byte order) to keep offsets valid.

To add a new tool: add an entry to `TOOL_SCHEMAS` (JSON schema) and add a case in the `_LOCAL_FNS` dict (or the `Create_Thunk` branch in `_execute()`).

### File I/O conventions

- Writes are atomic: temp file + rename (`_write_task()`)
- Reads are wrapped in try/except for concurrent safety (`_read_task()`)
- Partial updates use `_patch_task()` (read-modify-write)

## Code quality tools available in `.venv`

```bash
black thunkd.py       # format
ruff check thunkd.py  # lint
mypy thunkd.py        # type check
pytest                # run tests (none exist yet)
```
