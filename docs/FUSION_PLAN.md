# Pionir fusion plan

## Goal

Provide one user-facing agent that routes work to independently deployable specialists. Fusion
means shared control, contracts, scheduling, and evaluation—not flattening model weights,
databases, personalities, or histories.

## Intended responsibilities

| System | Intended role | Integration status |
|---|---|---|
| Atani | Executive policy, planning, approvals, verification, event ledger | Working CLI adapter and bounded plan surface |
| ~~Theo / Tech-Support~~ | ~~Human-facing personality, conversation~~ | **Removed from Pionir 2026-09-10.** Was a placeholder voice only; the voice is Galatea (not yet wired). See SOURCE_AUDIT.md. |
| Bryo / Terrarium | Resource governor, CPU-first learning, experiment gate | Working read-only status and shared GPU lease |
| ~~Probability~~ | ~~Measured autonomy, cumulative memory, gene evolution~~ | **Retired 2026-09-09.** Adapter removed from Pionir; its consolidation/memory ideas were harvested into the memory engine. See ARCHITECTURE_DECISIONS_2026-09.md. |
| ~~Autogenesis~~ | ~~Deterministic curriculum, candidate generation, transactional lineage~~ | **Retired 2026-09-09.** Adapter removed; harvested for parts. |
| ~~Genesis agent~~ | ~~Local life loop, emotional state, Tor research~~ | **Retired 2026-09-09.** Adapter removed; emotional meters and the Tor-fail-closed pattern harvested. |

The public `genesis` world-simulator repository must not be assumed to be the same system as
the local `genesis-agent` directory.

## Phases

### Phase 0 — contracts and inventory

- Define task, capability, memory namespace, and model lease contracts.
- Record exact entry points, ports, models, stores, permissions, and tests for every source.
- Benchmark idle RAM, peak RAM, VRAM, cold start, and warm task latency.
- Keep all source projects read-only.

Exit criterion: every specialist has a completed adapter checklist and no contract requires
copying private state into Pionir.

### Phase 1 — one shell, two proven adapters

- Implement the Atani executive adapter.
- Implement the Theo conversation adapter using authenticated local IPC.
- Add an append-only event ledger with secret redaction.
- Enforce one GPU lease at a time on the 12 GB target machine.

Exit criterion: a task can be planned, approved when needed, routed to Theo, verified, and
audited with deterministic failure behavior.

### Phase 2 — resource and specialist integration

- Integrate Bryo's governor behind the Pionir scheduler contract.
- Add Probability, Autogenesis, and Genesis one at a time.
- Add health checks, timeouts, circuit breakers, and adapter-specific rollback.

Exit criterion: one failed specialist cannot corrupt shared state or prevent the core agent from
serving other work.

### Phase 3 — gated self-improvement

1. Collect corrections and successful outcomes as reviewable lessons.
2. Generate candidate prompt, policy, or adapter changes.
3. Evaluate candidates in isolated simulations and held-out tasks.
4. Check permissions, evidence, resource limits, and regressions.
5. Require Ian's approval for promotion.
6. Roll out gradually and automatically restore the incumbent on failure.

No agent may directly promote its own code, weights, policy, or permissions.

## Target-machine resource policy

- Hardware baseline: 64 GB system RAM and 12 GB VRAM.
- Default GPU concurrency: one heavyweight inference or training lease.
- Training and interactive inference do not overlap.
- Large depth models are cold-loaded only for explicit depth tasks.
- CPU-first background learning yields to interactive work.
- Every model declares estimated VRAM plus context overhead before admission.

These are conservative admission rules, not benchmark claims. Phase 0 measurements replace
estimates with observed values.
