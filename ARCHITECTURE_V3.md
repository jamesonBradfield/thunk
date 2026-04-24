# Thunk v3: The Headless Automaton Architecture

## The Core Philosophy
Thunk is no longer a conversational agent or an in-memory Hierarchical Finite State Machine (HFSM). It is a **headless, functional, I/O-bound AI router**. 

Local LLMs (7B-9B parameters) degrade rapidly when burdened with conversational history, orchestration logic, and context switching. To maximize their capability, we treat the LLM as a stateless CPU. The memory, state, and complex execution logic live entirely on the filesystem inside a `.thunk/` queue. 

**We enforce three strict principles:**
1. **True Amnesia:** Agents do not have chat logs. Their entire existence is defined by a single, static JSON prompt. If an agent fails, we do not append the error and ask it to try again. We terminate it and rewrite the prompt. 
2. **Dependency Injection:** Agents do not search for tools. The harness injects only the specific tools (e.g., `ASTSplice`) required for the immediate atomic task.
3. **The Illusion of Self-Execution:** Complex tasks are solved via hierarchical delegation, managed silently by the Harness. 

---

## The Engine: `thunkd.py`
The engine is a dead-simple Python daemon that polls a `.thunk/` directory for task files. 
A task file is a JSON contract:
`{"id": "task_1", "intent": "...", "target_files": [...], "allowed_tools": [...], "status": "pending"}`

**The Execution Loop:**
1. Watcher sees a `pending` file and claims it.
2. Formats a strict LiteLLM prompt using only the `intent` and injected `allowed_tools`.
3. If the LLM generates a valid tool call, the Harness executes the standalone Python script (e.g., Tree-sitter AST tools).
4. If the task completes, it writes the result to the JSON file and marks `status: success`.

---

## The Interceptor: `Create_Thunk`
To solve complex bugs without context bloat, agents can spawn sub-agents. However, the parent agent must remain completely unaware that it is managing a multi-agent hierarchy. 

**The Schema:**
```json
{
  "tool": "Create_Thunk",
  "parameters": {
    "intent": "The immediate atomic goal.",
    "target_files": ["Explicit boundaries. No guessing."],
    "parent_context": "Crucial constraints (e.g., 'I already tried X, do Y instead.').",
    "tools": ["Tools the child needs to succeed."]
  }
}

The Mechanics (The Suspended Queue):

    Parent calls Create_Thunk.

    Harness intercepts the call. It creates a new child_xyz.json file in the queue.

    Crucial: The Harness marks the parent's status as suspended and kills the parent's thread to prevent deadlocks and thread exhaustion.

    The Watcher loop processes the child.

    When the child hits success or failed, the Harness wakes the parent back up (status: pending), and injects the child's final output as the direct return value of the Create_Thunk tool.

    The parent thinks the tool simply ran a script and succeeded.

Strict Guardrails (Anti-Rube-Goldberg)

We do not allow silent retry loops or hidden hallucinations. We fail fast.

    Context Starvation Prevention: A child cannot be spawned without an explicit target_files array and parent_context. The child must wake up with perfect situational awareness.

    Instant Death on Tool Failure: If a child calls a tool with bad JSON or out-of-bounds byte ranges, the Harness does not feed the error back for a retry. The child is instantly marked failed, and the error bubbles up to the parent to adjust its delegation strategy.

    Anti-Yapping (Premature Victory): If an LLM is given action-oriented tools (like ASTSplice) but exits its generation loop with a verbal success message without ever calling the tool, the Harness overrides the success. It marks the task failed with the error: [Harness Error] Child agent hallucinated completion without executing mutating tools.

Theoretical Backing

This architecture is built on the proven mechanics of Deterministic Pushdown Automata and structured generation.

    We bypass the "Lost in the Middle" context degradation by strictly enforcing single-turn, amnesic task files.

    We decouple reasoning from syntax generation by forcing the LLM to output pure JSON tool calls, leaving the fragile source-code manipulation to deterministic Tree-sitter parsers (ExtractAST, ASTSplice).

    The .thunk/ directory acts as the tape of a Turing Machine, allowing infinite recursive logic depth while keeping the active LLM context under 1,000 tokens.
