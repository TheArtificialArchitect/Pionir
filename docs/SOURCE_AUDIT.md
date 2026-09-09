# Source interface audit

This file records source-grounded integration decisions. It intentionally contains no secrets,
transcripts, embeddings, model files, or private runtime state.

## Theo / Tech-Support

- Source: `PreShotCome/Tech-Support`, branch `machine-learning`.
- Python support: 3.10 or newer.
- Simple bridge: `python -m agent.server`, loopback port 9000, `GET /health`,
  `POST /chat`. This surface does not authenticate requests and is not Pionir's target.
- Production local bridge: `python -m agent.bridges.local_bridge`, loopback port 8765 by
  default, bearer-token authentication, capability-bearing `GET /health`, ordinary chat API,
  Cortex API, and `POST /peer/chat`.
- Token source: `BRIDGE_TOKEN`, or Theo's generated
  `~/.techsupport_agent/bridge_token.txt`; the token is never committed to Pionir.
- Atani already targets `http://127.0.0.1:8765` and resolves the shared token through
  `ATANI_THEO_TOKEN`, `BRIDGE_TOKEN`, or Theo desktop settings.

### Accepted first boundary

Pionir capability `conversation.theo_reply` calls the authenticated `/peer/chat` endpoint.
Theo's own implementation makes this path conversation-only: no recall, no tools
(`llm.chat(..., tools=[])`), no hands/code routing, no dream watermark, no growth scoring, no
transcript logging, and no access to Ian's private-memory briefing.

The endpoint accepts only the peer name `Atani`, enforced source-side in
`agent/bridges/local_storage.py`. Pionir sends exactly that in `adapters/theo_peer.py`.

**Re-verified 2026-08-30, and one part of the above had changed since it was written.** The peer
transcript is no longer seeded with `PERSONA_BASE` alone. It is now `PERSONA_BASE` plus
`compose_self_spine()`, which carries Theo's own selfhood while still excluding Ian's data, and
greps its own output before returning it. The source comment gives the reason: *"it withheld
Theo's own self along with Ian's data, and voice is not separable from continuity in him."*
Threads are also kept per `conversation_id`, up to 32, so there is continuity within a
conversation. Everything else in this section still holds exactly as recorded.

### Ian's decision, 2026-08-30: Theo is Pionir's voice

Recorded here because it changes what this boundary is being asked to do, not because the
boundary has moved. What has changed in Pionir: `reasoning.atani_chat` is renamed
`reasoning.atani_answer` and no longer claims the conversational vocabulary, plain conversation
routes to Theo, and the desktop defaults to him.

**What this boundary cannot yet deliver, and it is not a small gap.** `/peer/chat` opens every
turn by telling Theo that the speaker is *"another local synthetic agent — not Ian"*, and
instructs him not to expose Ian's private memory or update either human model. So a request
Ian makes through Pionir reaches a Theo who is told he is talking to Atani, holds no memory of
Ian, and answers addressed to Atani. That is Theo's voice and Theo's self, which is most of what
was wanted — but it is not Theo talking to Ian, and the difference is visible in the first reply.

### Explicitly deferred, and now the live question

Pionir does not call Theo's ordinary `/chat/send` endpoint. That path reaches Theo's own tools
and would bypass Pionir/Atani's action authorization, and the recorded condition for revisiting
it was that the source runtime add *a mode that separates voice/conversation from tool
execution*.

That condition is closer to met than it was: Theo now has a two-pass turn (`THEO_TWO_PASS`)
that separates deciding from speaking. It is not sufficient on its own — the split happens
inside Theo and does not hand authorization to Pionir, and the switch is off by default.

**Built and adopted 2026-08-30.** `/voice/chat` exists in the source and Pionir calls it.
`Agent.voice_chat()` runs `self.chat(text, no_tools=True)` — the *same* code path as an ordinary
turn rather than a parallel one, so it inherits every improvement the normal turn gets and there
is exactly one place the no-tools property can break. A source-side selftest asserts the model is
offered no tools, with a control asserting the ordinary path still is, so it cannot pass in a
world where nothing ever receives tools.

Contract as implemented, loopback-only and Bearer-authenticated:

    POST /voice/chat   {"content": str <= 32000, "conv": str optional}
    200                {"ok": true, "conv": str, "message": {id, role, content, createdAt, seq}}
    non-200            {"ok": false, "conv": str, "error": str}

Health also reports `"model"`, added 2026-09-04 at Pionir's request: the model the live client
actually resolved, read off the attached Agent rather than from `OLLAMA_MODEL`. Pionir asks for
it at boot instead of declaring a version, because Theo is promoted often and a declared id goes
stale silently - it stops matching anything resident, admission charges full weights instead of a
KV cache, and turns that would have fitted are refused with a correct-looking shortfall.
`PIONIR_THEO_MODEL_ID` pins a build when that is wanted; unset means ask.

