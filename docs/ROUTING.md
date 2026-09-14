# Intent routing

## Jobs: work that outlives the request

Every `POST /api/intent`, `POST /api/task` and approved action runs as a **job**
(`server.py`, class `Jobs`). The job is written to
`<state_root>/tasks/<task_id>.json` *before* the request is answered and again
from the worker thread when the work ends - so a result is never lost to a
client that gave up first (the voice at 180 s, the phone proxy at 6 s, Atani's
240 s kill of a 600 s Daedalus run that went on to modify code). The directory
keeps the newest 500 files.

| route | request | response |
|---|---|---|
| `POST /api/intent` | `{"request": str, "wait"?: float}` | finished within `wait`: **200**, the usual `{decision, status, ...}` plus `task_id`. Not yet: **202** `{"status":"running","task_id","decision"}` |
| `POST /api/task` | `{"capability", "payload"?, "permissions"?, "deferrable"?, "wait"?}` | finished: **200** `{ok, agent_id, result, evidence, task_id}` or `{ok:false, error, task_id}`; parked: **200** `{ok:false, status:"pending_approval", approval_id, task_id}`; not yet: **202** `{"status":"running","task_id"}` |
| `POST /api/approvals/approve` | `{"id": approval_id}` | **202** `{"ok":true,"status":"running","approval_id","task_id"}` at once. Claim is atomic (pending -> running): a second tap gets `{"ok":false,"error":{"type":"AlreadyResolved"}}`. When the job ends the approval record becomes `approved` or `approved_failed`, with `result` |
| `GET /api/task/<task_id>` | optional `?wait=<s>` long-poll | **200** the job file: `{task_id, kind, status, created_at, finished_at, request, result, error}`; **404** if unknown |
| `GET /api/tasks?n=20` | | `{"tasks":[...]}` newest first, without `result` bodies |

`wait` is seconds (default 150, max 600, 0 = answer immediately). `kind` is
`intent` / `task` / `approval`; `status` is `running` / `done` / `error` /
`pending_approval` / `unclear` / `self`. `result` is exactly the response dict
the POST returned or would have returned. For the voice: POST with a `wait`
under her own timeout, and on a 202 poll `GET /api/task/<task_id>?wait=60` until
`status` leaves `running`.

### The GPU hand-off (Daedalus has the card)

Daedalus runs `qwen3-coder:30b`. It is not a CPU tenant: measured 2026-09-13,
Ollama loads it at ~18-19 GB with ~10 GB on the card, and the load evicts
`gemma3:12b` (the voice). Its capability declares `requires_gpu=True`,
`estimated_vram_mb=10_000`, `context_vram_mb=0` - the on-card share, which fits
the budget (12 288 - 1 830 = 10 458 MB); declaring the whole 18-19 GB would make
a model that does run here unadmittable, and raising its context measured RAM,
not VRAM. So a coding job takes the shared GPU lease, whose holder record reads
`owner=pionir purpose="daedalus: qwen3-coder:30b"`.

`protected_models` (default `gemma3:12b`) are not evicted for a caller without
the lease - that refusal still stands. Under the lease they are: the voice
honours the lock (Galatea `gpu.py`) and has stood down, so evicting her model is
the planned swap. When the lease is released the scheduler unloads the job's
model (keep_alive 0, while still holding the lock, unless it was already
resident before) and then re-warms the protected models that were on the card
(`benchmark.warm`, on a background thread). Handback failures are logged, never
raised. A voice running code older than this does not honour the lock, so
relaunch her before Pionir.

Every POST must carry `Content-Type: application/json` and a local `Host`; a
browser `Origin`, if sent, must be `http://<Host>`. Anything else is **403**
`{"error":"forbidden"}` - a cross-site page cannot drive the admin surface.

`pionir route "<request>"` turns plain language into one declared capability and
runs it. `src/pionir/router.py`.

The registry already routed a *named* capability. This is the step before that:
deciding which capability a request meant.

## The rule it is built around

> A low-confidence classification falls back to asking which route, never to a
> guess, and every routing decision emits a `task.routed` audit event carrying
> its confidence — so that after a week there is evidence of how often it was
> right, rather than an impression.

