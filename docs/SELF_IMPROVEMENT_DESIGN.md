# Self-improvement worker (`builds.improve`) - design

Ian's decisions (2026-10-12, final): it changes the bots' settings, prompts, check rules and code -
always as a diff to the Pionir repo, never weights or training; Daedalus writes the patch and Claude
reviews it (the night-build split, the night Claude cap and cheaper model); every change reaches Ian
as an approval card and only his yes applies it; one pass a night after the build window, at most two
proposals a night; Pionir only. It follows `IMPROVEMENT_GATE.md` and `FUSION_PLAN.md` Phase 3:
propose, evaluate in isolation, check permissions and regressions, Ian approves the exact digest,
the gate applies nothing on its own, and an applied change can be undone.

## Pipeline (one candidate)

| state | who | what |
|---|---|---|
| `detected` | worker (no model) | a **cluster**: one worker + one normalised reason (digits -> `N`, case and spaces folded) seen `>= threshold` (5) times in `lookback_days` (7) - with count, first/last seen, 3 sample lines, a templated hypothesis and target files (the source files that contain the reason's own words). |
| `writing` | Daedalus (contained, GPU lease) | a FRESH clone of Pionir's `main` HEAD inside the sandbox workspace (`<sandbox>\pionir-imp-...`, marked, no remote, canonical config - `sandbox_repo_problem` passes). The intent carries the evidence, the protected list and the rule: add `tests/test_regression_<id>.py` reproducing the failure. Its gate runs only that file. |
| `verifying` | worker (contained, CPU) | **hard refusals first** (`selfimprove/guard.py`): protected paths, deleted/renamed tests, net-removed asserts, added skip markers, new dependencies, binary/link changes, size cap. Then, as `pionir-builds` in a job object: the regression test FAILS on base + test, PASSES on the change; the full suite on the change has no failure the base suite does not have (the base run is cached per base commit). |
| `reviewing` | Claude (one call, no tools) | diff + evidence + our results; a JSON verdict, approved only with every check true. A rejection (or a failed verification, or a failed Daedalus attempt) gets ONE Daedalus repair with the reasons, re-checked and re-reviewed; then `shelved`. Budget spent = waiting, never approval. |
| `awaiting_owner` | Pionir gate | `improve.apply` parked (PRIVILEGED, every call, never batched): evidence, diff (excerpt + full file path), verdict, undo command; payload = `{id, patch_sha256}`. |
| `applied` / `stale` / `denied` | Pionir, on Ian's yes only | main clean (tracked files), on `main`, base an ancestor of HEAD, `git apply --check` clean -> ONE commit naming the id, targeted tests re-run; red -> reverted automatically (`undone`). Moved and not applying -> `stale, will re-propose`. Never pushes, never restarts. |
| `undone` | Ian | `python -m pionir improve undo <id>` = `git revert` of that commit. |

## Sources (read-only)

crew.db `outputs` rows whose kind ends in `blocked/rejected/shelved/not_listed/attempt_failed/
review_failed` (their `reasons`), crew.db `runs` errors, the `VITALS:` lines of `crew.log`,
`builds.daedalus.json` failed attempts and rejected reviews, Ian's denials in the approvals queue
(not `expired`/`interrupted`), and recurring lessons in the shared `lessons` namespace. Its own
worker and its own capability are never evidence, and neither is what no patch fixes: NOT
CONFIGURED, a placeholder, the shared model not answering, a GPU/circuit "not a fault", or a
VITALS "is stale" line that only echoes failed runs already counted. A dry run on the live data
(2026-10-12) found the Etsy title (10) and `('medical')` (8+8) clusters and the night builds'
"committed nothing new" (6).

## Dedupe and caps

A cluster is never proposed while one of it is open; not for 30 days after `denied`/`undone`;
not for 14 days after `shelved` or an expired card; `stale` may come back the next night; after
`applied` only with `threshold` NEW sightings since; and never one not seen for `fresh_hours`
(48) - it may already be fixed by hand. At most `per_night` (2) candidates start per
night. A new Daedalus job starts only inside its window (default 07:00-11:00, after the build
window 01:00-07:00 ends), never while the build window is open or `builds.daedalus` has a job in
flight, and `rest_minutes` after its own last job (the card is Moss's again in between).

## Visible, not inert

Its tally row says counts and states; `pulse()` raises a VITALS **NO OUTPUT** (amber) when it
detected clusters on `amber_nights` (3) nights in a row and proposed none, naming why.
`python -m pionir improve status` lists every candidate and state. Without the sandbox (the
Python part of `tools\setup-build-sandbox.ps1`; node is NOT needed) or with
`PIONIR_AUTH_COMPAT` on, it says NOT SET UP and runs nothing.

## Protected (no agent promotes changes to its own gate)

Listed in `selfimprove/guard.py` (`PROTECTED`), tested one by one, enforced twice: by the worker
before review and again by the `improve.apply` adapter before parking and before applying.
