# Pionir

Pionir is the integration host for Ian's family of specialist AI agents. It presents one
coherent assistant while preserving each specialist's identity, state, permissions, and
learning loop behind explicit adapters.

Pionir is an orchestrator, not a repository merger. Existing projects remain independently
runnable and are integrated through a small capability protocol.

## Design rules

1. Only one heavyweight GPU model is leased at a time by default.
2. Memory is namespaced; private agent state is never imported implicitly.
3. Tool access is declared per capability and checked before dispatch.
4. Improvements are evaluated and approved before promotion.
5. Every routed task and state-changing action is suitable for an append-only audit event.
6. Source agents remain untouched until their adapter is tested against the contract.

## Initial architecture

```text
User or client
    -> Pionir executive
        -> capability registry
        -> permission/evidence gate
        -> model lease scheduler
        -> namespaced memory
        -> specialist adapter
```

The implementation is deliberately dependency-free. It establishes the contracts that
Atani, Theo, and Bryo/Terrarium can implement
without requiring their codebases to be copied into Pionir.

## Development

```bash
python -m unittest discover -s tests -v
python -m pionir doctor
python -m pionir capabilities
```

The initial dependency-free Windows desktop shell is available after installation as
`pionir-desktop`; see [Windows setup](docs/SETUP_WINDOWS.md). It presents explicit specialist
routes until the evaluated automatic router is ready. It is a working desktop control shell,
not yet a bundled single-file Windows application.

See [docs/FUSION_PLAN.md](docs/FUSION_PLAN.md) for the staged integration plan and
[docs/ADAPTER_CHECKLIST.md](docs/ADAPTER_CHECKLIST.md) for the source audit template. Confirmed
source interfaces and deferred boundaries are recorded in
[docs/SOURCE_AUDIT.md](docs/SOURCE_AUDIT.md).

Self-improvement candidates are governed by the deterministic, human-approved contract in
[docs/IMPROVEMENT_GATE.md](docs/IMPROVEMENT_GATE.md).

Typed action plans cross the [Atani bounded-executive contract](docs/ATANI_EXECUTIVE.md)
without giving Pionir arbitrary process or filesystem authority.

Windows installation and environment setup are documented in
[docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md).

Additional local agents can implement the dependency-isolated
[JSON stdio adapter protocol](docs/ADAPTER_PROTOCOL.md).

Existing specialist Discord integrations are preserved under the
[notification ownership rules](docs/NOTIFICATIONS.md); Pionir does not copy webhook secrets or
duplicate source-owned lifecycle messages.

## Security

Never commit transcripts, embeddings, model weights, databases, credentials, local `.env`
files, or the contents of `.techsupport_agent`. Pionir stores only configuration and adapter
code in Git; runtime state belongs in ignored, access-controlled storage.

### Who may call Pionir's HTTP API

Loopback is not an identity: any local process can reach `127.0.0.1:8780`. So
(`src/pionir/auth.py`):

- A request's own `permissions` are never trusted. A privileged, public, client or money
  action from ANY client parks for the owner (Discord, or the phone/dashboard approve
  button); an approved item runs in-process with exactly the permission recorded on it.
- Every POST that runs, parks or decides something names its caller with
  `Authorization: Bearer <token>`. One token per client - `crew`, `galatea`, `atani`,
  `desktop`, `dashboard`, `phone` - in `~/.pionir/secrets/pionir-client-<client>.token`
  (or `PIONIR_CLIENT_TOKEN_DIR`), made by Pionir on first start. The owner's dashboard
  holds its token as an HttpOnly session cookie, set by the signed-in link the launcher
  opens (`/?key=...`).
- Approve / deny / the digest request are the owner's alone: only `dashboard` and
  `phone` (Galatea's phone glass relays with it), and never for an item that client
  parked. Code, not config, decides that.
- What each client may call is a grant table: the code default in `auth.DEFAULT_GRANTS`,
  narrowed per client by an optional `<state_root>/auth/grants.json`. No grant holds a
  privileged permission (`auth.GRANTABLE_PERMISSIONS` is empty - the hook the Daedalus
  sandbox work will use for its one narrow crew grant).
- Read-only GETs stay open on loopback (the owner's browser, Pionir Desktop), and every
  request - GET or POST - must name a local Host (DNS rebinding) and, from a browser,
  come from Pionir's own page.

**`PIONIR_AUTH_COMPAT`** (default `on` for this release): a POST with no token is served
as `anonymous` - non-privileged work only, a warning logged - so a partial rollout does
not break the stack. A privileged call without a token is refused (401), never parked. A
wrong token is always refused. Set `PIONIR_AUTH_COMPAT=off` (for Pionir and the crew)
once every client is confirmed sending its token; the crew API's writes follow the same
flag.
