# thunk

A headless, amnesic UNIX job queue — a sub-processor for large external LLMs.

## What it is

Large LLMs (70B+) are good at planning but expensive to keep in the loop while a model hammers away at a codebase. Thunk solves this with a strict **Brain / Muscle** split:

- **The Brain** (your external LLM — opencode, OpenHands, Claude, etc.) does architectural reasoning. It never touches the codebase directly. It delegates via a single MCP tool: `Dispatch_Thunk`.
- **The Muscle** (`thunkd`) is a local daemon spinning up cheap 9B workers. Each worker gets a bash shell, an isolated task, and no memory of anything else.

The Brain's context stays pristine. The local model makes mistakes, reads stderr, and fixes them in isolation. The Brain only sees the final result.

## Components

| File | Role |
|---|---|
| `thunkd.py` | Daemon. Polls `.thunk/` for pending task files, runs workers, writes results. |
| `mcp_server.py` | MCP stdio server. Exposes `Dispatch_Thunk` to the Brain. |

## Running

```bash
# Activate the virtualenv
.venv/Scripts/activate   # Windows
source .venv/bin/activate # Linux/macOS

# Start the daemon (leave running in a terminal)
python thunkd.py

# Start the MCP server (configure as MCP stdio server in your LLM client)
python mcp_server.py
```

Install dependencies if needed:
```bash
pip install litellm pydantic python-dotenv mcp
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `THUNK_MODEL` | `openai/local` | LiteLLM model string for workers |
| `THUNK_API_BASE` | `http://localhost:8080/v1` | LLM server base URL |
| `THUNK_SHELL` | `msys2/current/usr/bin/bash.exe` | Bash executable — always bash, never cmd.exe |
| `THUNK_BASH_TIMEOUT` | `120` | Per-command timeout (seconds) |
| `THUNK_POLL` | `1.0` | Watcher poll interval (seconds) |
| `THUNK_DISPATCH_TIMEOUT` | `300` | Max seconds the MCP server waits per task |

Designed for local `llama-server` or any OpenAI-compatible endpoint.

## How it works

Tasks are JSON files in `.thunk/` picked up by the daemon:

```json
{
  "id": "task_abc",
  "intent": "Rename compute_value to compute_doubled in fixtures/demo_a.py",
  "target_files": ["fixtures/demo_a.py"],
  "allowed_tools": ["Execute_Bash"],
  "status": "pending"
}
```

The daemon claims it, builds an amnesic prompt, and runs the local model in an agentic loop. The model calls `Execute_Bash` until the task is done, then emits a terse result. The file flips to `status: success`.

### Hierarchical delegation

Workers can spawn sub-workers via `Create_Thunk`. The parent suspends (thread dies cleanly), the child runs, and the daemon resumes the parent with the child's result injected as the tool response. This allows arbitrarily deep delegation without thread exhaustion.

### Guardrails

- **Instant Death**: non-zero bash exit → task marked `failed` immediately. The error is not fed back to the model — it bubbles up to the parent or Brain to adjust strategy.
- **Anti-Yapping**: if a worker has `Execute_Bash` available but exits with plain text without ever calling it, the task is failed with `[Harness Error]`. No declaring victory without doing work.
- **Crash Recovery**: stale `running` tasks (daemon killed mid-execution) are reset to `pending` on startup.

## MCP tool

```
Dispatch_Thunk(intent, target_files, allowed_tools?)
```

Returns the worker's terse final output, or a `[FAILED]` / `[TIMEOUT]` message. The Brain never needs to know how many bash commands it took.