**Do not read `OLLAMA_MODEL` for this.** The source's own history is that a persisted env var
outvoting the launcher is how retrains v10 through v16 landed in Ollama and were never served,
and on 2026-09-04 this session observed a process-scope copy reading `theo-local-v17-q4` while
User scope and the promotion both said `theo-local-v25-q4`.

Model names are normalised before comparison: the promotion writes `theo-local-v25-q4` and the
daemon reports `theo-local-v25-q4:latest`, so a verbatim comparison never matches and the
discount would never apply.

Health advertises `capabilities["voice_chat"]`. **Not `voice`** — that flag already meant Piper's
text-to-speech, so probing it would report health for a different subsystem entirely. Pionir
probes `voice_chat`, and a test asserts it fails on a bridge with `peer` and `voice` up but the
voice path detached.

An empty `conv` asks Theo to open a thread and return its id; a non-existent one is a 404, so
Pionir sends `""` rather than inventing an id, and returns the id Theo gives back so the thread
continues across turns.

Pionir's adapter, capability, and agent id dropped "peer" from their names, because it no longer
calls that endpoint: `conversation.theo_reply` on agent `theo`, in `adapters/theo.py`. The
desktop route reads "Theo" rather than "Theo · safe peer" — that label was accurate while the
peer path was the truth and would be a lie now.

**Verified live 2026-09-04, and it found something no test on either side could.** A turn
completed in 23 s: Theo answered, opened a thread and returned its id, and continuity across
turns worked. Full memory is there - he knows Ian, the repository, and the history.

**But he does not know the speaker is Ian.** Asked "Who am I?" on a cold thread he answered
"You're Theo", and asked "Who are you speaking with right now?" he described himself, referred to
Ian in the third person, and closed with *"You are the human, here as yourself... say yours when
you speak."* He is asking to be told who is speaking.

**Not endpoint-specific, confirmed on the source side the same day.** The Theo pane ran the same
prompts through `eval_generate` - the ordinary path's own context, the one the desktop serves -
and they invert identically. `/voice/chat` is innocent, as `voice_chat()` being
`chat(text, no_tools=True)` predicted.

**The cause recorded here first was wrong, and the correction is the interesting half.** This
session attributed it to source commit `b22eae6b`, which moved "who everyone is" from a standing
anchor to on-demand-when-named, and guessed the speaker's identity left with the roster. It never
left, because it was never there. Every prompt named Ian as somebody who *exists* - "Ian is the
human you partner with" - and none named him as the person typing. The claim that the speaker is
Ian lived only in the docstrings of `Agent.voice_chat` and `_serve_voice`, which is exactly the
shape of HEAD 3.6: a comment asserting a property that no code establishes. So `b22eae6b` is
clear and its measured win (recitation reflex 0/8 against 4/8) stands unreverted.

**Fixed source-side in `b47b15b1`, and it changes nothing measurable.** A "## Who you are talking
to" block in `_name_block()`, carrying the per-path default this session argued for: `/peer/chat`
composes its own transcript from `PERSONA_BASE` + the self spine and never touches `_name_block`,
so Atani stays correctly a peer without special-casing, and a source selftest now asserts the peer
path does not inherit the default. The clause "unless a system line in this conversation names a
different speaker" is what makes the peer path's negative assertion and this positive one one rule
rather than two.

Measured four arms at n=30, interleaved with the order rotated: 9/30 wrong before, 8/30 with the
block, 8/30 with the briefing register also reframed, 6/30 labelling the user turn `Ian: <text>`
against a 7/30 baseline. Then the *same* baseline code again: 7/30 against the first run's 9/30.
That last number is what makes the other four readable - an effect under about +/-3/30 is invisible
to this harness and every arm sits inside the band. The block ships because it says something true
for 70 tokens, and its docstring records in capitals that it does not fix the behaviour, so nobody
later reads it as a fix that regressed.

**Pionir asked for a `speaker` field on `/voice/chat` and it was declined, correctly.** It could
only ever hold "Ian" - any other value is a peer, and `/peer/chat` already carries that with a
boundary the voice path deliberately lacks. A field with one valid value is a field that
eventually carries a wrong one, and it would give this endpoint a second, weaker way to say
something that already has a right answer. Nothing to build here: the default is the mechanism,
and it lives where the ordinary turn is composed.

Superseded: the note below was written before that run.

