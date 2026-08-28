# Phase 0 — observed resource measurements

Taken 2026-08-27 on the target workstation. Reproduce with `python -m pionir benchmark`.

The fusion plan's resource policy is explicit that its admission rules are "conservative
admission rules, not benchmark claims", and that Phase 0 measurements replace the estimates.
This is that replacement. Every figure below is observed, not derived.

## Machine

| | |
|---|---|
| GPU | NVIDIA GeForce RTX 3060, 12288 MB, driver 610.47, WDDM |
| Ollama | 0.32.15 |
| Measured | 2026-08-27 21:10–21:35 local |

## The card was not idle, and that changes the budget

Two things held VRAM throughout, and neither belongs to Pionir.

**1. The desktop floor is 1830 MB, not the 1024 MB the config reserves.** Measured five times
across independent runs with every model evicted: 1780, 1827, 1828, 1830, 1836 MB. It includes
Windows shell, Edge, Agora, the Claude desktop app, Steam, and an idle League of Legends client.
Ian is at the keyboard; this floor is the normal working state, not a contaminated one.

**2. `genesis-agent` holds a 4423 MB model resident, continuously.** `qwen2.5-coder:7b`, kept
hot by the uvicorn process `src.brain.api:app` on 127.0.0.1:8000 (`C:\Users\Ian\genesis-agent`,
running since 2026-08-26 19:19).

This was previously read as a leaked model left behind by a closed stack. It is not a leak. It
was evicted cleanly, the card stayed clear for ten seconds, and **it was reloaded 4 seconds
after the card next became free** — measured, repeatedly. A live client is re-requesting it.

That distinction matters for admission control. A leak can be swept once. A polling client
cannot, and it does not participate in the shared GPU lock, so `max_gpu_leases = 1` and
`SharedGpuLock` between Pionir and Bryo say nothing about whether the card is actually free.

### Three budgets, and only one of them is real

| Budget | Usable VRAM | Basis |
|---|---|---|
| Configured today | 11264 MB | `total 12288 − reserved 1024`, an estimate |
| Card otherwise idle | 10458 MB | `12288 − 1830` observed floor |
| **Real steady state** | **6035 MB** | `12288 − 1830 − 4423` with Genesis resident |

The configured reserve is 806 MB smaller than the observed floor, so the scheduler will admit
a model that cannot fit even on an otherwise-idle card. This is an estimate that was true when
written and was never re-measured.

## Per-model measurements

At `num_ctx = 4096`, from a verified-cold start (daemon inventory confirmed empty first).

Live routing candidates:

| Model | Role | Resident VRAM | Cold load | Warm latency | Throughput | On GPU |
|---|---|---|---|---|---|---|
| `theo-local-v17-q4` | Theo, conversation | 4423 MB | 4.1 s | 0.17 s | 47.6 tok/s | yes |
| `qwen2.5-coder:7b` | code | 4423 MB | 3.1 s | 0.09 s | 49.0 tok/s | yes |
| `qwen2.5:7b-instruct` | general | 4423 MB | 4.6 s | 0.08 s | 49.4 tok/s | yes |
| `moondream` | vision | 1017 MB | 3.1 s | 0.05 s | 185.5 tok/s | yes |
| `nemotron-3.5-lightning:30b-a3b` | depth (25 GB) | **0 MB** | 33.3 s | — | **30.4 tok/s** | no — CPU |

Retired Theo builds, measured before that was known. They are **not** routing tiers and no
admission rule should be derived from them; they are kept here only as size-class evidence,
because the repository holds no other dense model at either size:

| Model | Resident VRAM | Cold load | Throughput | On GPU |
|---|---|---|---|---|
| `theo-local-v16` (retired) | 7421 MB | 5.6 s | 30.5 tok/s | yes |
| `theo-local-v7` (retired, 15 GB) | **0 MB** | 18.4 s | **6.4 tok/s** | no — CPU |

Theo is on v17. `src/pionir/adapters/theo_peer.py` already declares
`theo-local-v17-q4:latest`, so no code assumed otherwise.

Warm latency is a 16-token round trip against an already-resident model. Throughput is from the
daemon's own `eval_count / eval_duration` over a 76–160 token generation; an earlier pass
produced 5–7 token samples, which are too few for the rate to mean anything and were discarded.

### Context cost is real and must be declared

Measured by re-loading each model at 4096 and at 16384 and differencing resident VRAM:

| Model | 4096 | 16384 | Per 1K context |
|---|---|---|---|
| `theo-local-v17-q4` | 4423 MB | 4792 MB | 30.75 MB |
| `qwen2.5:7b-instruct` | 4423 MB | 4792 MB | 30.75 MB |
| `qwen2.5-coder:7b` | 4423 MB | 4940 MB | 43.08 MB |
| `moondream` | 1017 MB | 1017 MB | n/a — context capped below the request |

