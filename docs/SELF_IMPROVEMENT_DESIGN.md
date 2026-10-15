# Self-improvement worker (`builds.improve`) - design

Ian's decisions (2026-10-12, final): it changes the bots' settings, prompts, check rules and code -
always as a diff to the Pionir repo, never weights or training; Daedalus writes the patch and Claude
reviews it (the night-build split, the night Claude cap and cheaper model); every change reaches Ian
as a card and only his hand applies it; one pass a night, at most two proposals a night;
Pionir only. It follows `IMPROVEMENT_GATE.md` and `FUSION_PLAN.md` Phase 3: propose, evaluate in
isolation, check permissions and regressions, Ian decides, the gate applies nothing on its own,
nothing of the patch is ever run as Ian, and a change can be undone.

**Live mode: detect + draft a fix for Ian to apply by hand (2026-10-14).** Because the guard's
protected closure now leaves nothing auto-patchable, every real cluster is apply-by-hand - so
instead of a bare "needs a human" alert the worker DRAFTS a fix for it: Daedalus writes the patch
and its regression test in the sandbox (this works for protected files too - it is only a draft,
never committed by the bot), it is verified and reviewed exactly as an auto-apply candidate would
be, and Ian gets a card showing the evidence, the verified diff, Claude's verdict, paste-ready
apply/undo steps, and the clear mark **apply by hand - the bot will not commit this**. Approving a
draft card only acknowledges it. The auto-apply path (`improve.apply` commits one patch on Ian's
yes) stays, gated behind the guard's patchable check, and is dead while nothing is patchable.

Revised after an adversarial review (NO-GO on the first version): the patch is FROZEN before it is
verified, the guard is an allowlist, verification cannot be gamed by import errors or tautologies,
the whole diff is on Ian's card, the GPU is used only at night and Moss's model is sidelined only
while she is idle, and one lease keeps every user of the build sandbox apart.

## Pipeline (one candidate)

| state | who | what |
|---|---|---|
| `detected` | worker (no model) | a **cluster**: one worker + one normalised reason (digits -> `N`) seen `>= 5` times in 7 days and in the last 48 h; the same failure seen from two sources (a failed run and its lesson) is ONE cluster. If its fix lives in a PROTECTED file (e.g. `crew/etsy/rules.py`), it is drafted all the same - those files become the targets Daedalus writes against, and the card is apply-by-hand. |
| `writing` | Daedalus (contained) | a fresh clone of `main` in the sandbox; `coding.daedalus_build_quiet` - like a night build it may sideline Moss's model, but ONLY while she is idle (no turn with Ian for 30 min, nobody typing - asked under the lease before anything is sidelined; active -> the night is skipped), and the lease ends with the job; its window is 05:45-07:00 (Moss is re-asked every minute; any turn cancels the build), a start any time the job still fits once the night builds are done (still busy at the last start: the night is skipped, saying so). Its gate runs only `tests/test_regression_<id>.py`. |
| `verifying` | worker (contained, CPU) | the patch is read from the sandbox ONCE and FROZEN in `~/.pionir/improve/<id>/` (sha256 recorded). Every later step uses those bytes on TRUSTED trees: the owner's base in a private clone in the owner's temp folder, the frozen patch applied there. Guard first (below); then the regression test must fail **by an assertion** on base and pass with the change; then no test may fail with the change unless main fails it the same way (per test: `failure:X` / `error:X`). |
| `reviewing` | Claude (one call, no tools) | the frozen diff + evidence + our results; only between 01:00 and noon (so it is charged to that night's cap). A rejection, a failed verification or a failed Daedalus attempt gets ONE repair; then `shelved`. |
| `awaiting_owner` | Pionir gate | a PATCHABLE fix only: the frozen bytes re-hashed and re-guarded; `improve.apply {id, digest}` - digest = sha256 over (patch, base, tests) - parked on every call; the card shows the WHOLE diff (<= 6,000 chars). (Dead while nothing is patchable.) |
| `drafted` | Pionir gate | a fix in PROTECTED code: the same frozen bytes and digest, parked as an **apply-by-hand** card - evidence, verified diff, Claude verdict, paste-ready `cd` / `git apply` / test steps and undo, marked "the bot will not commit this". Terminal: approving it only acknowledges it (`execute` commits nothing); Ian applies it himself. 14-day rest before the same cluster is drafted again. |
| `applied` / `stale` / `denied` | Pionir, on Ian's yes | a PATCHABLE fix: the bytes read once and applied: `main`, no tracked change, nothing of his (untracked or ignored) where the patch writes, base an ancestor of HEAD; `apply --cached`, the guard again on his index, ONE commit naming the id; nothing of it is run as Ian (its tests ran contained, on these bytes). Never a push or restart. |
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
the self-improvement code). On 2026-10-14 that leaves 0 modules auto-patchable (the six that are
not protected as files - `crew/__main__.py`, `crew/claudedraft.py`, `doctor_fix.py`,
`memory_health.py`, `video/fetch.py`, `video/sponsor.py` - each still imports a protected name,
so the whole-file rule below refuses an auto-apply to it): every fix is apply-by-hand, which is
why the worker now DRAFTS them. **Patchable by construction:** a module is auto-patchable only if
its WHOLE resulting file (the full post-patch AST, not only the added lines) imports nothing
protected - no `import`/`from` of any protected module or a name from one, at any level - and uses
no reflective or dynamic primitive (exec/eval/compile, `__import__`, importlib, set/del/getattr
with a computed name, vars/globals/locals, `__dict__`/`__class__`/dunder setters, `sys.modules`,
operator setters, builtins); a changed file may never reach into another module; a test a patch
leaves imports only the standard library, declared dependencies and patchable modules. The taint
check (a name bound from a protected module may only be read and called) stays as defence in
depth; the whole-file import rule is the guarantee. At the source, the grant, scope, spend and
payout tables are immutable. A patch whose ONLY refusal is that it edits protected files is not
rejected: it is a draft for Ian to apply by hand (`guard.only_protected_files`). Any other refusal
still shelves it, whether it would be auto-applied or drafted.
Refused also: deletions, binaries, links, file modes, fewer real asserts in any test (a
tautology is not one), removed tests, skip markers, test code touching the test machinery, any
import of something Pionir does not import already (AST, anywhere in the file), any new
`__import__`/`import_module`, and more than 8 files / 400 lines / 6,000 characters.

## One user of the sandbox at a time

`build_sandbox.SandboxLease`: a cross-process lock held by a build Daedalus for its whole life,
the night builds' and the API builder's contained checks, this worker's runs and the video
tutorial runner. Whoever finds it held waits (never a failure) and never reaps.

## Dedupe, caps, visibility

Never proposed again while open; 30 days after `denied`/`undone`; 14 after `shelved`, an
unanswered card or a `drafted` card; 3 nights after `stale`; after `applied` only with 5 NEW
sightings. At most 2 candidates start a night. The tally counts proposals, drafts and applied; the
pulse goes amber (VITALS **NO OUTPUT**) after 3 nights with clusters and no proposal, saying why.
`python -m pionir improve status` lists every candidate. Without the sandbox (Python part only;
node not needed) or with `PIONIR_AUTH_COMPAT` on: NOT SET UP, nothing runs.
