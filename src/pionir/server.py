"""A loopback HTTP server and dashboard that make Pionir visible and launchable.

Pionir has been a CLI: correct, inspectable, and invisible. This is the window
onto it - a local page where the routing can be watched deciding, the roster's
health and the card's VRAM seen at a glance, the audit ledger read as it grows,
and any specialist driven directly, privileged ones behind a confirm.

The endpoint logic lives in ``PionirApp`` as plain dict-returning methods so it
is testable without a socket; the HTTP handler is a thin shell over it. The
server binds loopback only - it is an admin surface for the person at the
machine, the same trust boundary as the specialists it front-ends.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bootstrap import PionirRuntime
from .cli import _capabilities, _doctor, _jsonable
from .contracts import RiskLevel, Task
from .errors import PionirError, RoutingAmbiguous
from .router import Candidate, IntentRouter, RoutingDecision
from .scheduler import observed_free_vram_mb

MAX_REQUEST_BYTES = 1_000_000
_UI_PATH = Path(__file__).parent / "web" / "dashboard.html"

# The only thing the voice runs on her own through /api/intent: asking Atani to
# think (no doer, no side effect). Everything else is a doer's job, which only
# Atani tasks - see PionirApp.intent.
_VOICE_REASONING = frozenset({"reasoning.atani_answer", "reasoning.atani_depth"})


def _decision_json(decision: RoutingDecision) -> dict[str, Any]:
    return {
        "capability": decision.capability,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "resolved": decision.resolved,
        "runner_up": decision.runner_up,
        "candidates": [_candidate_json(item) for item in decision.candidates],
    }


def _candidate_json(candidate: Candidate) -> dict[str, Any]:
    return {
        "capability": candidate.capability,
        "agent_id": candidate.agent_id,
        "description": candidate.description,
        "score": candidate.score,
        "matched": list(candidate.matched),
    }


class PionirApp:
    """Every dashboard endpoint, as plain data - no HTTP, so it is unit-testable."""

    def __init__(self, runtime: PionirRuntime) -> None:
        self.runtime = runtime
        self.router = IntentRouter(runtime.executive)

    # ---- read-only views -------------------------------------------------
    def roster(self) -> list[dict[str, Any]]:
        """The registered specialists, from the registry, without probing them.

        Deliberately does not reach out to each service - that is doctor's job
        and it is slow. The dashboard paints this instantly and fills health in
        behind it.
        """

        return _capabilities(self.runtime)

    def gpu(self) -> dict[str, Any]:
        budget = self.runtime.settings.resource_budget
        resident: list[dict[str, Any]] = []
        try:
            from .benchmark import read_loaded_models

            resident = [
                {"name": item.name, "fully_on_gpu": bool(item.fully_on_gpu)}
                for item in read_loaded_models()
            ]
        except Exception:  # noqa: BLE001 - a display must never take the server down
            resident = []
        total = used = None
        try:
            from .benchmark import read_gpu_memory

            memory = read_gpu_memory()
            total, used = memory.total_mb, memory.used_mb
        except Exception:  # noqa: BLE001
            total = used = None
        return {
            "observed_free_mb": observed_free_vram_mb(),
            "total_mb": total,
            "used_mb": used,
            "budget": {
                "total_mb": budget.total_vram_mb,
                "reserved_mb": budget.reserved_vram_mb,
                "usable_mb": budget.usable_vram_mb,
                "max_gpu_leases": budget.max_gpu_leases,
            },
            "resident": resident,
        }

    def state(self) -> dict[str, Any]:
        return {
            "roster": self.roster(),
            "gpu": self.gpu(),
            # The voice's own URL, so the dashboard can embed her glass as its
            # Voice view rather than opening her in a separate tab. None when no
            # voice is configured, and the dashboard hides the view.
            "voice_url": self.runtime.settings.galatea_url,
            "generated_at": datetime.now(UTC).isoformat(),
        }

    def doctor(self) -> dict[str, Any]:
        return _doctor(self.runtime)

    def audit(self, limit: int = 60) -> dict[str, Any]:
        sink = self.runtime.executive.audit_sink
        events = sink.recent(limit) if hasattr(sink, "recent") else []
        integrity = "verified"
        head = None
        try:
            count, head = sink.verify()  # type: ignore[attr-defined]
        except PionirError as error:
            integrity = f"broken: {error}"
            count = None
        return {
            "integrity": integrity,
            "events_total": count,
            "head_sha256": head,
            "events": events,
        }

    # ---- driving the brain ----------------------------------------------
    def classify(self, request: str) -> dict[str, Any]:
        return {"decision": _decision_json(self.router.classify(request))}

    def route(
        self,
        request: str,
        *,
        permissions: list[str] | None = None,
        execute: bool = True,
    ) -> dict[str, Any]:
        """Classify a request, and unless told otherwise run it.

        With ``execute`` false this is the 'explain' path: it shows where the
        request would go and how sure the router is, and touches no model. With
        it true, an ambiguous request comes back as a question rather than a
        guess, and any specialist or gate error is returned as data - the
        dashboard shows the failure, it does not crash on it.
        """

        decision = self.router.classify(request)
        if not execute:
            return {"executed": False, "decision": _decision_json(decision)}
        if not decision.resolved:
            return {
                "executed": False,
                "decision": _decision_json(decision),
                "question": decision.question(),
            }
        try:
            _, result = self.router.route(
                request, granted_permissions=permissions or ()
            )
        except RoutingAmbiguous as question:
            return {
                "executed": False,
                "decision": _decision_json(decision),
                "question": str(question),
            }
        except Exception as error:  # noqa: BLE001 - surfaced to the page as data
            return {
                "executed": False,
                "decision": _decision_json(decision),
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        return {
            "executed": True,
            "decision": _decision_json(decision),
            "result": _jsonable(result.output),
            "agent_id": result.agent_id,
            "evidence": list(result.evidence),
        }

    def intent(self, request: str) -> dict[str, Any]:
        """The voice's one seam for getting something done - and it reaches only
        Atani, never a doer.

        The design (Ian, 2026-09-11): the voice does not control the organs
        directly. She views and reads, and she may ask Atani for a specific bot,
        but only Atani tasks the doers. So this hands her intent to Atani and
        nothing else. Atani reasoning answers her outright. A request that needs
        a doer (Daedalus, Melete) is Atani's to dispatch - and Atani tasking the
        doers is the next piece, being built on Atani's side; until it lands,
        such a request comes back as via_manager, unrun, naming who it is for.
        Anti-confabulation still holds: what she reports is what actually came
        back, and 'not wired yet' is reported as exactly that.
        """

        decision = self.router.classify(request)
        base = {"decision": _decision_json(decision)}
        if not decision.resolved:
            return {**base, "status": "unclear", "question": decision.question()}
        cap = decision.capability
        if cap == "conversation.galatea_reply":
            return {**base, "status": "self", "note": "that routes back to the voice - answer it yourself"}
        if cap in _VOICE_REASONING:
            # Asking Atani to think: Atani answers her directly, no doer involved.
            return self._run_intent(cap, {"content": request}, frozenset({"atani.chat"}), decision)
        if self._capability_risk(cap) is RiskLevel.READ_ONLY:
            # She may view, read and look for herself (Bryo's vitals, a status
            # snapshot); that is not tasking a doer, so it runs directly.
            return self._run_intent(cap, {"content": request}, frozenset(), decision)
        # Everything else names a doer's job. The voice does not task doers - she
        # asks Atani, and Atani tasks the right bot through Pionir. Hand the whole
        # request to Atani the manager; it decides, tasks, waits, and returns what
        # actually happened. manager.atani_manage holds no GPU lease of its own,
        # so the doer's task can take the single lease.
        return self._run_intent(
            "manager.atani_manage",
            {"content": request},
            frozenset({"atani.manage"}),
            decision,
        )

    def _capability_risk(self, name: str) -> RiskLevel | None:
        for manifest in self.runtime.executive.registry.manifests():
            for capability in manifest.capabilities:
                if capability.name == name:
                    return capability.risk
        return None

    def _run_intent(self, capability, payload, permissions, decision, *, planned=False):
        try:
            result = self.runtime.executive.execute(Task(capability, payload, permissions))
        except Exception as error:  # noqa: BLE001 - returned to the voice as data
            return {
                "decision": _decision_json(decision),
                "status": "error",
                "error": {"type": type(error).__name__, "message": str(error)},
            }
        return {
            "decision": _decision_json(decision),
            "status": "planned" if planned else "done",
            "planned": planned,
            "agent_id": result.agent_id,
            "result": _jsonable(result.output),
            "evidence": list(result.evidence),
        }

    def run_task(
        self,
        capability: str,
        payload: dict[str, Any],
        *,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Invoke one named capability directly - the executive-goal path and any
        deliberate call that does not go through natural-language routing."""

        try:
            result = self.runtime.executive.execute(
                Task(capability, payload, frozenset(permissions or ()))
            )
        except Exception as error:  # noqa: BLE001 - returned as data
            return {"ok": False, "error": {"type": type(error).__name__, "message": str(error)}}
        return {
            "ok": True,
            "agent_id": result.agent_id,
            "result": _jsonable(result.output),
            "evidence": list(result.evidence),
        }


