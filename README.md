# Pionir

Pionir is the spine of Ian's personal AI system. It routes work to a family of specialist agents,
lets one heavyweight model use the GPU at a time, records every step in a tamper-evident
ledger, and holds every privileged or public action until the owner says yes.

Around that spine now sits **the crew**: a small business crew that runs the Dokaz side
business. Workers each do one job, division leaders distil what the workers recorded, and
**Moss** (the voice, running in Galatea) reads the leaders' reports and directs the crew. Every
client email, delivery, blog post, Instagram card, dev.to cross-post and Gumroad listing is
parked for Ian's approval on Discord before it goes out.

Pionir is an orchestrator, not a repository merger. Each specialist stays in its own repo, runs
on its own, and is reached through a small adapter.

## Who does what

| Name | Role | How Pionir reaches it |
|---|---|---|
| **Pionir** | Spine: route, schedule, admit GPU work, audit, gate approvals | this repo |
| **Moss** (Galatea) | Voice and brain. Holds conversation, sets the crew's goals, sends Ian a daily brief | `galatea` adapter, loopback HTTP `:8799` |
| **Atani** | Manager. Decides who does what, delegates and verifies. Runs on CPU (qwen3:4b) | `atani` CLI adapter |
| **Daedalus** | The only bot that touches code (qwen3-coder:30b, takes the whole card) | loopback HTTP `:8771` |
| **Melete** | Tool executor: files, git, shell, web | loopback HTTP `:8770` |
| **Bryo** (Terrarium) | Resource governor. Senses memory and GPU pressure; Pionir makes the decisions | `python -m bryo.status`, plus a pulse feed back to Bryo |
| **Nyx** / **Voodoo** | Offensive / defensive security companions | redacted status plus approval-gated runs |
| **The crew** | Business workers and division leaders | `python -m pionir.crew`, loopback `:8782` |
| **Scrooge** | The Dokaz API at `api.dokaz.net`: orders, blog, media, deliveries, traffic | HTTPS with token files |

## Design rules

1. **One heavyweight GPU model at a time.** A cross-process GPU lock plus a VRAM budget.
   Pionir refuses anything that doesn't fit, because an overcommitted Ollama quietly falls back
   to the CPU. The voice's model is protected: only a job that needs the whole card, holding the
   lease, may evict it, and she is re-warmed afterwards.
2. **Nothing public and no money moves without Ian's yes.** A capability marked
   `requires_approval` parks on every call. `spends_money` implies it. The crew has no money
   lever at all.
3. **Everything recorded is real.** A worker that cannot do its job says `not_configured` or
   `not_wired`, never a fake zero. A missing metric is "unavailable", and pending is never
   reported as done.
4. **Tool access is declared per capability** and checked before dispatch. The router only
   classifies unprivileged capabilities. A request it cannot place confidently becomes a question.
5. **Every routed task and state change is audited** in a SHA-256 hash-chained JSONL ledger.
6. **Memory is namespaced.** Lessons learned from failures are shared, and every bot recalls
   them before acting.
7. **Improvements are evaluated and approved before promotion.** Ian approves the exact
   digest of each candidate.
8. **Loopback only.** Every internal HTTP surface binds `127.0.0.1` and checks Host and Origin.

## Architecture

```text
 Ian (phone / Discord / dashboard)          Moss (Galatea, the voice)
        |   approve / deny                      |  /api/intent      crew.* capabilities
        v                                       v                        |
 +--------------------------- Pionir server :8780 -----------------------+-------+
 |  intent router -> capability registry -> permission / approval gate          |
 |       -> circuit breaker -> Bryo pacing -> GPU lease scheduler -> adapter    |
 |  audit ledger (hash chain)   approval queue   Discord gate   cortex memory    |
 +------------------------------------------------------------------------------+
        |              |             |            |               |
      Atani        Daedalus       Melete     Nyx / Voodoo   Scrooge, Instagram,
                                                            dev.to, Gumroad, Discord
        ^
        |  jobs by capability name (POST /api/task)
 +--------------------- crew :8782 -----------------------+
 |  workers (one job each) -> store -> division leaders  |
 |  -> grounded reports -> Direction digest -> Moss       |
 +--------------------------------------------------------+
```

### The spine (`src/pionir/`)

