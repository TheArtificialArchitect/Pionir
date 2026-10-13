# Self-improvement worker (`builds.improve`) - design

Ian's decisions (2026-10-12, final): it changes the bots' settings, prompts, check rules and code -
always as a diff to the Pionir repo, never weights or training; Daedalus writes the patch and Claude
reviews it (the night-build split, the night Claude cap and cheaper model); every change reaches Ian
as an approval card and only his yes applies it; one pass a night, at most two proposals a night;
Pionir only. It follows `IMPROVEMENT_GATE.md` and `FUSION_PLAN.md` Phase 3: propose, evaluate in
isolation, check permissions and regressions, Ian approves the exact digest, the gate applies
nothing on its own, nothing of the patch is ever run as Ian, and an applied change can be undone.

Revised after an adversarial review (NO-GO on the first version): the patch is FROZEN before it is
verified, the guard is an allowlist, verification cannot be gamed by import errors or tautologies,
the whole diff is on Ian's card, the GPU is used only at night and Moss's model is sidelined only
while she is idle, and one lease keeps every user of the build sandbox apart.

## Pipeline (one candidate)

| state | who | what |
|---|---|---|
| `detected` | worker (no model) | a **cluster**: one worker + one normalised reason (digits -> `N`) seen `>= 5` times in 7 days and in the last 48 h; the same failure seen from two sources (a failed run and its lesson) is ONE cluster. If its own words live only in a PROTECTED file (e.g. `crew/etsy/rules.py`), it is reported as **needs a human** and nothing is cloned or run. |
| `writing` | Daedalus (contained) | a fresh clone of `main` in the sandbox; `coding.daedalus_build_quiet` - like a night build it may sideline Moss's model, but ONLY while she is idle (no turn with Ian for 30 min, nobody typing - asked under the lease before anything is sidelined; active -> the night is skipped), and the lease ends with the job; its window is 05:45-07:00 (Moss is re-asked every minute; any turn cancels the build), a start any time the job still fits once the night builds are done (still busy at the last start: the night is skipped, saying so). Its gate runs only `tests/test_regression_<id>.py`. |
| `verifying` | worker (contained, CPU) | the patch is read from the sandbox ONCE and FROZEN in `~/.pionir/improve/<id>/` (sha256 recorded). Every later step uses those bytes on TRUSTED trees: the owner's base in a private clone in the owner's temp folder, the frozen patch applied there. Guard first (below); then the regression test must fail **by an assertion** on base and pass with the change; then no test may fail with the change unless main fails it the same way (per test: `failure:X` / `error:X`). |
| `reviewing` | Claude (one call, no tools) | the frozen diff + evidence + our results; only between 01:00 and noon (so it is charged to that night's cap). A rejection, a failed verification or a failed Daedalus attempt gets ONE repair; then `shelved`. |
| `awaiting_owner` | Pionir gate | the frozen bytes re-hashed and re-guarded; `improve.apply {id, digest}` - digest = sha256 over (patch, base, tests) - parked on every call; the card shows the WHOLE diff (<= 6,000 chars). |
| `applied` / `stale` / `denied` | Pionir, on Ian's yes | the bytes read once and applied: `main`, no tracked change, nothing of his (untracked or ignored) where the patch writes, base an ancestor of HEAD; `apply --cached`, the guard again on his index, ONE commit naming the id; nothing of it is run as Ian (its tests ran contained, on these bytes). Never a push or restart. |
| `undone` | Ian | `python -m pionir improve undo <id>` = `git revert` of that commit. |

## The guard (`selfimprove/guard.py`)

ALLOWLIST: Python modules under `src/pionir/` (not a new top-level one, never `__init__.py`,
`*customize.py`, `bootstrap.py`, `.git*`) and `tests/test_*.py`, plus the catalogue's settings.
PROTECTED (listed with reasons, each tested): the approval gate and its surfaces, auth, money and
spend rules, secrets handling, content and listing rules, the build sandbox and everything that
runs in it, the GPU lease, CI, adapters, the crew's wiring, the test harness, dependency files, the
self-improvement code and its tests, and every module that code imports (computed by a test) -
and, computed at run time from the tree itself, every module any protected file LOADS, and
every module the SECURITY CORE imports at all (`guard.CORE`: the approval gate and cards,
auth, money/spend/Claude caps, secrets, the build sandbox, the GPU lease, the executive, the
adapters, the night builds, the crew's runtime/workers/leader/pool/vitals/clock/products and
the self-improvement code). On 2026-10-13 that leaves 6 of 217 modules patchable
(`crew/__main__.py`, `crew/claudedraft.py`, `doctor_fix.py`, `memory_health.py`,
`video/fetch.py`, `video/sponsor.py`): most clusters are reported as needing a human.
A changed file (src or tests) may never reach into another module - structurally, over the
added code: no reflective or dynamic primitive at all (exec/eval/compile, `__import__`,
importlib, set/del/getattr with a computed name, vars/globals/locals, `__dict__`/`__class__`/
dunder setters, `sys.modules`, operator setters, builtins); no NEW import of a protected module
or of a name from one; and TAINT - a name bound from a protected module, through assignment,
walrus, for/with/comprehension targets, unpacking and defaults, may only be read and called:
no store, del, augmented assignment, mutating method, and no passing it to a call, returning
it or putting it in a container. Tests patch only unprotected modules, in a `with` or
decorator. At the source, the grant, scope, spend and payout tables are immutable.
Refused also: deletions, binaries, links, file modes, fewer real asserts in any test (a
tautology is not one), removed tests, skip markers, test code touching the test machinery, any
import of something Pionir does not import already (AST, anywhere in the file), any new
`__import__`/`import_module`, and more than 8 files / 400 lines / 6,000 characters.

## One user of the sandbox at a time

`build_sandbox.SandboxLease`: a cross-process lock held by a build Daedalus for its whole life,
the night builds' and the API builder's contained checks, this worker's runs and the video
tutorial runner. Whoever finds it held waits (never a failure) and never reaps.

## Dedupe, caps, visibility

Never proposed again while open; 30 days after `denied`/`undone`; 14 after `shelved` or an
unanswered card; 3 nights after `stale`; after `applied` only with 5 NEW sightings. At most 2
candidates start a night. The tally lists open candidates and clusters needing a human; the pulse
goes amber (VITALS **NO OUTPUT**) after 3 nights with clusters and no proposal, saying why.
`python -m pionir improve status` lists every candidate. Without the sandbox (Python part only;
node not needed) or with `PIONIR_AUTH_COMPAT` on: NOT SET UP, nothing runs.