**The success path had not been run live.** Theo's backend was not listening on 8765 and the card
had 1270 MB free at 100% utilisation, so a real turn could not be attempted, and starting the
backend by hand would hold the mutex the desktop app needs. The failure path *was* exercised end
to end: routing reached `conversation.theo_reply` at confidence 1.0 and admission refused the
turn before touching the bridge — 5190 MB required against 1270 MB observed free. Treat the
reply path as unbuilt until one real turn has gone through it.

### The seam Pionir cannot check for itself

Worth stating plainly, because it is the one place work on Theo can reach Pionir without
anything here noticing.

**Pionir's authorization boundary rests on a property enforced entirely inside Theo's process.**
If `/voice/chat` ever offers the model a non-empty tool list, Theo can act — Melete's hands,
Daedalus's worktrees — outside Pionir's permission gates and outside its audit ledger, and
Pionir will record the turn as an ordinary conversation and report success. There is no
observation available on this side that would distinguish the two: the reply looks the same.

It is guarded, and guarded well: the source applies `no_tools` *after* the tool selector's
exception fallback, which is the route that would otherwise quietly re-arm the full set, and the
selftest asserts the property with `THEO_TWO_PASS` on and carries a control asserting the
ordinary path still receives tools. But that is Theo's test protecting Pionir's boundary, and
from inside Theo an empty tool list looks like an omission rather than the whole reason the
endpoint exists.

Two smaller couplings, for completeness. The `/voice/chat` request and response shape and the
`voice_chat` health flag: a change there breaks this adapter loudly, with a typed error, which is
the right failure. And the declared model id, which drifts on every retrain — see
`PIONIR_THEO_MODEL_ID`. Everything else in Pionir is indifferent to Theo's internals; with the
adapter unconfigured the runtime boots, the audit verifies, routing aim scores full marks on the
remaining probes, and a conversational request asks which route rather than substituting Atani.

### The decision behind it

**Ian chose neither existing path, 2026-08-30.** `/chat/send` was offered and declined:
it would give full Theo and also let his tools reach past Pionir's permission gates and audit
ledger, which is the thing this deferral was protecting. The agreed target is a third endpoint,
requested from the Theo pane:

    POST /voice/chat        Bearer, loopback
    request   {"content": "...", "conv": "..."}
    response  {"ok": true, "conv": "...", "message": {...}}   as /chat/send

`PERSONA_BASE` + `compose_self_spine()` + continuity briefing + recall + human model, the
speaker is Ian rather than a peer, and — the single reason the endpoint exists rather than
Pionir calling `/chat/send` — `llm.chat(transcript, tools=[])`. Full memory, zero tools, action
authorization stays with Pionir. Health should advertise it so Pionir probes the capability it
uses rather than a neighbouring one.

**Until it exists, Pionir stays on `/peer/chat` and no client is written for `/voice/chat`.** An
adapter pointed at an endpoint that returns 404 is a tested component that does nothing, which
is this estate's most expensive failure; the Pionir side gets built and verified end to end
against a live endpoint, in one piece.

One defect found in `/chat/send` while specifying this, reported and not inherited:
`_serve_chat_send` catches a handler exception, sets the reply to `"(internal error: ...)"`,
persists it as an assistant message, and returns HTTP 200 with `ok: true`. A caller cannot tell
that from an answer, so Pionir would render an error string as something Theo said and record
the task as completed.

## Atani

- Source: `PreShotCome/Atani`, branch `main`, package version 1.2.0.
- Python support: 3.12 or newer.
- Atani is currently a library/CLI application, not a general executive HTTP service.
- Its `BoundedExecutive` already enforces step/replan bounds, capability authorization, exact
  action approvals, observation hashing, postcondition verification, persistent action state,
  and pause controls.
- Its existing `TheoPeerClient` already implements the same authenticated `/health` and
  `/peer/chat` contract selected above.

### Accepted first boundary

Pionir invokes Atani's existing JSON CLI through its configured Python 3.12 environment. Normal
and depth reasoning are separate capabilities with separate model-resource declarations, and
both require Pionir's `atani.chat` permission. The subprocess boundary lets Pionir itself remain
compatible with Python 3.11 and avoids importing or duplicating Atani's persistent state.

Atani's general `BoundedExecutive` is not yet exposed through a stable CLI command. A future
goal/action adapter must map Pionir tasks to that exact interface rather than reimplement its
approval and verification logic inside Pionir.

## Bryo / Terrarium

- Source: `PreShotCome/terrarium`, branch `build`, package version 0.1.0.
- Python support: 3.12 or newer.
- Bryo's `Governor` owns its process RSS ladder, pagefile tripwire, host-load torpor,
  pre-flight allocation sizing, control files, and revocable opportunistic GPU leases.
