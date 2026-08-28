# Atani bounded-executive contract

Pionir sends a JSON object to `atani executive` over standard input. The protocol identifier
is `atani.executive.v1`. Atani creates or resumes the goal, converts each declared step into an
action envelope, checks the exact capability/operation/resource through its broker, executes
only a closed registered tool, verifies the declared postcondition, and persists the outcome.

The request must contain exactly one of:

- `goal`: a new internal goal with `title`, `description`, and optional
  `success_criteria`; or
- `goal_id`: the identifier of an existing active goal.

Each step declares `tool`, `resource`, `reason`, `expected_outcomes`, `arguments`, and an
optional `idempotency_key`. The first implementation exposes `workspace.create_text`,
`workspace.edit_text`, `workspace.read_digest`, and `workspace.list`. Tool arguments cannot
name a different resource than the authorized action envelope.

Run [the example request](../examples/atani-plan.json) with:

```powershell
.\.venv\Scripts\pionir.exe run-atani-plan .\examples\atani-plan.json
```

This is an action boundary, not a remote shell. Unknown tools, absolute/traversing paths,
unverifiable outcomes, oversized requests, exhausted budgets, paused autonomy, and denied
capabilities fail closed. The output contains hashes and action identifiers, not raw private
memory.