def _ui_bytes() -> bytes:
    try:
        return _UI_PATH.read_bytes()
    except OSError:
        return b"<h1>Pionir</h1><p>dashboard.html is missing.</p>"


def _make_handler(app: PionirApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Pionir/0.1"

        def log_message(self, *_args: Any) -> None:  # keep the console quiet
            return

        def _send(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any] | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_REQUEST_BYTES:
                return {} if length <= 0 else None
            try:
                document = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            return document if isinstance(document, dict) else None

        def do_GET(self) -> None:
            route = urlparse(self.path)
            if route.path in ("/", "/index.html"):
                body = _ui_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            try:
                if route.path == "/api/state":
                    self._send(app.state())
                elif route.path == "/api/doctor":
                    self._send(app.doctor())
                elif route.path == "/api/capabilities":
                    self._send({"capabilities": app.roster()})
                elif route.path == "/api/audit":
                    n = int(parse_qs(route.query).get("n", ["60"])[0] or 60)
                    self._send(app.audit(min(max(n, 1), 500)))
                else:
                    self._send({"error": "not found"}, 404)
            except Exception as error:  # noqa: BLE001
                self._send({"error": type(error).__name__, "message": str(error)}, 500)

        def do_POST(self) -> None:
            route = urlparse(self.path)
            body = self._body()
            if body is None:
                self._send({"error": "bad json"}, 400)
                return
            try:
                if route.path == "/api/route":
                    request = str(body.get("request", "")).strip()
                    if not request:
                        self._send({"error": "request is required"}, 400)
                        return
                    self._send(
                        app.route(
                            request,
                            permissions=[str(p) for p in body.get("permissions", [])],
                            execute=bool(body.get("execute", True)),
                        )
                    )
                elif route.path == "/api/intent":
                    request = str(body.get("request", "") or body.get("intent", "")).strip()
                    if not request:
                        self._send({"error": "intent is required"}, 400)
                        return
                    self._send(app.intent(request))
                elif route.path == "/api/task":
                    capability = str(body.get("capability", "")).strip()
                    if not capability:
                        self._send({"error": "capability is required"}, 400)
                        return
                    payload = body.get("payload")
                    self._send(
                        app.run_task(
                            capability,
                            payload if isinstance(payload, dict) else {},
                            permissions=[str(p) for p in body.get("permissions", [])],
                        )
                    )
                else:
                    self._send({"error": "not found"}, 404)
            except Exception as error:  # noqa: BLE001
                self._send({"error": type(error).__name__, "message": str(error)}, 500)

    return Handler


def serve(
    runtime: PionirRuntime,
    *,
    port: int = 8780,
    open_browser: bool = True,
) -> int:
    """Run the dashboard in the foreground until interrupted.

    Foreground and loopback by design: it is awake while this window is open,
    nothing starts it on its own, and closing the window stops it - the same
    contract as every other visible process in the estate.
    """

    app = PionirApp(runtime)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(app))
    url = f"http://127.0.0.1:{port}/"
    print("  PIONIR")
    print(f"  the brain is visible at {url}")
    print("  awake while this window is open; Ctrl+C stops it.")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0
