"""Inspectable command-line shell for the Pionir integration spine."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from . import benchmark, bryofeed, recallcheck, routecheck
from .bootstrap import PionirRuntime, build_runtime
from .config import PionirSettings
from .contracts import Task
from .errors import PionirError, RoutingAmbiguous
from .router import IntentRouter
from .scheduler import canonical_model, observed_free_vram_mb


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    # Mapping, not dict. Every TaskResult.output is a MappingProxyType, which is
    # not a dict subclass, so it fell through to `default=str` and every reply
    # printed as a quoted Python repr instead of JSON - unparseable by anything
    # downstream. Seen on the first live Theo turn.
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _print(value: Any) -> None:
    print(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pionir",
        description="Resource-aware orchestration for Ian's specialist AI agents",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser(
        "server", help="run the Pionir dashboard: see the brain and drive it"
    )
    server.add_argument("--port", type=int, default=8780)
    server.add_argument(
        "--no-browser", action="store_true", help="do not open the dashboard in a browser"
    )
    commands.add_parser("doctor", help="check state integrity and specialist reachability")
    commands.add_parser("capabilities", help="show registered specialist contracts")
    commands.add_parser("audit-verify", help="verify the durable audit hash chain")

    atani = commands.add_parser("ask-atani", help="run Atani's bounded reasoning pipeline")
    atani.add_argument("text", nargs="+")

    atani_plan = commands.add_parser(
        "run-atani-plan",
        help="run a versioned JSON plan through Atani's bounded executive",
    )
    atani_plan.add_argument("request_file", type=Path)

    feed = commands.add_parser(
        "bryo-feed",
        help="write Pionir's live pulse to a cache file Bryo's sensors read (a foreground pane)",
    )
    feed.add_argument("--url", default=bryofeed.DEFAULT_URL, help="the running Pionir server")
    feed.add_argument("--out", default=bryofeed.DEFAULT_OUT, help="cache file Bryo reads")
    feed.add_argument("--interval", type=float, default=bryofeed.DEFAULT_INTERVAL,
                      help="seconds between polls")
    feed.add_argument("--once", action="store_true", help="write one snapshot and exit (for checks)")

    commands.add_parser(
        "instagram-check",
        help="check the Instagram token: who it is for and today's posting quota "
             "(reads only; never posts, never prints the token)",
    )
    commands.add_parser(
        "devto-check",
        help="check the dev.to API key: who it belongs to "
             "(reads only; never posts, never prints the key)",
    )
    gumroad = commands.add_parser(
        "gumroad-check",
        help="check the Gumroad token: whose account it is, the products and their sales "
             "(reads only; never changes a product, never prints the token)",
    )
    gumroad.add_argument(
        "--probe-upload", action="store_true",
        help="run once after setup: make a throwaway DRAFT (never published), upload a tiny "
             "zip and a cover to it the way a real publish does, report what Gumroad shows, "
             "then delete it; exit 0 only if every step passed",
    )
    orders = commands.add_parser(
        "orders",
        help="list the paid client orders on Scrooge "
             "(reads only; never emails anyone, never prints the ops token)",
    )
    orders.add_argument("--status", default=None,
                        help="only orders in this status (one of: "
                             "awaiting_payment, paid, in_progress, delivered, declined, "
                             "refunded, quote_requested, quoted)")
    commands.add_parser(
        "deliveries",
        help="list each order's zips waiting in the deliveries folder, with their sizes and "
             "sha256 and whether they pass client.deliver's checks "
             "(local only; never uploads or emails, never prints a secret)",
    )
    commands.add_parser("bryo-status", help="read Bryo's non-mutating status snapshot")
    commands.add_parser("nyx-status", help="read Nyx's redacted offensive-security health")
    commands.add_parser("voodoo-status", help="read Voodoo's redacted defensive posture")

    route = commands.add_parser(
        "route",
        help=(
            "classify a plain-English request and run it; exits 3 with a question "
            "rather than guessing when the classification is not confident"
        ),
    )
    route.add_argument("text", nargs="+")
    route.add_argument(
        "--explain",
        action="store_true",
        help="show the classification and its evidence without running anything",
    )
    route.add_argument(
        "--permission",
        action="append",
        dest="permissions",
        help=(
            "grant one permission to this request; repeatable. Permissions are not "
            "granted by default, so a denied route is reported as denied rather "
            "than quietly re-pointed at a specialist you can reach"
        ),
    )
    route.add_argument(
        "--confidence-floor",
        type=float,
        default=None,
        help="override the confidence below which the router asks instead of routing",
    )

    check = commands.add_parser(
        "route-check",
        help=(
            "measure routing aim against known-answer probes; classifies only, "
            "executes nothing, and takes no GPU lease"
        ),
    )
    check.add_argument(
        "--no-save",
        action="store_true",
        help="report without recording the result for doctor to read",
    )

    bench = commands.add_parser(
        "benchmark",
        help="measure observed VRAM, cold start, and warm latency for local models",
    )
    bench.add_argument(
        "models",
        nargs="*",
        help="model tags to measure; defaults to every installed model",
    )
    bench.add_argument(
        "--context",
        type=int,
        action="append",
        dest="contexts",
        help="context length to measure; repeat to measure KV growth (default 4096, 16384)",
    )
    bench.add_argument(
        "--ollama-url",
        default=benchmark.DEFAULT_OLLAMA_URL,
        help="local Ollama base URL",
    )

    remember = commands.add_parser(
        "remember",
        help="write one memory into Pionir's store",
    )
    remember.add_argument("text", nargs="+", help="the memory text")
    remember.add_argument(
        "--kind", default="note", help="episode, fact, canon, note, lookup, thought (default note)"
    )
    remember.add_argument("--namespace", default="shared", help="which stream it belongs to")
    remember.add_argument("--salience", type=float, default=None, help="override the kind default")

    recall = commands.add_parser(
        "recall",
        help="recall the memories most relevant to a query (lexical, no model, no GPU)",
    )
    recall.add_argument("query", nargs="+", help="what to recall about")
    recall.add_argument("-k", type=int, default=8, help="how many to return (default 8)")
    recall.add_argument("--namespace", default=None, help="scope to one stream (default: all)")

    lesson = commands.add_parser(
        "lesson",
        help="record a lesson into the shared namespace every bot reads before acting",
    )
    lesson.add_argument("text", nargs="+", help="the lesson: a mistake, a correction, a warning")
    lesson.add_argument("--slug", default=None, help="a name so other lessons can [[link]] to it")
    lesson.add_argument("--link", action="append", dest="links", default=[],
                        help="a slug this lesson points at; repeat for several")

    lessons = commands.add_parser(
        "lessons",
        help="recall the lessons relevant to what you're about to do (the recall-before-act hook)",
    )
    lessons.add_argument("intent", nargs="+", help="the task or plan you're considering")
    lessons.add_argument("-k", type=int, default=3, help="how many lessons (default 3)")

    consolidate = commands.add_parser(
        "consolidate",
        help="fold a conversation's raw turns in a namespace into a durable episode",
    )
    consolidate.add_argument("namespace", help="the conversation namespace to fold")
    consolidate.add_argument("--model", default=None,
                            help="chat model to distil with (default: PIONIR_DISTIL_MODEL)")
    consolidate.add_argument("--min-turns", type=int, default=None,
                            help="minimum un-consolidated turns before folding")

    commands.add_parser(
        "reindex-memory",
        help="embed any memories that lack a vector for the current model (hybrid recall backfill)",
    )
    rc = commands.add_parser(
        "recall-check",
        help=(
            "measure the memory engine's recall against known-answer probes; "
            "gates exact/buried recall, reports paraphrase recall as the "
            "lexical-vs-semantic signal. Lexical by default, no model, no GPU."
        ),
    )
    rc.add_argument(
        "--embed",
        action="store_true",
        help="also run the probes through the local embedder to show the hybrid lift",
    )
    return parser


def _doctor(runtime: PionirRuntime) -> dict[str, Any]:
    sequence, digest = runtime.executive.audit_sink.verify()  # type: ignore[attr-defined]
    specialists: dict[str, Any] = {}
    for agent_id, adapter in runtime.adapters.items():
        status = getattr(adapter, "status", None)
        if status is None:
            specialists[agent_id] = {"status": "no_health_contract"}
            continue
        try:
            specialists[agent_id] = {"status": "ok", "details": status()}
        except Exception as error:  # noqa: BLE001 - doctor reports isolated failures
            specialists[agent_id] = {
                "status": "unavailable",
                "error_type": type(error).__name__,
                "message": str(error),
            }
    if "bryo" not in runtime.adapters:
        specialists["bryo"] = {
            "status": "not_configured",
            "message": "set PIONIR_BRYO_STATUS_COMMAND_JSON",
        }
    if "nyx" not in runtime.adapters:
        specialists["nyx"] = {
            "status": "not_configured",
            "message": "set PIONIR_NYX_STATUS_COMMAND_JSON",
        }
    if "voodoo" not in runtime.adapters:
        specialists["voodoo"] = {
            "status": "not_configured",
            "message": "set PIONIR_VOODOO_STATUS_COMMAND_JSON",
        }
    if "galatea" not in runtime.adapters:
        specialists["galatea"] = {
            "status": "not_configured",
            "message": "set PIONIR_GALATEA_URL to wire in the voice",
        }
    if "daedalus" not in runtime.adapters:
        specialists["daedalus"] = {
            "status": "not_configured",
            "message": "PIONIR_DAEDALUS_URL is off",
        }
    if "melete" not in runtime.adapters:
        specialists["melete"] = {
            "status": "not_configured",
            "message": "PIONIR_MELETE_URL is off",
        }
    recorded = routecheck.load(runtime.settings.routing_check_path)
    if recorded is None:
        # Never measured is reported, not omitted. An absent row is the state
        # most likely to be read as fine.
        aim: dict[str, Any] = {
            "status": "never_measured",
            "message": "run 'pionir route-check' to measure routing aim",
        }
    else:
        age = recorded.age_days()
        aim = {
            "status": recorded.status,
            "accuracy": recorded.accuracy,
            "correct": recorded.correct,
            "probes": recorded.total,
            "under_ask": len(recorded.under_ask),
            "misroutes": len(recorded.misroutes),
            "age_days": age,
        }
        if age > routecheck.STALE_DAYS:
            aim["stale"] = f"last measured {age:.0f} days ago"
        # Stale isn't only about age: a check taken against a different set of
        # routable capabilities is measuring a different router. If the registered
        # set has changed since, say so and count it stale regardless of age.
        current_hash = routecheck.registry_fingerprint(IntentRouter(runtime.executive))
        if recorded.registry_hash and recorded.registry_hash != current_hash:
            aim["stale"] = (
                "the registered capability set has changed since this was measured"
            )
    return {
        "runtime": "ok",
        "state_root": str(runtime.settings.state_root),
        "audit": {
            "status": "verified",
            "events": sequence,
            "head_sha256": digest,
        },
        "gpu": {
            "total_vram_mb": runtime.settings.total_vram_mb,
            "reserved_vram_mb": runtime.settings.reserved_vram_mb,
            "usable_vram_mb": runtime.settings.resource_budget.usable_vram_mb,
            # None means unmeasurable here, not zero. Admission falls back to the
            # static budget in that case; see scheduler.observed_free_vram_mb.
            "observed_free_vram_mb": observed_free_vram_mb(),
            "maximum_heavyweight_leases": 1,
            # The exact path of the shared GPU lock, surfaced so Bryo can be
            # pointed at the same file rather than deriving it independently -
            # two components resolving one piece of state is how they desync
            # (HEAD 3.9). Pionir is the authority; this is the value to match.
            "shared_gpu_lock_path": str(runtime.settings.gpu_lock_path),
            # Every declared model against what the daemon actually holds. A
            # declaration naming a model nobody runs any more still works, but
            # silently loses the resident-model discount and starts refusing
            # turns that would have fitted - with nothing to say why.
            "declared_models": _declared_models(runtime),
        },
        # The ledger records how confident routing was. This records whether it
        # was right, which the ledger structurally cannot say.
        "routing_aim": aim,
        # Counts, not a health verdict. An empty store here is honest - ready and
        # unused, not broken - and distinguishing that from a store that recalls
        # nothing because it is broken is exactly the wired-but-inert check (3.1).
        "memory": runtime.cortex.stats(),
        "specialists": specialists,
    }


def _declared_models(runtime: PionirRuntime) -> list[dict[str, Any]]:
    try:
        resident = {canonical_model(item.name) for item in benchmark.read_loaded_models()}
        reachable = True
    except benchmark.BenchmarkError:
        resident, reachable = set(), False
    seen: dict[str, bool] = {}
    for manifest in runtime.executive.registry.manifests():
        for capability in manifest.capabilities:
            if capability.model is not None and capability.model.requires_gpu:
                # Normalised, exactly as admission does. Compared verbatim this
                # read False for a model that was resident - Theo reports his
                # build untagged and the daemon tags it `:latest` - so the view
                # built to show declaration drift was inventing it, while the
                # scheduler underneath was correct. Observed live 2026-09-04.
                seen[capability.model.model_id] = (
                    canonical_model(capability.model.model_id) in resident
                )
    return [
        {"model_id": name, "resident": seen[name] if reachable else None}
        for name in sorted(seen)
    ]


def _capabilities(runtime: PionirRuntime) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for manifest in runtime.executive.registry.manifests():
        for capability in manifest.capabilities:
            output.append(
                {
                    "agent_id": manifest.agent_id,
                    "agent_version": manifest.version,
                    "name": capability.name,
                    "description": capability.description,
                    "risk": capability.risk.value,
                    "required_permissions": sorted(capability.required_permissions),
                    "model": (
                        {
                            "id": capability.model.model_id,
                            "estimated_vram_mb": capability.model.total_vram_mb,
                        }
                        if capability.model is not None
                        else None
                    ),
                }
            )
    return output


def _execute(args: argparse.Namespace, runtime: PionirRuntime) -> int:
    if args.command == "server":
        # Imported here, not at module load: server imports this module, so a
        # top-level import would be circular.
        from .server import serve

        return serve(runtime, port=args.port, open_browser=not args.no_browser)
    if args.command == "doctor":
        report = _doctor(runtime)
        _print(report)
        unavailable = any(
            item["status"] == "unavailable"
            for item in report["specialists"].values()
        )
        return int(unavailable or report["routing_aim"]["status"] == "failing")
    if args.command == "capabilities":
        _print(_capabilities(runtime))
        return 0
    if args.command == "audit-verify":
        sequence, digest = runtime.executive.audit_sink.verify()  # type: ignore[attr-defined]
        _print({"status": "verified", "events": sequence, "head_sha256": digest})
        return 0
    if args.command == "ask-atani":
        result = runtime.executive.execute(
            Task(
                "reasoning.atani_answer",
                {"content": " ".join(args.text)},
                frozenset({"atani.chat"}),
            )
        )
        _print(result.output)
        return 0
    if args.command == "run-atani-plan":
        request = json.loads(args.request_file.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Atani executive request file must contain a JSON object")
        result = runtime.executive.execute(
            Task(
                "executive.atani_run",
                request,
                frozenset({"atani.executive"}),
            )
        )
        _print(result.output)
        return 0
    if args.command == "bryo-status":
        if "bryo" not in runtime.adapters:
            raise ValueError(
                "Bryo is not configured; set PIONIR_BRYO_STATUS_COMMAND_JSON"
            )
        result = runtime.executive.execute(Task("organism.bryo_status", {}))
        _print(result.output)
        return 0
    if args.command == "nyx-status":
        if "nyx" not in runtime.adapters:
            raise ValueError("Nyx is not configured; set PIONIR_NYX_STATUS_COMMAND_JSON")
        _print(runtime.executive.execute(Task("security.nyx_status", {})).output)
        return 0
    if args.command == "voodoo-status":
        if "voodoo" not in runtime.adapters:
            raise ValueError("Voodoo is not configured; set PIONIR_VOODOO_STATUS_COMMAND_JSON")
        _print(runtime.executive.execute(Task("security.voodoo_status", {})).output)
        return 0
    if args.command == "route":
        router = IntentRouter(
            runtime.executive,
            **(
                {"confidence_floor": args.confidence_floor}
                if args.confidence_floor is not None
                else {}
            ),
        )
        request = " ".join(args.text)
        if args.explain:
            _print(router.classify(request))
            return 0
        # One-shot CLI run: prime Bryo's pressure with a single synchronous read
        # before executing, so a stressed organism can actually pace this. peek()
        # alone would be neutral for the whole short-lived process.
        reader = getattr(runtime, "pressure_reader", None)
        read_now = getattr(reader, "read_now", None)
        if callable(read_now):
            try:
                read_now()
            except Exception as exc:  # noqa: BLE001 - the body is advisory, never load-bearing
                # advisory, but said out loud: a silent miss runs the task as if Bryo were calm
                print(f"pionir: could not read Bryo's pressure ({type(exc).__name__}: {exc}); "
                      "running without it", file=sys.stderr)
        try:
            decision, result = router.route(
                request, granted_permissions=frozenset(args.permissions or ())
            )
        except RoutingAmbiguous as question:
            _print(
                {
                    "status": "question",
                    "question": str(question),
                    "confidence": (
                        question.decision.confidence if question.decision else 0.0
                    ),
                    "reason": question.decision.reason if question.decision else "unknown",
                    "options": [
                        candidate.capability
                        for candidate in (
                            question.decision.candidates if question.decision else ()
                        )
                    ],
                }
            )
            return 3
        _print(
            {
                "routed_to": decision.capability,
                "confidence": decision.confidence,
                "agent_id": result.agent_id,
                "output": dict(result.output),
            }
        )
        return 0
    if args.command == "route-check":
        result = routecheck.run(IntentRouter(runtime.executive))
        if not args.no_save:
            routecheck.save(runtime.settings.routing_check_path, result)
        _print(
            {
                "status": result.status,
                "accuracy": result.accuracy,
                "correct": result.correct,
                "probes": result.total,
                "under_ask": [item.request for item in result.under_ask],
                "misroutes": [
                    {"request": item.request, "expected": item.expected, "got": item.actual}
                    for item in result.misroutes
                ],
                "skipped": list(result.skipped),
                "results": [item for item in result.results],
            }
        )
        return 0 if result.status != "failing" else 1
    if args.command == "benchmark":
        models = tuple(args.models) or tuple(
            benchmark.list_installed_models(args.ollama_url)
        )
        if not models:
            raise ValueError("no models are installed in the local Ollama daemon")
        contexts = tuple(args.contexts) if args.contexts else (4096, 16384)
        report = benchmark.run(models, contexts=contexts, base_url=args.ollama_url)
        _print(report)
        return 0
    if args.command == "remember":
        memory_id = runtime.cortex.remember(
            args.kind, " ".join(args.text), namespace=args.namespace, salience=args.salience
        )
        _print({"remembered": memory_id, "stats": runtime.cortex.stats()})
        return 0
    if args.command == "recall":
        hits = runtime.cortex.recall(" ".join(args.query), k=args.k, namespace=args.namespace)
        _print(
            {
                "query": " ".join(args.query),
                "recalled": [
                    {
                        "kind": memory.kind,
                        "namespace": memory.namespace,
                        "score": round(memory.score, 3),
                        "text": memory.text,
                    }
                    for memory in hits
                ],
            }
        )
        return 0
    if args.command == "lesson":
        lesson_id = runtime.cortex.record_lesson(
            " ".join(args.text), slug=args.slug, links=tuple(args.links)
        )
        _print({"recorded_lesson": lesson_id, "stats": runtime.cortex.stats()})
        return 0
    if args.command == "lessons":
        hits = runtime.cortex.lessons_for(" ".join(args.intent), k=args.k)
        _print(
            {
                "before you act on": " ".join(args.intent),
                "lessons": [
                    {
                        "text": memory.text,
                        "score": round(memory.score, 3),
                        "via": memory.via or "direct",
                    }
                    for memory in hits
                ],
            }
        )
        return 0
    if args.command == "consolidate":
        from .consolidate import DEFAULT_MIN_TURNS, Consolidator, OllamaDistiller

        # A chat model, never the embed model: nomic-embed-text cannot chat, so
        # distilling with it always declined and consolidation never happened.
        model = args.model or runtime.settings.distil_model
        if not model:
            raise ValueError("no distil model; pass --model or set PIONIR_DISTIL_MODEL")
        if runtime.settings.embed_model and model == runtime.settings.embed_model:
            raise ValueError(
                f"distil model {model!r} is the embedding model, which cannot chat; "
                "pass --model <chat model> or set PIONIR_DISTIL_MODEL"
            )
        consolidator = Consolidator(runtime.cortex, OllamaDistiller(model))
        outcome = consolidator.consolidate(
            args.namespace, min_turns=args.min_turns or DEFAULT_MIN_TURNS
        )
        if outcome is None:
            _print({"consolidated": False, "reason": "not enough turns, or the distiller declined"})
        else:
            _print(
                {
                    "consolidated": True,
                    "episode_id": outcome.episode_id,
                    "facts": len(outcome.fact_ids),
                    "folded_turns": outcome.folded_turns,
                }
            )
        return 0
    if args.command == "reindex-memory":
        filled = runtime.cortex.reindex()
        _print({"reindexed": filled, "stats": runtime.cortex.stats()})
        return 0
    if args.command == "recall-check":
        embedder = runtime.cortex.embedder if getattr(args, "embed", False) else None
        result = recallcheck.run(embedder=embedder)
        _print(result.to_dict())
        # Non-zero when gated recall falls below the floor, so CI can gate on it.
        return 0 if result.passed else 1
    return 2


def instagram_check(settings: PionirSettings | None = None, *, opener: Any = None) -> int:
    """``pionir instagram-check``: 0 ok, 1 not configured (or could not check), 2 rejected.
    No runtime is built: it reads the token file and asks the Graph API two questions."""
    from .adapters.instagram import InstagramAdapter, InstagramSettings

    configured = settings or PionirSettings.from_environment()
    if configured.instagram_graph_url is None:
        _print({"status": "not_configured", "message": "PIONIR_INSTAGRAM_GRAPH_URL is off"})
        return 1
    adapter = InstagramAdapter(InstagramSettings(graph_url=configured.instagram_graph_url,
                                                 token_file=configured.instagram_token_path),
                               opener=opener)
    code, report = adapter.check_account()
    _print(report)
    return code


def devto_check(settings: PionirSettings | None = None, *, opener: Any = None) -> int:
    """``pionir devto-check``: 0 ok, 1 not configured (or could not check), 2 rejected.
    No runtime is built: it reads the key file and asks dev.to one question (GET
    /users/me). Never posts, never prints the key."""
    from .adapters.devto import DevtoAdapter, DevtoSettings

    configured = settings or PionirSettings.from_environment()
    if configured.devto_url is None:
        _print({"status": "not_configured", "message": "PIONIR_DEVTO_URL is off"})
        return 1
    adapter = DevtoAdapter(DevtoSettings(api_url=configured.devto_url,
                                         key_file=configured.devto_key_path,
                                         ledger_file=configured.devto_ledger_path),
                           opener=opener)
    code, report = adapter.check_account()
    _print(report)
    return code


def gumroad_check(settings: PionirSettings | None = None, *, opener: Any = None,
                  probe_upload: bool = False) -> int:
    """``pionir gumroad-check``: 0 ok, 1 not configured (or could not check), 2 rejected.
    No runtime is built: it reads the token file and asks Gumroad who it is and what it
    sells (GET /v2/user, GET /v2/products). Never changes a product, never prints the
    token. With ``--probe-upload`` it instead proves the upload paths on a throwaway
    draft that is never published and is deleted at the end (ProductAdapter.probe_upload)."""
    from .adapters.products import ProductAdapter, product_settings

    configured = settings or PionirSettings.from_environment()
    if configured.gumroad_url is None:
        _print({"status": "not_configured", "message": "PIONIR_GUMROAD_URL is off"})
        return 1
    adapter = ProductAdapter(product_settings(configured), opener=opener)
    if probe_upload:
        return adapter.probe_upload(say=lambda line: print(line, flush=True))
    code, report = adapter.check_account()
    _print(report)
    return code


def orders(settings: PionirSettings | None = None, *, status: str | None = None,
           opener: Any = None) -> int:
    """``pionir orders``: 0 ok, 1 not configured (or could not list), 2 token rejected.
    No runtime is built: it reads the ops token file and asks Scrooge one question (GET
    /dash/orders). Never emails anyone, never prints the token."""
    from .adapters.clients import ClientAdapter, ClientSettings

    configured = settings or PionirSettings.from_environment()
    if configured.content_url is None:
        _print({"status": "not_configured", "message": "PIONIR_CONTENT_URL is off"})
        return 1
    adapter = ClientAdapter(ClientSettings(base_url=configured.content_url,
                                           token_file=configured.ops_token_path),
                            opener=opener)
    code, report = adapter.list_orders(status)
    _print(report)
    return code


def deliveries(settings: PionirSettings | None = None) -> int:
    """``pionir deliveries``: 0 listed, 1 no deliveries folder (or the secrets could not
    be read). No runtime, no network: it runs client.deliver's checks on each zip under
    the deliveries folder and prints the reasons, never a secret value."""
    from .adapters.clients import ClientAdapter, client_settings

    configured = settings or PionirSettings.from_environment()
    code, report = ClientAdapter(client_settings(configured)).list_deliveries()
    _print(report)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "deliveries":
        try:
            return deliveries()
        except (ValueError, OSError) as error:
            _print({"status": "error", "error_type": type(error).__name__,
                    "message": str(error)})
            return 1
    for name, check in (("instagram-check", instagram_check), ("devto-check", devto_check),
                        ("gumroad-check", gumroad_check)):
        if args.command == name:
            try:
                if getattr(args, "probe_upload", False):
                    return gumroad_check(probe_upload=True)
                return check()
            except (ValueError, OSError) as error:
                _print({"status": "error", "error_type": type(error).__name__,
                        "message": str(error)})
                return 1
    if args.command == "orders":
        try:
            return orders(status=args.status)
        except (ValueError, OSError) as error:
            _print({"status": "error", "error_type": type(error).__name__,
                    "message": str(error)})
            return 1
    if args.command == "bryo-feed":
        # A standalone poller: it reads the running server over HTTP and needs no
        # runtime of its own (no Cortex, no GPU lock), so it short-circuits here.
        return bryofeed.run_feed(url=args.url, out=args.out, interval=args.interval, once=args.once)
    try:
        return _execute(args, build_runtime())
    except (PionirError, ValueError, OSError, json.JSONDecodeError) as error:
        _print({"status": "error", "error_type": type(error).__name__, "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
