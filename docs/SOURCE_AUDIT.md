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

Pionir capability `conversation.theo_peer_reply` calls the authenticated `/peer/chat` endpoint.
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
inside Theo and does not hand authorization to Pionir, and the switch is off by default. Closing
this needs Ian, and it needs the Theo pane, because either the peer path learns that Ian can be
the speaker or Pionir gets a third endpoint that is voice-with-memory but still tool-free.
Neither is Pionir's to build alone.

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
