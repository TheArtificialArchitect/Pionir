# Architecture decisions — 2026-09-08

What was settled this session about the shape of the whole agent estate, and —
because more than one session is working at once — **what must not be touched
yet, and in what order things come apart.** Read the "do not scrap" section
before removing anything from any repo.

## The settled shape

A single system with one job per component, each stripped of the standalone
"creature" baggage it carries today.

| Role | Bot | Status of the decision |
|---|---|---|
| Spine — route, schedule, admit, audit | **Pionir** | Keeps its whole job. Not merged into anyone. |
| Manager — decide who, delegate, approve, verify | **Atani** | Strips to a cold executive; her affect layer is removed and donated to the voice. **Not yet done.** |
| Governor — resource physics (sensor) | **Bryo** | Stays. Wired in as the *sensor*; Pionir stays the *gate*. |
| Coding — the only bot that touches code | **Daedalus** | Promoted from Theo-scoped to shell-scoped. Authorization must move to Pionir's gates. |
| Tools — pure executor | **Melete** | Same promotion as Daedalus. |
| Offense | **Nyx** | As-is. |
| Defense | **Voodoo** | As-is. |
| Memory — shared, retrieval-first | **new engine** | Cortex + Probability's consolidation, purpose-built. Verify Probability's code before merging. |
| Voice / Brain of Pionir | **Galatea** | Becomes the voice; keeps her whole self (interior + initiative are the asset, not baggage). |

Separate from all of the above: **a standalone conversationalist that replaces
Theo**, built with Galatea's method, self-tools + browser only, requesting its
own upgrades through a Discord relay. Brief:
[`the-voice-founding-brief.md`](the-voice-founding-brief.md). This one is not
part of Pionir and depends on nothing here.

## The seam decisions (the ones that were actually hard)

- **Brain vs Manager are two jobs, and the dividing line is the soul.** The
  Brain has emotions, opinions, will; the Manager must not, or you get an
  approver whose delegation is colored by mood. The Brain never touches a doer —
  it forms intent and hands it to the Manager, who decides who, checks
  authorization, delegates, verifies, and hands the result back to be spoken.
- **Bryo is the sensor; Pionir is the gate.** Both touch VRAM. Bryo owns
  continuous resource physics (RSS ladder, pagefile tripwire, host-load torpor)
  and *reports*; Pionir owns the decision to load a heavyweight model and
  *records* it. Bryo never blocks a lease itself; Pionir never runs the RSS
  ladder itself. The `vram_probe` seam in `scheduler.py` is already injectable,
  so this needs no rewrite — Bryo plugs into it. Pionir's `doctor` now surfaces
  `shared_gpu_lock_path` so Bryo is pointed at the exact file rather than
  deriving it (HEAD 3.9).
- **Memory's fix for forgetting is retrieval, not a bigger window.** Recall the
  relevant few memories each turn; consolidate conversations into episodes as
  they scroll out. The window size stops mattering.
- **Emotions unify into one system, not three side by side.** Appraisal (Atani),
  two-timescale mood (Galatea), persistent meters (Genesis), honest range
  (Theo). One coherent model in whichever agent carries emotion.

## DO NOT SCRAP OR STRIP YET — these are harvest sources

The new voice agent's brief instructs the building session to **pull working
concepts from Galatea, Theo, Atani, Genesis, and Probability.** Until that
session has taken what it needs, every one of these must stay intact and
runnable. Scrapping or stripping them early destroys the source.

- **Theo** — the Cortex memory engine, the honest affect range, the web
  discipline. Do not begin the Theo rebuild or delete anything until the voice
  session confirms it has Cortex and the affect model.
- **Atani** — the appraisal/catalog/channel affect module is a donor to the
  voice's emotion system *and* the thing being stripped out to make her a cold
  manager. **The strip and the harvest are the same act on the same code** —
  harvest first, strip second, never the reverse.
- **Genesis agent** — the emotional meters and the Tor-fail-closed research
  pattern are wanted; the proposal-only growth pattern is the model for the
  Discord upgrade-request channel. Retire it *after* those are lifted.
- **Probability** — its consolidation and curiosities feed the new memory
  engine, but its code is un-vetted ("no idea if it works"). Read and test it in
  isolation before a row of it touches the shared memory. A bad memory layer
  poisons everything downstream (HEAD 3.13: 97% → 81% on one bad import).
- **Galatea** — the whole method spine. Obviously kept.

Safe to scrap now: nothing. Safe to *plan* to scrap after harvest: Autogenesis
(harvest anything useful first), Genesis agent (keep the meters), and
Probability's 24/7 organism half once its memory parts are lifted.

## Order of operations

1. **Additive first** (no removal): build the new voice agent against stubs;
   measure Pionir's routing aim (done — 7/7 on live capabilities); make the
   Bryo/Pionir lock contract observable (done). This document.
2. **Harvest** the donor concepts into the new voice and the new memory engine,
   with every source bot still intact.
3. **Strip** only after harvest: Atani → cold manager, promote Daedalus/Melete
   with authorization moved to Pionir's gates.
4. **Scrap** only after strip: the organism halves nobody needs.

Do steps out of order and you lose a harvest source or wire an inert component.
Both are the estate's most expensive, best-documented failures.

## Decided 2026-09-08

- **Pionir owns Daedalus's and Melete's action authorization.** Once promoted out
  of Theo's scope, their actions flow through Pionir's permission gates and audit
  ledger, not Theo's. This is the boundary that made `/chat/send` get declined for
  the voice — resolved in Pionir's favor.

- **Memory: lexical, and Pionir's own for now.** Recall is BM25 + recency, no
  embedding model on the card. `cortex.py` is built and wired into Pionir alone;
  the standalone conversationalist builds its own in its session, and the two are
  compared later rather than forced onto one library now. Semantic recall stays a
  documented addition that re-ranks the same candidate set, not a rewrite — added
  only if measured recall quality asks for it.

## Memory engine — open items (built core, deferred by choice)

The core store is built, wired, and driveable from the CLI. These are the next
pieces, none urgent, none needing the strip:

- **A recall eval (`recall-check`).** Like `route-check`, but for memory:
  known-answer probes measuring recall@k. It is the instrument that decides the
  lexical-vs-semantic question for real, and the guard that catches an import
  regression (a memory system read 97% then 81% on one bad batch — HEAD 3.13).
  Build this before trusting recall quality by feel.
- **Link expansion.** `recall()` returning one hop of `[[slug]]`-linked memories
  alongside the direct hits. Slugs and links are already stored; the expansion is
  the unbuilt half.
- **The consolidation seam.** A model-free interface for a caller (the voice, a
  distill pass) to fold raw messages into episodes and extract facts. The engine
  stays model-free and testable; the distilling lives in the caller. Do not build
  it until something is writing conversations for it to fold.

## Open decisions, not yet made

- Whether the new standalone conversationalist and Galatea-the-voice stay two
  agents or eventually converge. For now: two.
- Whether Pionir's memory and the standalone agent's memory converge later, once
  both exist and can be compared.
