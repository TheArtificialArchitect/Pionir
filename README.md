# Pionir

Pionir is the integration host for Ian's family of specialist AI agents. It presents one
coherent assistant while preserving each specialist's identity, state, permissions, and
learning loop behind explicit adapters.

Pionir is an orchestrator, not a repository merger. Existing projects remain independently
runnable and are integrated through a small capability protocol.

## Design rules

1. Only one heavyweight GPU model is leased at a time by default.
2. Memory is namespaced; private agent state is never imported implicitly.
3. Tool access is declared per capability and checked before dispatch.
4. Improvements are evaluated and approved before promotion.
5. Every routed task and state-changing action is suitable for an append-only audit event.
6. Source agents remain untouched until their adapter is tested against the contract.

## Initial architecture

```text
User or client
    -> Pionir executive
        -> capability registry
        -> permission/evidence gate
        -> model lease scheduler
        -> namespaced memory
        -> specialist adapter
```

The first implementation is deliberately dependency-free. It establishes the contracts that
Atani, Theo, Bryo/Terrarium, Probability, Autogenesis, Genesis, and EvolutionaryAI can implement
without requiring their codebases to be copied into Pionir.

## Development

```bash
python -m unittest discover -s tests -v
```

See [docs/FUSION_PLAN.md](docs/FUSION_PLAN.md) for the staged integration plan and
[docs/ADAPTER_CHECKLIST.md](docs/ADAPTER_CHECKLIST.md) for the source audit template. Confirmed
source interfaces and deferred boundaries are recorded in
[docs/SOURCE_AUDIT.md](docs/SOURCE_AUDIT.md).

## Security

Never commit transcripts, embeddings, model weights, databases, credentials, local `.env`
files, or the contents of `.techsupport_agent`. Pionir stores only configuration and adapter
code in Git; runtime state belongs in ignored, access-controlled storage.