Both halves are load-bearing, and both are covered by tests that fail if the
behaviour is removed.

## How it decides

Deterministic and lexical. **No model is consulted to decide which model to
load** — that circularity would spend the GPU the router exists to arbitrate,
and would make every decision unreproducible.

That was a design argument when it was written. It now has a measurement behind
it, from the Theo pane on 2026-08-30/31: ten distinct probes, each unambiguously
warranting a tool, replicated.

| the model was asked to choose a tool | fired |
|---|---|
| inside its full conversational prompt, at the temperature its voice needs | 1/10 |
| in a dedicated pass: minimal voiceless prompt, narrow schemas, temperature 0 | **10/10** |

Two things follow, and only these two. **The capability is intact** - the same
model, the same tools, the same questions. What decides is the prompt around the
decision. And **the failure is silent**: a zero raises nothing, logs nothing, and
is indistinguishable from a turn that needed no tool.

So a router that asked a small local model which specialist to use would work in
a harness and degrade in production, without anything anywhere saying so. That
is the property worth avoiding - not the accuracy, which was perfect under
controlled conditions. Pionir's routing decision does not depend on anyone
maintaining those conditions.

The three properties of that decision pass travel together and are confounded
within this comparison. Which of them is load-bearing is not known.

*Earlier revisions of this section cited a per-condition table from the same
investigation about prose suppressing structured output. Those cells were n=1 -
at temperature 0 a fixed prompt is deterministic, so sampling one prompt eight
times measures the loop, not the model - and the finding did not replicate when
re-run with varied inputs (1/5 against 1/5, p = 1.0). It is withdrawn. The
correction is kept here because it is more transferable than the claim was: when
citing someone's live investigation, cite only what is well powered, and expect
to be wrong otherwise.*

A capability's vocabulary is whatever it says about itself: its name, its
description, its agent id, and its declared `routing_hints`. Each term is
weighted by how *few* capabilities use it, computed from the registry at
classification time. So `bryo` outweighs `status`, and registering a new
specialist re-weights routing automatically. There is no table of categories
inside the router to fall out of date.

`confidence` is the winner's share of the top two scores. A dead tie is 0.50; a
request nothing else matched is 1.00. **It measures separation, not correctness**
— it says the winner was distinctive, not that it was right. Whether it *was*
right is what the ledger is for.

## When it asks instead

| reason | what happened |
|---|---|
| `matched` | routed |
| `ambiguous` | the top two scored too close (below the floor, default 0.60) |
| `not_distinctive` | the request matched only vocabulary a whole family shares |
| `no_match` | nothing registered matched at all |
| `empty_request` | no words to route on |
| `no_capabilities` | nothing is registered |

An ask exits **3** and prints the candidate routes, so the question is
answerable rather than merely a refusal:

```
$ pionir route "organism status"
{
  "status": "question",
  "question": "That names a family of capabilities rather than one of them. Which did you mean?
    1. organism.autogenesis_status - Read Autogenesis controls and recent ledger events
    2. organism.bryo_status - Read Bryo's current vitals and lineage snapshot
    3. organism.genesis_status - Read Genesis health and life-loop counters
    4. organism.probability_status - Read Probability's operational state without private memory",
  "confidence": 0.5,
  "reason": "not_distinctive"
}
```

That is the correct answer to that request. Four specialists report status, and
the request names the family rather than a member of it; picking one would be a
coin toss presented as a decision.

Plain conversation is an **ASK** again as of 2026-09-10: Theo was removed from
Pionir (he was only ever a placeholder voice; Galatea is the intended voice and
is not wired yet), so there is no conversational capability at all, and `"chat
with someone"` has no right answer. The router asks rather than misrouting it to
Atani's reasoning - which it can only do because Atani gave up the conversational
words: `reasoning.atani_chat` was renamed `reasoning.atani_answer` so the router,
which scores a capability's own name, does not let "chat" pull conversation to
Atani. When the voice is wired, plain conversation gets a right answer again and
the probe returns to expecting it.

## What it does not do