| Area | Modules | What it does |
|---|---|---|
| Contracts | `contracts.py`, `registry.py`, `errors.py` | Capabilities with a risk level (`read_only`, `reversible_write`, `privileged`), permissions, model needs, and the approval and money flags. Resolving a route is deterministic: highest priority, then agent id. |
| Routing | `router.py`, `routecheck.py` | Deterministic lexical router (no model). It answers with a question when a request is ambiguous. `route-check` measures routing accuracy against known-answer probes. |
| Execution | `runtime.py`, `reliability.py` | The `Executive` runs route, circuit breaker, Bryo pacing, GPU lease, adapter, and audit. Refusals don't trip the breaker. A tripped breaker writes a lesson. |
| Resources | `scheduler.py`, `shared_gpu.py`, `benchmark.py`, `bryo_pressure.py`, `bryofeed.py` | VRAM admission with a measured budget and KV-cache cost, the shared GPU lock, evicting and re-warming models, and Bryo's pressure reading. It fails open. |
| Memory | `cortex.py`, `consolidate.py`, `recallcheck.py`, `memory.py` | SQLite memory: BM25 search, optionally fused with Ollama embeddings. Old conversation turns are folded into summaries. Shared lessons are recalled before acting. An embedder that times out pauses with a doubling cooldown instead of stalling every task. |
| Governance | `audit.py`, `approvals.py`, `improvement.py`, `atomic.py` | Hash-chained ledger; approval queue that runs each parked action once, expires it after 24 h and never replays it after a crash; promotion gate; atomic file swaps that retry on Windows. |
| Surfaces | `server.py`, `web/dashboard.html`, `discord_gate.py`, `desktop.py`, `cli.py` | Loopback HTTP API and dashboard. The Discord gate lets only Ian's ✅/❌ reaction answer an approval. There is also a Tk desktop shell and the `pionir` CLI. |
| Social | `social/card.py`, `social/post.py` | Deterministic 1080×1350 Instagram card. The approved image is pinned by sha256. |

### Adapters (`src/pionir/adapters/`)

Specialists: `atani_cli`, `galatea`, `daedalus`, `melete`, `bryo_status`, `nyx_status`,
`voodoo_status`, `crew`, and `stdio_json` (any agent declared in `specialists.toml`, see
[ADAPTER_PROTOCOL](docs/ADAPTER_PROTOCOL.md)).

Business adapters. Every one marked "Ian's yes" parks on every call:

