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
Theo's own implementation makes this path conversation-only: no recall, tools, hands/code
routing, growth scoring, transcript logging, or access to Ian's private-memory briefing.

The endpoint currently accepts only the peer name `Atani`. Pionir therefore exposes it as an
Atani-to-Theo specialist capability, not as a general user chat endpoint.

### Explicitly deferred

Pionir does not call Theo's ordinary `/chat/send` endpoint yet. That path can reach Theo's own
tools and would bypass Pionir/Atani's action authorization unless the source runtime adds a
mode that separates voice/conversation from tool execution.

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
authority over heavyweight specialist model scheduling. A later cross-process lease protocol
must be implemented by both sides before Pionir may assume it can revoke Bryo's GPU work; a
Pionir-only lock would provide false safety because Bryo would not observe it.
