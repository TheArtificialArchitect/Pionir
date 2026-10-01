# products.api_builder - design (one page)

**Goal.** Grow the Dokaz API (api.dokaz.net, the Scrooge Worker) one paid product at a
time, without a human writing it and without any step that spends money, deploys or touches
production unless Ian says yes.

**What a product is** (Scrooge `worker/src/products/<id>.ts`, registered in `index.ts`):
the `Product` interface (`id`, `name`, `summary`, `docs[]`, `handle`, `health`), a test file,
and a `Hit` line per endpoint in `tools/smoke.ps1`. Metering, auth and billing are generic,
so a stateless product needs no D1 table. Product ids are lowercase letters only.

## The pipeline (one product at a time, nothing runs unattended past a gate)

1. **Backlog** - `~/.pionir/apibuilds/backlog.json`, owner-editable (reply on the card:
   `add`, `remove`, `top`). Each entry is the whole product on paper: id, name, summary,
   endpoints with example request/response, the limits it will state, and WHY it should sell
   (who searches for it). Seeded with a few ideas marked **unvalidated**; Moss may reorder
   from traffic data but never invents an entry.
2. **Write** - Claude on Ian's Max (`claude -p`, never the API), in the *Write-only,
   empty-directory* runner the Fiverr website worker already uses. It cannot read, run or
   fetch anything, so the brief carries the conventions. It returns files; nothing it says
   is trusted.
3. **Check (static, ours)** - every returned byte is validated before it touches a repo:
   allowed paths only (`src/products/<id>.ts`, `test/<id>.test.ts`, the smoke line, the
   registry line), size caps, no `fetch`/`eval`/`Function`/dynamic import/`process`/secrets,
   no network except what the product declares, no new dependency, product id unused.
4. **Stage** - applied to a branch `api/<id>` of a git worktree of Scrooge, never `main`.
   Committed locally. Not pushed.
5. **Card** - a Discord / Approvals card to Ian: product, endpoints, diff stat, the static
   checks that passed. **His yes is what allows step 6** - model-written code is not run as
   Ian before a human has seen the card (the same reason night builds use a sandbox user).
6. **Verify (on his yes)** - `tsc --noEmit` and `vitest run` in the worktree, incl. the
   openapi-drift and smoke-coverage tests. Red = reported with the output, one repair pass,
   then shelved. Green = branch is ready.
7. **Release stays Ian's** - the worker never runs `deploy.ps1`, never writes D1 and never
   publishes SDKs. It reports "branch api/<id> is green; run `tools\deploy.ps1` from
   the worktree". (A later step may route the deploy itself through the approval gate.)

## Rules inherited (do not weaken)
- Max via CLI only; counted against the shared daily Claude cap and the division share.
- No model words reach customers: product copy comes from the backlog entry, checked.
- Fails closed; every refusal has a typed reason on a row. A latch has a way back
  (owner `retry <id>` / `remove <id>`).
- Tests never touch Claude, Scrooge's real repo, `~/.pionir` or the network.

## Open decisions for Ian (defaults in brackets, built on unless he says otherwise)
1. Idea source [owner's backlog + Moss's reorder; no auto-invention].
2. Release [Ian runs deploy.ps1 himself].
3. Price/plan [none: new products ride the existing plans; no per-product price].

## Update: Daedalus writes, Claude only reviews and edits

This supersedes the "Claude writes the product" parts above. The flow per product:

1. Overnight (the 01:00-07:00 window, one new product a night) Pionir generates a small
   self-contained scaffold repo in the build sandbox (`api-<id>`): a trimmed Product
   interface, env and openapi helpers, one model product, a vitest config and BRIEF.md.
   It is never the real Scrooge repo.
2. Daedalus (local model, contained sandbox user, free) writes the three product files via
   the Builds division's `coding.daedalus_build` job; its gate runs `tsc` and `vitest` with
   the sandbox's node (absolute paths from `setup.json`). No node set up: NOT SET UP, nothing built.
3. Pionir re-checks statically (tamper, banned constructs, spec) and re-runs the tests
   contained. A failure goes back to Daedalus with the reasons, then is shelved.
4. Claude does ONE review (no tools; `claude_model`, default `claude-sonnet-5`, per-night cap
   3 inside the daily cap). If it finds fixable problems: at most ONE edit pass (Write-only),
   re-checked and re-tested; still red means shelved. An edited product is staged without a
   second review; the owner's approval of the real verify is the final gate.
5. Staging onto Scrooge `api/<id>` and the approval-gated real `tsc` + `vitest` are unchanged.
   Scaffold-vs-real drift is caught by that verify.