A 7B at 32K context therefore costs roughly 5.3–5.8 GB resident, not 4.4 GB. `ModelRequirement`
already separates `estimated_vram_mb` from `context_vram_mb`; these are the numbers for it.

### Nothing above ~12 GB gets the GPU at all

`theo-local-v7` (15 GB) and `nemotron` (25 GB) both answered correctly and both held **zero**
VRAM — Ollama ran them on the CPU and did not keep them resident afterwards, despite a five
minute keep-alive. There is no partial-offload middle ground on this card at these sizes.

The consequence for the plan's line "large depth models are cold-loaded only for explicit depth
tasks": on this machine a large depth model is not a GPU tenant, so it does not need a GPU lease
at all. It needs a CPU admission decision and a much longer timeout.

The two behave completely differently, and the difference is architectural rather than a matter
of size — which is why "large model" is not a useful admission category on its own:

* `theo-local-v7` (retired) is dense. At 6.4 tok/s a 300-token answer takes ~47 s plus 18 s to
  load. Any dense model of that size would be unusable interactively here.
* `nemotron-3.5-lightning:30b-a3b` is a mixture-of-experts with ~3B active parameters. At
  30.4 tok/s on CPU it matches a dense 8B running *on the GPU*, for zero VRAM. Its cost is the
  33 s cold load, not its generation speed.

`nemotron-3.5-lightning:30b-a3b` is therefore the depth tier, and the only large model worth
routing to: it is both affordable and non-competing for the card. Admission should key on
active parameters and measured throughput, not on file size.

## Two 7B models fit simultaneously

Verified with both fully resident at `num_ctx = 4096`:

```
theo-local-v17-q4  4423 MB   fully_on_gpu: true
qwen2.5-coder:7b   4423 MB   fully_on_gpu: true
floor              1836 MB
total used        10682 MB / 12288 MB
```

`max_gpu_leases = 1` is therefore over-conservative for two quantised 7B models — but only by
about 1.6 GB, and that margin disappears as soon as either model is given a wider context
(two 7Bs at 16K would need ~11.5 GB with the floor, which does not fit). The default should
stay at one lease. What the measurement rules out is the assumption that co-residency is
impossible; what it rules in is that any relaxation must be computed from declared context, not
assumed.

With v16 retired, **every live GPU candidate is 4423 MB or smaller** and the depth tier takes no
VRAM at all. So there is no longer a heavyweight GPU tier to arbitrate between: the realistic
contention is several same-sized 7Bs, plus whatever Genesis is holding. That is a scheduling
problem about *how many* small leases fit, not about which single large model wins the card.

## What this changes

1. `reserved_vram_mb` must rise from 1024 to at least the observed 1830 MB floor. A larger
   value is defensible given that the floor moves with what Ian has open.
2. Admission must account for VRAM held by processes that do not hold the lock. Genesis is one
   today; the shared lock cannot see it, and a free-looking `nvidia-smi` reading four seconds
   after an eviction is not evidence the card is available.
3. `context_vram_mb` should be computed from the declared context at ~31–43 MB per 1K rather
   than left at zero. `TheoPeerSettings` currently declares 4700 MB model + 1500 MB context
   against a measured 4423 MB + 126 MB at 4K context. Both are conservative, so they fail
   safe and are not bugs; the 1500 MB figure is only correct if Theo runs about 48K context.
4. The depth tier is a CPU tier on this hardware, and `nemotron-3.5-lightning:30b-a3b` is the
   only large model worth routing to.
5. There is no heavyweight GPU tier left to arbitrate. Every live GPU candidate is the same
   4423 MB size class, so the lease question is how many fit, not which one wins.

## Honesty notes

* No source project was modified. `genesis-agent` was left running.
* `qwen2.5-coder:7b` was evicted from VRAM repeatedly during measurement. Genesis reloads it
  within seconds unaided, and it was resident again at the end of the run.
* League of Legends was open throughout and was deliberately not touched.
* `theo-local-v16` and `theo-local-v7` were measured before it was established that both are
  retired Theo builds. They were included here as a "medium" and a "large" tier; neither is a
  routing candidate, and the tables above have been corrected. No conclusion in this document
  now rests on either.
* `theo-local-v16` reported 7421 MB resident in one pass and 0 MB in another, while returning
  30.5 tok/s both times. A dense 8B on CPU would return roughly 6 tok/s, so it was on the GPU
  in both; the 0 MB is a residency-read race against eviction, not a CPU fallback. The 7421 MB
  figure is the one to trust.
* The module cross-checks Ollama's `size_vram` against an `nvidia-smi` delta. On WDDM those
  deltas proved unreliable whenever a third party loaded a model mid-measurement, which is why
  contended measurements are now recorded as such rather than averaged in. Per-model
  `size_vram` was stable and reproducible; the driver delta was not.
