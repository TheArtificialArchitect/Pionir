# Self-improvement promotion gate

Pionir separates proposing, evaluating, authorizing, and applying an improvement. No component
may perform more than its assigned stage merely because it produced a promising candidate.

## Contract

1. A source agent proposes an immutable candidate reference, hypothesis, target, risk, and
   explicit rollback reference.
2. An isolated evaluator compares it with the incumbent on a named held-out suite.
3. Permission, resource-limit, rollback, and held-out checks must all pass.
4. Any recorded regression blocks promotion regardless of aggregate score.
5. The candidate must beat the incumbent by the configured minimum gain.
6. Ian approves the exact SHA-256 digest of the candidate record.
7. The gate returns an authorization decision; it does not deploy the change.

The split is intentional. Probability or Autogenesis may propose candidates, Genesis or
Terrarium may run trials, and Atani may verify evidence, but none of those systems can
manufacture Ian's approval or silently substitute a different candidate after approval.

## Still required before real promotion

- A durable candidate/evaluation ledger.
- Target-specific staged deployment adapters.
- Health thresholds and observation windows for canary releases.
- An automatic target-specific rollback executor.
- Signed or otherwise authenticated human approval when Pionir gains a network-facing UI.

Until these exist, `PromotionGate` is an authorization policy and testable contract—not a code
deployment mechanism.
