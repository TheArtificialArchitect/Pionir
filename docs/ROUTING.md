# Intent routing

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
$ pionir route "chat with someone"
{
  "status": "question",
  "question": "That names a family of capabilities rather than one of them. Which did you mean?
    1. conversation.theo_peer_reply - A bounded, conversation-only Theo reply to Atani
    2. reasoning.atani_chat - Atani's bounded default conversational reasoning",
  "confidence": 0.5,
  "reason": "not_distinctive"
}
```

That is the correct answer to that request. Theo and Atani both hold a
conversation; choosing one would be a coin toss presented as a decision.

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
1 unrouted  outcome=ask   confidence=0.50 reason=not_distinctive options=conversation.theo_peer_reply,reasoning.atani_chat
2 unrouted  outcome=ask   confidence=0.00 reason=no_match        options=conversation.theo_peer_reply,executive.atani_run,...
3 unrouted  outcome=route capability=reasoning.atani_chat confidence=0.77 reason=matched runner_up=conversation.theo_peer_reply refused=PermissionDenied
4 theo-peer outcome=route capability=conversation.theo_peer_reply confidence=1.00 reason=matched runner_up=none
```

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