**It does not weaken the gates underneath it.** `CapabilityNotFound` and
`PermissionDenied` still come from the registry untouched. In particular, when a
classified route is denied for permission, the router does **not** substitute a
capability the caller can reach — Theo's conversation capability requires no
permission and Atani's requires `atani.chat`, so the helpful-looking move is to
answer via Theo instead. That turns a refusal into a silent misroute, and
`test_a_denied_route_is_not_substituted_for_a_reachable_one` exists to stop it.

The CLI grants no permissions by default; pass `--permission atani.chat`.

## The evidence trail

Every classification writes one `task.routed` event, including the ones that
never reached a specialist. Without that the ledger would show only the routes
that were confident *and* allowed, and the question the system most needs
answering — how often it could not tell — would have no evidence behind it.

```
1 unrouted  outcome=ask   confidence=0.50 reason=not_distinctive options=organism.bryo_status,organism.genesis_status
2 unrouted  outcome=ask   confidence=0.00 reason=no_match        options=conversation.theo_reply,executive.atani_run,...
3 unrouted  outcome=route capability=reasoning.atani_answer confidence=0.77 reason=matched runner_up=conversation.theo_reply refused=PermissionDenied
4 theo      outcome=route capability=conversation.theo_reply confidence=1.00 reason=matched runner_up=none
```

The ledger measures **reach** and cannot measure **aim**. It records which
capability was chosen and how confidently, so a ledger full of confident routes
reads identically whether those routes were right or wrong. Accuracy is a
separate instrument:

```
$ pionir route-check
{"status": "ok", "accuracy": 1.0, "correct": 12, "probes": 12,
 "under_ask": [], "misroutes": []}
```

Known-answer probes, classified and thrown away — nothing is executed, no model
loads, no GPU lease is taken, so it is safe to run while the card is busy. The
three failure kinds are counted apart because they are not equally bad: a
`misroute` sends the wrong specialist to answer confidently, an `over_ask` is
the router doing its job at some cost, and an **`under_ask` fails the check on
its own whatever the score**, because guessing where it should have asked is the
one thing it exists not to do.

`pionir doctor` reports the last result, its age, and warns past 30 days.
Aim degrades quietly; a measurement taken once and never repeated is an
impression with a number attached.

**Read the score knowing who wrote the probes.** They were written by whoever
wrote the classifier, at the same time, which makes a perfect score the weakest
kind of evidence there is - the implementation tested against its author's own
idea of what people ask. It catches regressions well and calibration badly.

**The harness writes nothing to the ledger**, and a test enforces it. That has
to hold for the calibration below to mean anything: an instrument that bumps the
counters it is calibrated against measures itself, and reports the agreement as
a pass. A green light manufactured out of a loop is worse than no calibration,
because it looks like evidence.

The ledger is the external vector that fixes this, and the two are complementary
rather than redundant. The probes give accuracy against known answers on
invented traffic; the ledger gives the shape of *real* traffic - how often it
actually asked, and at what confidence. If the ask-rate over a week of real
requests diverges sharply from the ask-rate across the probe set, it is the
probe set that is wrong. Requests that had to be asked about are the best source
of new probes, because they are the only ones nobody made up.

The detail is parseable `key=value` and **never contains the request text**. The
audit ledger excludes payloads by design and routing is not the place to start
putting user content into it; `test_the_audit_detail_never_carries_the_request_text`
holds that line.

To read a week of it:

```powershell
Get-Content $env:USERPROFILE\.pionir\audit\events.jsonl |
  ConvertFrom-Json |
  Where-Object { $_.event_type -eq 'task.routed' } |
  Select-Object occurred_at, agent_id, detail
```

## Making a new specialist routable

Declare the words a person would use for it. Nothing in the router changes.

```python
Capability(
    name="media.kairos_clip",
    description="Cut a highlight clip",
    routing_hints=frozenset({"kairos", "clip", "highlight"}),
)
```

Operator-authored specialists declare the same thing in their TOML:

```toml
routing_hints = ["kairos", "clip", "highlight"]
```

## Tuning

`--confidence-floor` raises or lowers the bar for one request. Raising it makes
the router ask more often; it cannot make it guess. Since a request only one
capability matches scores 1.00 by construction, no floor will turn an unambiguous
route into a question.
