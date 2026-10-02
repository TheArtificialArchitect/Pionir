# The Pionir economy - design (one page, for Ian's review)

**Goal.** Give the crew a currency, a store, homes and (later) a bank, so that earning is
tied to work that is *checked* and spending changes what an agent can actually do. Fun is a
goal; so is making the real caps (Claude calls, GPU time, autonomy) behave like a market
instead of a rulebook. **Nothing in it is real money**: Pionir currency never converts to
dollars and never authorises a real spend. Real money keeps its existing gate (Ian's approval
on Discord / the Approvals tab).

## The one rule that makes it work
**Pay only on an outcome Pionir can check without asking the agent.** Bryo's reward machinery
reported 4,314 successful practice runs while it never used a habit it had learned: a reward
on self-report teaches reporting. So payouts come from the *verifier*, never the earner.

| Pays | Verified by |
|---|---|
| Build passed its checks and was staged | the static + verify checks (not Daedalus's say-so) |
| Post / product / listing approved | Ian's approval, recorded by the gate |
| Order delivered | Fiverr/Scrooge event (`completed`) |
| Revenue arrived | Scrooge ledger |
| **Excellence bonus** | approved with **no edits**, revenue from the work, or Ian's explicit rating |

Failure is not fined at first (a latch needs a way back): an agent that fails earns nothing
and its work is retried by the existing rules.

## Pieces (built in this order, each useful alone)
1. **Ledger** - append-only, one row per payment or purchase (who, why, amount, the
   verifying event id). Balances are *derived* from it, never stored twice (two stores
   desync). Hash-chained so tampering shows. Lives in Pionir; the crew reads it.
2. **Payout rules** - deterministic code, no model: `event -> amount`. A table Ian can read
   and edit. Daily mint cap per agent so a bug cannot print money.
3. **Store** - sells only real, bounded things:
   - *Resources*: Claude review calls (inside the nightly cap), GPU lease priority, a larger
     model tier for a task.
   - *Autonomy*: e.g. one approval-free repeat of an action Ian has already approved 10x.
     Never anything on the money / customer-facing list; never past a hard gate.
   - *Design choices*: names, colours, avatar, voice style, home decor. Cosmetic, cost
     nothing real, a good sink.
4. **Homes** - each named agent (Moss, the leaders, Atani, Bryo) gets a persistent home: a
   small record (rooms, furniture, trophies of what it built) bought with currency. Plain
   data first, shown on the dashboard. It is also the seed of the room/VR idea: the same
   record later drives a 3D scene. Personality-free workers get no home, by design.
5. **Plutus, the analyst** (name to be cleared against `known_names`) - an independent
   *observer*, not a participant. It cannot mint, pay or spend. It watches the ledger and
   reports: money supply, what the store sells vs. what is bought, inflation, and **gaming
   signals** (an agent whose earnings rise while Ian's approvals fall; payouts clustered on
   one cheap task type; a verifier that never says no). It writes a weekly digest card.
   Its core is plain statistics; a model only words the report. The thing that audits the
   economy is never paid by it.
6. **Bank + paper investing (later)** - savings with interest from a treasury, loans between
   agents, and paper holdings fed by the Mr-Crab / Hemera paper books. Pure play money.

## Guard rails
- Payout path and the money gate stay separate code; a test proves no currency balance can
  authorise a gated action.
- **Check the output counter, not the verdict log**: Plutus reports payouts *made*, and a
  zero is shown as a zero, never hidden.
- A purchase that raises an agent's power is reversible (a lease or token that expires), so
  nothing is a one-way door.
- Tests never touch the real ledger or `~/.pionir`.

## Decided by Ian (2026-10-01)
1. The currency is **Bolts** (Zeus's bolt). Symbol: a bolt count, whole numbers only.
2. **Workers get accounts and homes too**, not only named agents: the point is that every
   agent can work, earn and upgrade, and that the ones who come up with better things earn
   more and get better things. Personality stays out of workers' *speech*; a home and a
   balance are data, not personality.
3. Defaults accepted: no fines at first; excellence = no-edit approvals + revenue (+ Ian's
   rating on any card); build step one first (ledger + payout table + read-only dashboard
   tab, no store yet).
4. Upgrades stay bounded: nothing bought can pass a hard gate (money, customers, publishing,
   deploys), and every power-raising purchase expires or can be revoked.
