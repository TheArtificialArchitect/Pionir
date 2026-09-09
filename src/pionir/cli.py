"""Inspectable command-line shell for the Pionir integration spine."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from . import benchmark, routecheck
from .adapters import TheoAdapter
from .bootstrap import PionirRuntime, build_runtime
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
    commands.add_parser("doctor", help="check state integrity and specialist reachability")
    commands.add_parser("capabilities", help="show registered specialist contracts")
    commands.add_parser("audit-verify", help="verify the durable audit hash chain")

    atani = commands.add_parser("ask-atani", help="run Atani's bounded reasoning pipeline")
    atani.add_argument("text", nargs="+")
    atani.add_argument("--depth", action="store_true")

    atani_plan = commands.add_parser(
        "run-atani-plan",
        help="run a versioned JSON plan through Atani's bounded executive",
    )
    atani_plan.add_argument("request_file", type=Path)

    theo = commands.add_parser(
        "ask-theo",
        help="ask Theo directly: his full memory and self, with no tools",
    )
    theo.add_argument("text", nargs="+")
    theo.add_argument("--conversation")
    commands.add_parser("bryo-status", help="read Bryo's non-mutating status snapshot")
    commands.add_parser(
        "autogenesis-status",
        help="read Autogenesis controls and recent ledger events",
    )
    commands.add_parser(
        "probability-status",
        help="read Probability's redacted operational status",
    )
    commands.add_parser(
        "genesis-status",
        help="read Genesis health and life-loop counters",
    )

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
    if "theo" not in runtime.adapters:
        specialists["theo"] = {
            "status": "not_configured",
            "message": "set PIONIR_THEO_TOKEN or BRIDGE_TOKEN",
        }
    if "bryo" not in runtime.adapters:
        specialists["bryo"] = {
            "status": "not_configured",
            "message": "set PIONIR_BRYO_STATUS_COMMAND_JSON",
        }
    if "autogenesis" not in runtime.adapters:
        specialists["autogenesis"] = {
            "status": "not_configured",
            "message": "set PIONIR_AUTOGENESIS_STATUS_COMMAND_JSON",
        }
    if "probability" not in runtime.adapters:
        specialists["probability"] = {
            "status": "not_configured",
            "message": "set PIONIR_PROBABILITY_URL",
        }
    if "genesis" not in runtime.adapters:
        specialists["genesis"] = {
            "status": "not_configured",
            "message": "set PIONIR_GENESIS_URL",
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
        capability = "reasoning.atani_depth" if args.depth else "reasoning.atani_answer"
        result = runtime.executive.execute(
            Task(
                capability,
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
    if args.command == "ask-theo":
        adapter = runtime.adapters.get("theo")
        if not isinstance(adapter, TheoAdapter):
            raise ValueError("Theo is not configured; set PIONIR_THEO_TOKEN")
        payload = {"content": " ".join(args.text)}
        if args.conversation:
            payload["conversation_id"] = args.conversation
        result = runtime.executive.execute(Task("conversation.theo_reply", payload))
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
    if args.command == "autogenesis-status":
        if "autogenesis" not in runtime.adapters:
            raise ValueError(
                "Autogenesis is not configured; set "
                "PIONIR_AUTOGENESIS_STATUS_COMMAND_JSON"
            )
        result = runtime.executive.execute(
            Task("organism.autogenesis_status", {})
        )
        _print(result.output)
        return 0
    if args.command == "probability-status":
        if "probability" not in runtime.adapters:
            raise ValueError(
                "Probability is not configured; set PIONIR_PROBABILITY_URL"
            )
        result = runtime.executive.execute(
            Task("organism.probability_status", {})
        )
        _print(result.output)
        return 0
    if args.command == "genesis-status":
        if "genesis" not in runtime.adapters:
            raise ValueError("Genesis is not configured; set PIONIR_GENESIS_URL")
        result = runtime.executive.execute(Task("organism.genesis_status", {}))
        _print(result.output)
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
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _execute(args, build_runtime())
    except (PionirError, ValueError, OSError, json.JSONDecodeError) as error:
        _print({"status": "error", "error_type": type(error).__name__, "message": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