| Capability | Gate |
|---|---|
| `content.publish` (blog on api.dokaz.net) | Ian's yes |
| `content.crosspost_devto` | Ian's yes |
| `social.instagram_post` | Ian's yes; card pinned by sha256 |
| `social.instagram_insights` | read only |
| `product.gumroad_publish` | Ian's yes; goes on sale only after every upload succeeds |
| `product.gumroad_list` | read only |
| `client.email` / `client.find_report` / `client.deliver` | Ian's yes |
| `client.orders` / `client.set_status` | read / reversible |
| `owner.notify` (Moss's daily brief and rare alerts) | rate-limited: 2 briefs and 4 alerts per 24 h; can never ping anyone |

Each outgoing payload is checked before it is parked, so Ian's approval is never spent on
something that would bounce. Delivery zips are inspected (`deliveries.py`) for secrets,
executables, path tricks, zip bombs and a missing README. They are inspected again at approval
time.

### The crew (`src/pionir/crew/`)

Workers have no model, no personality and no memory of their own. The roster is data in
`crew/catalogue.json`:

| Division | Workers |
|---|---|
| Treasury | `ledger`: Scrooge revenue in integer cents |
| Watch | `health`: site up/down and latency |
| Posting | `blog` (one checked draft a day), `instagram` (one card a day), `devto` (cross-post), `results` (traffic plus Instagram insights) |
| Contracts | `orders` (order desk, every 5 min), `delivery` (ships finished zips), `finder` ("Find it for me") |
| Products | `shelf` (staged Gumroad products), `api_builder` (placeholder) |
| Builds | `daedalus` (placeholder) |

How work flows:

1. **Workers run.** A dispatcher runs due workers on a bounded pool, with per-provider
   politeness.
2. **Results are stored.** Every attempt and output goes to one SQLite store.
3. **Words and actions are borrowed.** A worker gets words from the shared local model
   (`gemma3:12b`, one call at a time, with an hourly ceiling). It acts only by submitting Pionir
   jobs by capability name.
4. **Leaders report.** Each division leader builds a bounded, time-stamped brief. It then
   either abstains or writes a report that must pass a deterministic **grounding** check:
   - every figure matches a recorded value *and* unit;
   - no unknown names appear;
   - worker health is not misstated.

   A failing report gets one repair. After that, only the workers' own figures go up.
5. **Moss directs.** Moss reads the Direction digest and sets goals and compute shares:
   `model_calls` and `claude_escalations`, never money.
6. **Claude helps, within limits.** Claude is used only through the owner's CLI subscription,
   with a small daily cap. The finder's research gets web tools only.
7. **Silence is flagged.** Vitals flag workers that never succeed, go silent, or go stale.

Client packages, from `crew/orders.py`:

| Package | Price | Turnaround |
|---|---|---|
| Small | $149 | 3 business days |
| Standard | $399 | 5 business days |
| Find it for me | $19 | 2 business days; refunded if nothing is found |
| Custom | quoted by Ian | — |

Every client email is a fixed template. Briefs are screened, and a flagged brief becomes a
decline that Ian approves. Refunds are always issued by hand.

## Running it

Pionir targets Windows (RTX 3060 12 GB, Ollama) and is tested on Linux and Windows,
Python 3.11 and 3.12. Its only runtime dependency is Pillow.

```powershell
.\pionir.ps1            # dashboard, Galatea, Daedalus, Melete, crew and Bryo in one Terminal window
.\pionir.ps1 -Stop      # stop them all; waits for Bryo to checkpoint
.\pionir.ps1 -NoCrew -NoBryo -NoSpecialists -NoVoice   # start fewer panes
```

The launcher never installs a service, scheduled task or startup entry. Closing the window stops
everything.

Useful commands:

```bash
python -m pionir server              # dashboard + API on 127.0.0.1:8780 (+ Discord gate)
python -m pionir doctor              # ledger, specialists, routing aim, GPU, memory
python -m pionir capabilities
python -m pionir route "…" --explain # classify and run; exits 3 with a question if unsure
python -m pionir route-check         # routing accuracy against known-answer probes
python -m pionir recall-check        # memory recall against known-answer probes
python -m pionir lessons "…"         # what the bots have learned about this
python -m pionir orders | deliveries | gumroad-check | instagram-check | devto-check   # read-only
python -m pionir.crew --once         # run every crew worker and leader once, print, exit
```

Tests (CI runs the same thing):

```bash
python -m pip install "pillow>=10.1"
PYTHONPATH=src python -m unittest discover -s tests -v
```

The suite cannot reach live services. Every outside call is faked.

Configuration is through environment variables (`PIONIR_*`, `PIONIR_CREW_*`, `PIONIR_DISCORD_*`).
See [.env.example](.env.example) and [Windows setup](docs/SETUP_WINDOWS.md). Credentials live in
token files under `~/.pionir/secrets/`. They are read on each call, sent only in headers, and
scrubbed from logs. `tools/setup-*.ps1` walk through Discord, Gumroad, Instagram and dev.to.

## Documentation

- [ARCHITECTURE_DECISIONS_2026-09](docs/ARCHITECTURE_DECISIONS_2026-09.md): the settled roles
  and the harvest-before-scrap order.
- [FUSION_PLAN](docs/FUSION_PLAN.md): the staged integration plan.
- [ROUTING](docs/ROUTING.md): `/api/intent`, `/api/task`, approvals, and the GPU hand-off.
- [IMPROVEMENT_GATE](docs/IMPROVEMENT_GATE.md): propose, evaluate, authorize, apply.
- [ATANI_EXECUTIVE](docs/ATANI_EXECUTIVE.md): typed plans without arbitrary process authority.
- [ADAPTER_PROTOCOL](docs/ADAPTER_PROTOCOL.md) and
  [ADAPTER_CHECKLIST](docs/ADAPTER_CHECKLIST.md): how to add an agent.
- [SOURCE_AUDIT](docs/SOURCE_AUDIT.md): confirmed source interfaces.
- [NOTIFICATIONS](docs/NOTIFICATIONS.md): specialists keep their own notifiers.
- [PHASE0_BENCHMARK](docs/PHASE0_BENCHMARK.md): measured VRAM and latency.

## Security

Never commit transcripts, embeddings, model weights, databases, credentials, local `.env`
files, or the contents of `.techsupport_agent`. Pionir stores only configuration and adapter
code in Git. Runtime state belongs in ignored, access-controlled storage under
`PIONIR_STATE_ROOT`.
