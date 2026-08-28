"""Inspectable command-line shell for the Pionir integration spine."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from .adapters import AtaniCliAdapter, TheoPeerAdapter
from .bootstrap import PionirRuntime, build_runtime
from .contracts import Task
from .errors import PionirError


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
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
        "ask-theo-peer",
        help="ask Theo through the conversation-only Atani peer boundary",
    )
    theo.add_argument("text", nargs="+")
    theo.add_argument("--conversation")
    commands.add_parser("bryo-status", help="read Bryo's non-mutating status snapshot")
    commands.add_parser(
        "autogenesis-status",
        help="read Autogenesis controls and recent ledger events",
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
    if "theo-peer" not in runtime.adapters:
        specialists["theo-peer"] = {
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
            "maximum_heavyweight_leases": 1,
        },
        "specialists": specialists,
    }


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
        return int(
            any(
                item["status"] == "unavailable"
                for item in report["specialists"].values()
            )
        )
    if args.command == "capabilities":
        _print(_capabilities(runtime))
        return 0
    if args.command == "audit-verify":
        sequence, digest = runtime.executive.audit_sink.verify()  # type: ignore[attr-defined]
        _print({"status": "verified", "events": sequence, "head_sha256": digest})
        return 0
    if args.command == "ask-atani":
        capability = "reasoning.atani_depth" if args.depth else "reasoning.atani_chat"
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
    if args.command == "ask-theo-peer":
        adapter = runtime.adapters.get("theo-peer")
        if not isinstance(adapter, TheoPeerAdapter):
            raise ValueError("Theo peer is not configured; set PIONIR_THEO_TOKEN")
        payload = {"content": " ".join(args.text)}
        if args.conversation:
            payload["conversation_id"] = args.conversation
        result = runtime.executive.execute(Task("conversation.theo_peer_reply", payload))
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
