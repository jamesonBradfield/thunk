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

## The Downsides

1. The "Nuke" Risk (Recursive File Modification)

Because Thunk workers are given Execute_Bash as their primary tool, they have the technical ability to run destructive commands across your entire project.

  Irreversible Edits: A 9B model might attempt a complex sed or awk command that successfully executes (exit code 0) but fundamentally corrupts the logic or data of several files.

  Scope Creep: If target_files are not strictly defined by the Brain, a worker could inadvertently modify files outside the intended refactor area.

  Recursive Deletion: A hallucinated bash command could theoretically include rm -rf or other destructive operations that wipe directories before the Brain can intervene.

2. The "Deadlock" and "Self-Sabotage" Loop

The meta-tooling capability—where Thunk can spawn children or modify its own engine—creates a risk of logical loops.

  Recursive Resource Exhaustion: Through Create_Thunk, an agent could theoretically spawn an infinite chain of sub-agents, consuming all available system memory or API credits until the THUNK_DISPATCH_TIMEOUT is reached.

  Engine Tampering: Since Thunk has access to the codebase via bash, it could theoretically "fix" its own thunkd.py or mcp_server.py files in a way that disables safety guardrails or introduces backdoors.

3. "Instant Death" Blind Spots

The Instant Death on Tool Failure guardrail is designed to prevent hallucinations, but it can be a double-edged sword.

  Lost Context: When a worker dies, the "amnesic" nature of the system means all immediate trial-and-error context in that thread is lost. If the Parent/Brain doesn't capture the stderr correctly, you can end up in a cycle where you are repeatedly killing workers without understanding the root cause of the failure.

  False Successes: The "Anti-Yapping" guardrail only triggers if no tools are called. If a model calls a non-mutating tool (like ls) and then declares victory without actually performing the requested edit, the system might mark it as a success, leading to "silent failures" where the Brain thinks work was done when it wasn't.

4. Hardware and Environment Vulnerability

The architecture relies on the local environment being "UNIX-like," which creates friction on Windows systems.

  Shell Injection: While thunkd.py is hardwired to MSYS2 bash to prevent cmd.exe collisions, the system essentially executes raw strings from an LLM directly into your shell. A malicious or highly confused model could execute unintended system-level commands.

  VRAM/System Instability: Running 30B+ models alongside high-context 9B workers pushes consumer hardware to its thermal and memory limits. If the thermal limits are pushed too far (the "microwave" scenario), it could lead to system crashes or hardware degradation.

Recommended Mitigations

To minimize these dangers, the "Pristine Working Tree" Protocol is critical:

  Mandatory Git Checkpoints: Never run Thunk on an uncommitted working tree. The Brain must be able to run git restore . the moment a Thunk task results in a "success" that actually broke functionality.

  Restricted Bash Environment: Running the daemon inside a container or a restricted VM would prevent a "hallucinated bash" from touching sensitive system files outside the project directory.

  Stricter Intent Validation: The Brain (the larger, more capable model) must act as a ruthless "Lead Maintainer," verifying the git diff of every Thunk completion before it is committed.