- A missing or unreadable GPU is intentionally treated as busy, so Bryo never scavenges blind.
- `python -m bryo.status` is intentionally read-only: it opens Bryo's database read-only and
  never constructs the mind.

### Accepted first boundary

Pionir exposes `organism.bryo_status` by calling Bryo's existing read-only status module in
Bryo's own Python environment. It does not import Bryo, mutate its genome, create control files,
or take over its watchdog.

### Resource-governor relationship

Bryo remains the authority over its own CPU, memory, and scavenger GPU work. Pionir remains the
authority over heavyweight specialist model scheduling. Pionir now holds an OS-owned,
non-blocking lock at `~/.pionir/resource/gpu.lock` for every heavyweight GPU call. Bryo must
hold that same lock for the complete lifetime of each opportunistic GPU lease. Process exit
releases the OS lock automatically; metadata in the file is diagnostic only.

## Retired from Pionir — 2026-09-09

The three sections below (Autogenesis, Probability, Genesis agent) record audits
of integrations that **no longer exist in Pionir.** Their status adapters,
config, CLI commands, and desktop routes were removed on 2026-09-09 once the new
voice session had harvested what it needed from them. The audits are kept as the
record of what was integrated and why it was removed; the order and reasoning are
in `ARCHITECTURE_DECISIONS_2026-09.md`. Bryo (governor) and Atani (manager) stay.

## Autogenesis

- Source: `PreShotCome/autogenesis`, branch `main`, package version 0.1.0.
- Python support: 3.11 or newer, with no runtime dependencies.
- Autogenesis is a clean-room evolutionary organism with a deterministic curriculum,
  transactional SQLite ledger, hash-chained events, loopback observatory, and its own
  deny-by-default extension gate.
- Its persistent online process uses `state-live`, checkpoints every generation, and owns its
  own pause/control surface.

Autogenesis should enter Pionir as an evaluation and candidate-generation specialist through
the JSON stdio protocol. Pionir must not import its live organism state or replace its
supervisor. A read-only status adapter should precede any mutation/evolution command.

## Probability

- Source: supplied local `C:\src\Probability` archive, package version 0.1.0.
- Python support: 3.11 or newer; the protected kernel has no required third-party runtime
  dependencies.
- Runtime: a lightweight local supervisor with deterministic cognition, SQLite life record,
  capability-limited genes, optional model-assisted experiments, fixed evaluation, and
  reversible Git promotion.
- Local dashboard/API: loopback port 8791; `GET /health`, `GET /api/state`, and a mutating
  `POST /api/control` surface.
- Discord: source-owned Gateway bot and/or outbound webhook with owner-only control commands.
- Private material in the supplied archive included a local environment file, access token,
  state database, experiment data, and Git history. None is imported or committed to Pionir.

### Accepted first boundary

Pionir capability `organism.probability_status` reads `/api/state` only over loopback. The
adapter returns operational mode, counters, resource pressure, model availability, self-model
counts, and Discord configuration booleans. It deliberately removes memories, events,
curiosities, goals, experiments, usage records, channel ID, and all credentials.

Pionir does not call `/api/control`; Probability continues to own pause, resume, dream,
evolution, emergency stop, evaluation, promotion, watchdog behavior, and Discord delivery.

## Genesis agent

- Source: supplied local `C:\Users\Ian\genesis-agent` archive, package version 0.2.0.
- Python support: 3.12 with FastAPI, Uvicorn, WebSockets, Pydantic, HTTPX/SOCKS, and PyYAML.
- Runtime: Ollama-backed life loop, deterministic emotional meters, Markdown/Obsidian vault,
  proposal-only metacoder, and Tor-fail-closed web research.
- Local dashboard/API: loopback port 8000; health, state, journal, chat, clear-chat, and
  WebSocket surfaces.
- Genesis is browser-hosted, not a native desktop bundle. Its PowerShell launcher owns the
  Python process, UI preflight, Ollama preflight, and optional private Tor process.
- The supplied archive included installed dependencies, a Tor cache, and live vault content.
  None is imported or committed to Pionir.

### Accepted first boundary

Pionir capability `organism.genesis_status` reads `/api/health` and `/api/state` over loopback.
It retains subsystem booleans, model name, life-loop counters, last action name, and emotion
meters. It removes filesystem paths, probe details, journal content, inner monologue, and action
results.

Genesis currently has no API authentication. Pionir therefore does not expose chat, journal,
clear-chat, WebSocket chat, or any state-changing Genesis operation. Those can be considered
only after Genesis gains an authenticated capability boundary.
