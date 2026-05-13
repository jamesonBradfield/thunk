# Feature: Git Chains of Hashes — MVP Test Report

## What Was Built

Implements **pass-by-reference context management** for the Thunk orchestrator.

**The problem:** When the Brain LLM dispatches a child via `Create_Thunk`, it may inline thousands of tokens of code into the `intent` field. After the child completes, that bloated tool call lives forever in the parent's conversation tape.

**The solution:** Workers commit their changes and return a git hash. The harness then collapses any oversized `Create_Thunk` intent in the parent's tape to a 40-character pointer. "Chains of hashes linked to chains of thoughts."

### Changes to `thunkd.py`

| Component | What it does |
|-----------|-------------|
| `INTENT_COLLAPSE_THRESHOLD` | Env var `THUNK_INTENT_COLLAPSE` (default 500 chars). Intents longer than this get collapsed. |
| `_extract_commit_hash(text)` | Regex for a bare 40-char hex string or `commit: <hash>` pattern in worker output. |
| `_collapse_intent_in_tape(messages, tool_call_id, hash)` | Walks the parent's message tape, finds the `Create_Thunk` call by `tool_call_id`, replaces its `intent` with `[COLLAPSED — refer to git commit <hash>]` if over threshold. |
| `_resume_ready_suspended()` | Now extracts the commit hash from each successful child result and fires `_collapse_intent_in_tape` before resuming the parent. |
| Retry loop in `_execute()` | 5 retries with 5 s × attempt backoff on `ServiceUnavailableError` / `RateLimitError` (handles model-still-loading 503). |
| System prompt | Workers instructed to commit target files and return the 40-char hash before declaring success. Uses `git reset HEAD; git add <target_files>; git diff --cached --quiet \|\| git commit -m '...'; git rev-parse HEAD` as a single call to prevent scope creep and double-commit death. |

### Changes to `mcp_server.py`

`Dispatch_Thunk` description updated to tell the Brain: keep `intent` high-level, never inline code, and that successful results include a git commit hash.

---

## Test Run — Qwen3.6-35B-A3B (Q4_K_M), 8 k ctx, localhost:8080

Model: `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf` via llama-server (ROCm, RX 7700 XT)  
Harness: thunkd v4, WSL2 / `/bin/bash`

### test_001 — ❌ Failed

**Intent:** Add `subtract(a, b)` to `fixtures/demo_a.py`, run it, commit.  
**Failure:** `Bash exit code 127: python: command not found`  
**Root cause:** System prompt said "bash worker" with no OS context; model assumed `python` (Windows/macOS default).  
**Fix:** Added "running in a Linux environment" and changed example to `python3`.

---

### test_002 — ❌ Failed

**Intent:** Same.  
**Failure:** `litellm.ServiceUnavailableError: 503 Loading model`  
**Root cause:** thunkd fired the first LLM call while llama-server was still warming up. Bare `except Exception` treated it as fatal.  
**Fix:** Added retry loop — 5 attempts, 5 s × attempt backoff — for `ServiceUnavailableError` and `RateLimitError`.

---

### test_003 — ❌ Failed

**Intent:** Same (with `python3` in task text as belt-and-suspenders hint).  
**Failure:** `Bash exit code 1: nothing to commit, working tree clean`  
**Root cause:** Worker successfully committed on the first call, then called `git commit` a second time in a separate `Execute_Bash` call. Second call returns exit code 1 → Instant Death.  
**Note:** The commit *did* land (`ac8a1b7`). The file was correct. Only the harness status was wrong.  
**Fix:** Collapsed the entire commit sequence into one mandatory `Execute_Bash` call:  
```
git reset HEAD; git add <target_files>; git diff --cached --quiet || git commit -m '...'; git rev-parse HEAD
```
`git diff --cached --quiet` skips `git commit` if nothing is staged (exit 0 either way). `git reset HEAD` clears any pre-staged junk so only target files get committed.

**Side effect caught here:** Worker's `git add -A` committed `thunkd.py` and `mcp_server.py` alongside the target file. Fixed by baking `git add <target_files>` (from `task.target_files`) into the system prompt instead of `-A`.

---

### test_004 — ✅ Success

**Intent:** Same.  
**Result:**
```
Final commit hash: `a2e3a773d2f9a83988cf0a3dabd4900255c914bd`
```
**Status:** `success`  
**Context usage:** ~1 300 tokens across 6 LLM calls (well within 8 k limit).  
**Commit verified:**
```
a2e3a77 Add subtract(a, b) function to fixtures/demo_a.py
```

---

## What "Chains of Hashes" Looks Like in Practice

**Without this feature** — parent tape after child completes:
```json
{ "role": "assistant", "tool_calls": [{
    "name": "Create_Thunk",
    "arguments": { "intent": "Write the router:\n```python\n... [2 000 lines] ...\n```" }
}]}
```

**With this feature** — same slot after `_collapse_intent_in_tape` fires:
```json
{ "role": "assistant", "tool_calls": [{
    "name": "Create_Thunk",
    "arguments": { "intent": "[COLLAPSED — refer to git commit a2e3a773...]" }
}]}
```

The parent resumes with a 40-character pointer instead of 2 000 lines. Git is the memory.
