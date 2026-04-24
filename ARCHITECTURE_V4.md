# Thunk v4: The MCP Sub-Processor Architecture

## The Paradigm Shift (Inversion of Control)
Thunk is no longer a standalone, self-contained AI orchestrator. It is a **headless, amnesic UNIX job queue** designed to act as a sub-processor for larger, external LLMs (like a 70B+ model running in `opencode` or OpenHands).

We have established a strict **Macro-Orchestrator / Micro-Executor** split:
* **The Brain (External Large LLM):** Handles complex reasoning, architectural planning, and decomposition. It **never** touches the codebase directly. It interacts with the system exclusively through an MCP tool (`Dispatch_Thunk`).
* **The Muscle (Thunkd):** A lightweight Python daemon polling a `.thunk/` directory. It spins up cheap, local LLMs (e.g., 9B parameters) to execute isolated, atomic tasks.

## Why We Did This (Context Protection)
When Large LLMs edit code directly, their massive context windows become poisoned by their own typos, failed bash commands, and syntax errors. 

Thunk acts as an isolation layer. The local 9B model hammers away at the codebase, makes mistakes, reads `stderr`, and fixes them in isolation. The Large LLM only sees the final result: *"Task complete. Here is the output."* This saves massive token costs and keeps the Brain's context pristine.

---

## The Unix Tooling Pivot
We have officially stripped out all custom Tree-sitter AST extraction and splicing tools (`ariadne.primitives`). They were too fragile and forced the LLM to calculate byte-ranges.

Instead, we adhere to the Unix philosophy. The local Thunk workers are given one primary tool: `Execute_Bash`.
* **Exploration:** `cat`, `grep`, `find`, `ls`
* **Modification:** `sed`, `awk`, `patch`, `echo`
* **Validation:** `pytest`, `flake8`, `npm run build`

The Thunk worker is simply a bash-wielding grunt. If a bash command fails, `stderr` triggers the Thunk engine's Instant Death guardrail, the task is marked `failed`, and the error bubbles back up to the Brain to rethink the approach.

---

## The Execution Loop
1. **The Brain** decides a file needs refactoring. It calls the `Dispatch_Thunk` MCP tool with an intent and target files.
2. The MCP server generates a `task_xyz.json` file and drops it into `.thunk/`.
3. **The Muscle (`thunkd.py`)** wakes up. It formats a strict, amnesic prompt for the local 9B model: *"Here is your intent. You have `Execute_Bash`. Go."*
4. The 9B model runs bash commands until the task is complete, then writes `status: success` to the JSON file.
5. The MCP server reads the finished JSON and returns the final output to the Brain.

## Guardrails
The core invariants of Thunk v3 remain entirely intact:
1. **True Amnesia:** No chat logs across tasks.
2. **Fail Fast:** Tool errors result in instant task death. No silent retry loops.
3. **Anti-Yapping:** If a Thunk worker declares victory without ever executing a mutating bash command, the daemon overrides it and fails the task.
