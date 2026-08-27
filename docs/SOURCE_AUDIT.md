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

### Next boundary

Do not duplicate Atani's executive in Pionir. Add a thin in-process adapter after defining a
versioned mapping between Pionir tasks and Atani goals/actions. Because Atani requires Python
3.12, the integrated runtime baseline must be Python 3.12 even though Theo supports 3.10.
