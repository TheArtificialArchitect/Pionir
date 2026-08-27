# Specialist adapter protocol

Pionir supports independently packaged specialists through one JSON document on standard input
and one JSON document on standard output. Standard error is diagnostic only. The command is an
operator-authored argument array and is always launched without a shell.

## Task request

```json
{
  "protocol": "pionir.task.v1",
  "task_id": "UUID",
  "agent_id": "probability",
  "capability": "forecast.probability",
  "payload": {}
}
```

## Task result

```json
{
  "protocol": "pionir.result.v1",
  "task_id": "same UUID",
  "agent_id": "probability",
  "output": {},
  "evidence": ["probability:model-run:123"]
}
```

Pionir rejects mismatched protocol versions, task IDs, agent IDs, non-object output, malformed
evidence, timeouts, nonzero exit codes, and documents larger than the protocol limit.

## Health request and result

Request:

```json
{"protocol":"pionir.health.v1","agent_id":"probability"}
```

Healthy result:

```json
{"protocol":"pionir.health.v1","agent_id":"probability","ok":true}
```

## Configuration

Specialists are declared in a TOML file outside source control, based on
`specialists.example.toml`. Set its absolute path through `PIONIR_SPECIALISTS_FILE`. Each
manifest declares its command, capabilities, permissions, memory namespaces, risk, priority,
timeout, and model-resource estimate.

This protocol is the preferred first integration for Probability, Autogenesis, Genesis, and
EvolutionaryAI because each can keep its own Python version, virtual environment, models, and
state. A direct library adapter should be used only when a source audit proves that shared
process state is safe and useful.
