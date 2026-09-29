"""The Ollama gate: another program's model calls, under Pionir's GPU arbitration.

Peter (C:\\src\\The-Web) distils news with ``qwen2.5:7b-instruct`` against a HARD-CODED
``http://127.0.0.1:11434`` (``peter/distiller/ollama.py``: "never configurable") and
takes no GPU lease. With Moss's ``gemma3:12b`` resident the card shows ~10.8 of 12.3 GB
used, the 7B does not fit, and Ollama silently runs it CPU-offloaded - the exact
failure ``scheduler.py`` exists to refuse. Peter may not be edited, so the gate reaches
him from outside: his pane runs with ``HTTP_PROXY`` pointed here, Python's urllib sends
his plain-HTTP calls to this proxy (``NO_PROXY`` names the few plain-HTTP news feeds he
reads, so they still go direct), and every model call is placed by Pionir:

- **GPU, under a lease.** The gate takes the shared GPU lease through a
  ``ModelLeaseScheduler`` (the same lock, budget, protected models and handback the
  Daedalus path uses). Holding it makes Moss stand down at her next tick; the gate then
  waits for her to SAY so (her state reports ``gpu.yielding``) before anything of hers
  is sidelined - never mid-turn. The lease lingers a little after the last call so a
  burst (one call per subject per cycle) pays one swap, not twenty; when it ends the
  handback unloads the 7B and re-warms her model.
- **CPU, explicitly.** When the lease cannot be had (another tenant holds the card),
  Moss does not stand down in time, or the gate is set to ``cpu``, the call is forwarded
  with ``options.num_gpu = 0``: Ollama runs it on the CPU because Pionir said so, and the
  placement is logged, counted, audited and returned in ``X-Pionir-Placement``. Never a
  silent spill.

The request filter is ported from the night-builds gate (``build_sandbox.OllamaGate``,
the build Daedalus's gate on :8773): the model listing reads, and chat / generate /
embed / show for the allowed models only - never pull, delete, create, copy or push, and
never another model. It is one gate with one filter; the build sandbox's copy should
become an instance of this class when that branch lands (it passes no arbiter).
"""
from __future__ import annotations

import http.client
import json
import logging
import select
import socket
import threading
import time
import urllib.error
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .contracts import ModelRequirement
from .errors import ResourceUnavailable
from .scheduler import ModelLease, ModelLeaseScheduler, canonical_model, kv_cache_vram_mb

_log = logging.getLogger(__name__)

GATE_PORT = 8774
GATE_READS = frozenset({"/api/tags", "/api/ps", "/api/version"})
GATE_MODEL_CALLS = frozenset({"/api/chat", "/api/generate", "/api/embed", "/api/embeddings",
                              "/api/show"})
# the calls that load a model (show only reads its card)
GATE_LOADING_CALLS = GATE_MODEL_CALLS - {"/api/show"}
GATE_MAX_BODY = 8_000_000
STATUS_PATH = "/pionir/gate"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Peter's distiller model and what it costs on the card: ~4.7 GB of weights (measured by
# the survey, 2026-09-28) plus the KV cache for Ollama's default 4096 context.
PETER_MODEL = "qwen2.5:7b-instruct"
PETER_REQUIREMENT_MB = 4_700
# How long a caller waits before it gives up: Peter's distiller times out at 120 s. A call
# the gate cannot finish inside this budget is abandoned - never left holding the queue.
CALLER_BUDGET_SECONDS = 110.0


class GateError(RuntimeError):
    pass


class Abandoned(RuntimeError):
    """The caller hung up or ran out of time; its upstream call was closed."""


def peter_requirement(model: str) -> ModelRequirement:
    """What one of the gate's models needs. ``exclusive_card``: on this 12 GB card it
    cannot sit beside the voice's 12B, so admitting it means sidelining hers - which the
    scheduler does only under the lease, and the gate only once she has stood down."""
    return ModelRequirement(
        model_id=model,
        estimated_vram_mb=PETER_REQUIREMENT_MB,
        context_vram_mb=kv_cache_vram_mb(4096),
        requires_gpu=True,
        exclusive_card=True,
    )


def guarded_evictor(evict: Callable[[str], None], voice_idle: "VoiceProbe",
                    protected: Iterable[str]) -> Callable[[str], None]:
    """The unload the gate's scheduler uses: a protected model (the voice's) is unloaded only
    if the voice is provably idle AT THAT MOMENT - asked again right before the unload, not
    only when the lease was taken. Otherwise it refuses (the scheduler then keeps her model
    and the call goes to the CPU on purpose)."""
    guarded = frozenset(canonical_model(m) for m in protected)

    def run(name: str) -> None:
        if canonical_model(name) in guarded:
            try:
                idle = voice_idle()
            except Exception:  # noqa: BLE001 - unreadable is not idle
                idle = None
            if idle is not True:
                raise GateError(f"{name} kept: the voice is not provably idle right now")
        evict(name)

    return run


def client_gone(sock: socket.socket) -> bool:
    """True when the caller has hung up: its socket reads as closed. Never blocks."""
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return False
        return sock.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def upstream_of(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise GateError("the Ollama gate forwards to a loopback Ollama only")
    return parsed.hostname, parsed.port or 11434


def target_path(raw: str, upstream: tuple[str, int]) -> str | None:
    """The Ollama path a request names, or None if it names anything else.

    A client that reaches the gate through ``HTTP_PROXY`` sends the absolute form
    (``POST http://127.0.0.1:11434/api/generate``); a client pointed straight at the gate
    sends the origin form (``/api/generate``). Only the upstream Ollama's own authority
    is served: anything else sent through the proxy is refused, never forwarded - the
    gate is not a general proxy."""
    if raw.startswith("/"):
        return raw.split("?", 1)[0]
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "http" or parsed.hostname is None:
        return None
    port = parsed.port or 80
    if parsed.hostname not in LOOPBACK_HOSTS or port != upstream[1]:
        return None
    return parsed.path or "/"


def refusal(method: str, path: str, body: bytes, models: frozenset[str]) -> str | None:
    """Why this request may not pass, or None (the night-builds filter, many models)."""
    if method == "GET":
        return None if path in GATE_READS else f"GET {path} is not allowed"
    if method != "POST" or path not in GATE_MODEL_CALLS:
        return f"{method} {path} is not allowed"
    try:
        doc = json.loads(body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return "the body is not JSON"
    model = (doc.get("model") or doc.get("name")) if isinstance(doc, dict) else None
    if model not in models:
        return f"only {', '.join(sorted(models))} may be used, not {str(model)[:60]!r}"
    return None


def on_cpu(body: bytes) -> bytes:
    """The same request, told to run on the CPU (``options.num_gpu = 0``)."""
    doc = json.loads(body.decode("utf-8") or "{}")
    options = doc.get("options") if isinstance(doc.get("options"), dict) else {}
    doc["options"] = {**options, "num_gpu": 0}
    return json.dumps(doc).encode("utf-8")


@dataclass(frozen=True, slots=True)
class Placement:
    where: str          # "gpu" | "cpu"
    reason: str


VoiceProbe = Callable[[], "bool | None"]


class GpuArbiter:
    """Decides, per model call, GPU under a lease or CPU on purpose - and holds the lease
    across a burst of calls.

    ``voice_idle`` answers True when the voice has stood down (her state says she is
    yielding) or is not running at all, False while she is up and not yet yielding, and
    None when it cannot tell - which counts as not idle. The gate is serial, so
    ``place``/``done`` are never called concurrently; ``reap`` runs from a timer thread and
    shares the lock."""

    def __init__(
        self,
        scheduler: ModelLeaseScheduler | None,
        *,
        voice_idle: VoiceProbe,
        requirement_for: Callable[[str], ModelRequirement] = peter_requirement,
        owner: str = "peter",
        mode: str = "auto",
        confirm_seconds: float = 20.0,
        linger_seconds: float = 30.0,
        poll_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_event: Callable[[str, str], None] | None = None,
    ) -> None:
        if mode not in ("auto", "cpu"):
            raise GateError(f"gate mode must be auto or cpu, not {mode!r}")
        self.scheduler = scheduler
        self.voice_idle = voice_idle
        self.requirement_for = requirement_for
        self.owner = owner
        self.mode = mode
        self.confirm_seconds = max(0.0, confirm_seconds)
        self.linger_seconds = max(0.0, linger_seconds)
        self.poll_seconds = max(0.01, poll_seconds)
        self._clock = clock
        self._sleep = sleep
        self._on_event = on_event
        self._lock = threading.RLock()
        self._lease: ModelLease | None = None
        self._lease_model: str | None = None
        self._last_used = 0.0
        self.counts = {"gpu": 0, "cpu": 0, "leases": 0, "refused": 0}
        self.recent: deque[dict[str, Any]] = deque(maxlen=20)
        # where the last model call ran, and why - The Web's inspector shows it
        self.last: dict[str, Any] | None = None

    # ------------------------------------------------------------------ decisions
    def _event(self, kind: str, detail: str) -> None:
        self.recent.appendleft({"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "event": kind, "detail": detail[:300]})
        _log.info("ollama gate: %s: %s", kind, detail)
        if self._on_event is not None:
            try:
                self._on_event(kind, detail)
            except Exception as error:  # noqa: BLE001 - auditing must never wedge a call
                _log.warning("ollama gate: event callback failed: %s", error)

    def _settle(self) -> bool:
        """Wait (bounded) for the voice to say she has stood down."""
        deadline = self._clock() + self.confirm_seconds
        while True:
            try:
                idle = self.voice_idle()
            except Exception as error:  # noqa: BLE001 - an unreadable voice is not an idle one
                _log.warning("ollama gate: voice probe failed: %s", error)
                idle = None
            if idle is True:
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(self.poll_seconds)

    def place(self, model: str) -> Placement:
        with self._lock:
            if self.mode == "cpu":
                return self._cpu(model, "the gate is set to CPU (PIONIR_OLLAMA_GATE_MODE=cpu)")
            if self._lease is not None and self._lease_model == model:
                self._last_used = self._clock()
                return self._gpu("lease held for this burst", model)
            self._release_locked("another model asked for the card")
            if self.scheduler is None:
                return self._cpu(model, "no GPU scheduler is wired")
            requirement = self.requirement_for(model)
            try:
                lease = self.scheduler.acquire(requirement, purpose=f"{self.owner}: {model}",
                                               settle=self._settle)
            except ResourceUnavailable as error:
                return self._cpu(model, str(error))
            except Exception as error:  # noqa: BLE001 - a broken probe must still place the call
                return self._cpu(model, f"{type(error).__name__}: {error}")
            self._lease, self._lease_model = lease, model
            self._last_used = self._clock()
            self.counts["leases"] += 1
            self._event("gpu.lease", f"{self.owner}: {model} has the card (the voice stood down)")
            return self._gpu("leased the card", model)

    def _note_last(self, where: str, model: str, why: str) -> None:
        self.last = {"where": where, "model": model, "reason": why[:200],
                     "at": datetime.now(UTC).isoformat(timespec="seconds")}

    def _gpu(self, why: str, model: str = "") -> Placement:
        self.counts["gpu"] += 1
        self._note_last("gpu", model or (self._lease_model or ""), why)
        return Placement("gpu", why)

    def _cpu(self, model: str, why: str) -> Placement:
        self.counts["cpu"] += 1
        self._note_last("cpu", model, why)
        self._event("gpu.cpu", f"{self.owner}: {model} runs on the CPU, on purpose: {why}")
        return Placement("cpu", why)

    def done(self) -> None:
        with self._lock:
            if self._lease is not None:
                self._last_used = self._clock()

    def reap(self) -> None:
        """Let the lease go once the burst is over (no call for ``linger_seconds``)."""
        with self._lock:
            if self._lease is not None and self._clock() - self._last_used >= self.linger_seconds:
                self._release_locked("the burst is over")

    def _release_locked(self, why: str) -> None:
        lease, model = self._lease, self._lease_model
        self._lease, self._lease_model = None, None
        if lease is None:
            return
        try:
            lease.release()      # handback: unload the 7B, re-warm the voice's model
        finally:
            self._event("gpu.release", f"{self.owner}: {model} gave the card back ({why})")

    def close(self) -> None:
        with self._lock:
            self._release_locked("the gate is stopping")

    def refused(self, why: str) -> None:
        with self._lock:
            self.counts["refused"] += 1
            self._event("refused", why)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"mode": self.mode, "holding": self._lease_model, "last": self.last,
                    "counts": dict(self.counts), "recent": list(self.recent)[:8]}


class OllamaGate:
    """A loopback proxy in front of Ollama. Without an arbiter it is the night-builds gate
    (filter only); with one, every loading call is placed on the GPU under a lease or on
    the CPU on purpose."""

    def __init__(self, models: Iterable[str], *, upstream: str = "http://127.0.0.1:11434",
                 port: int = GATE_PORT, arbiter: GpuArbiter | None = None,
                 reap_seconds: float = 1.0, caller_budget: float = CALLER_BUDGET_SECONDS,
                 watch_seconds: float = 0.25) -> None:
        self.models = frozenset(m for m in models if m)
        if not self.models:
            raise GateError("the Ollama gate needs at least one allowed model")
        self.upstream = upstream_of(upstream)
        self.port = port
        self.arbiter = arbiter
        self.reap_seconds = reap_seconds
        self.caller_budget = caller_budget
        self.watch_seconds = watch_seconds
        self.abandoned = 0
        self.refused: list[str] = []
        self._serial = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def status(self) -> dict[str, Any]:
        return {"url": self.url, "models": sorted(self.models),
                "upstream": f"{self.upstream[0]}:{self.upstream[1]}",
                "running": self._server is not None, "caller_budget_s": self.caller_budget,
                "abandoned": self.abandoned,
                "arbiter": self.arbiter.snapshot() if self.arbiter is not None else None}

    def _refuse_note(self, why: str) -> None:
        self.refused.append(why)
        del self.refused[:-50]
        if self.arbiter is not None:
            self.arbiter.refused(why)

    def forward(self, method: str, path: str, body: bytes, *, deadline: float | None = None,
                gone: Callable[[], bool] = lambda: False) -> tuple[int, dict[str, str], Any]:
        """Place and forward one allowed call. Returns (status, headers, chunk iterator).

        ``deadline`` (monotonic) is the caller's budget: the upstream call gets no more than
        what is left of it. A watcher closes the upstream connection the moment the caller
        hangs up (``gone``) or the budget runs out - Ollama stops generating for a closed
        connection - so an abandoned call never holds the queue for its full length.
        Raises ``Abandoned`` in that case."""
        deadline = deadline if deadline is not None else time.monotonic() + self.caller_budget
        placement: Placement | None = None
        if method == "POST" and path in GATE_LOADING_CALLS and self.arbiter is not None:
            model = json.loads(body.decode("utf-8") or "{}").get("model") or ""
            placement = self.arbiter.place(str(model))
            if placement.where == "cpu":
                body = on_cpu(body)
        left = deadline - time.monotonic()
        if gone() or left <= 0:
            self._done()
            raise Abandoned("the caller went away" if left > 0 else "the caller's time ran out")
        conn = http.client.HTTPConnection(*self.upstream, timeout=max(1.0, left))
        finished = threading.Event()
        why: list[str] = []

        def watch() -> None:
            while not finished.wait(self.watch_seconds):
                reason = ("the caller went away" if gone() else
                          "the caller's time ran out" if time.monotonic() > deadline else None)
                if reason:
                    why.append(reason)
                    try:
                        if conn.sock is not None:
                            conn.sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    conn.close()
                    return

        threading.Thread(target=watch, name="pionir-gate-watch", daemon=True).start()
        try:
            conn.request(method, path, body=body or None, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException, AttributeError) as error:
            finished.set()
            conn.close()
            self._done()
            if why:
                raise Abandoned(why[0]) from error
            raise OSError(str(error)) from error
        headers = {"Content-Type": resp.getheader("Content-Type") or "application/json"}
        if placement is not None:
            headers["X-Pionir-Placement"] = f"{placement.where}; {placement.reason}"[:300]

        def chunks():
            try:
                while True:
                    try:
                        chunk = resp.read(65536)
                    except (OSError, http.client.HTTPException, AttributeError) as error:
                        raise Abandoned(why[0] if why else str(error)) from error
                    if not chunk:
                        break
                    yield chunk
            finally:
                finished.set()
                conn.close()
                self._done()

        return resp.status, headers, chunks()

    def _done(self) -> None:
        if self.arbiter is not None:
            self.arbiter.done()

    def _abandon(self, why: str) -> None:
        self.abandoned += 1
        self._refuse_note(f"abandoned: {why}")

    def start(self) -> None:
        gate = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:
                pass

            def _json(self, status: int, doc: Any) -> None:
                data = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _refuse(self, why: str) -> None:
                gate._refuse_note(why)
                self._json(403, {"error": f"refused by the Pionir Ollama gate: {why}"})

            def _handle(self, method: str) -> None:
                arrived = time.monotonic()
                if method == "GET" and self.path.split("?", 1)[0] == STATUS_PATH:
                    return self._json(200, gate.status())
                path = target_path(self.path, gate.upstream)
                if path is None:
                    return self._refuse(f"{method} {self.path[:120]} is not Ollama; "
                                        "the gate is not a general proxy")
                length = int(self.headers.get("Content-Length") or 0)
                if length > GATE_MAX_BODY:
                    return self._refuse("the request is too large")
                body = self.rfile.read(length) if length else b""
                why = refusal(method, path, body, gate.models)
                if why:
                    return self._refuse(why)
                # The caller's budget starts when it asked. Waiting its turn, it is dropped the
                # moment it hangs up or its time runs out - the queue never serves the dead.
                deadline = arrived + gate.caller_budget
                gone = lambda: client_gone(self.connection)  # noqa: E731
                while not gate._serial.acquire(timeout=gate.watch_seconds):
                    if gone():
                        return gate._abandon("the caller went away while queued")
                    if time.monotonic() > deadline:
                        gate._abandon("the caller's time ran out while queued")
                        return self._json(504, {"error": "the Pionir Ollama gate: no turn inside the caller's budget"})
                try:
                    try:
                        status, headers, chunks = gate.forward(method, path, body, deadline=deadline,
                                                               gone=gone)
                    except Abandoned as exc:
                        gate._abandon(str(exc))
                        return None if gone() else self._json(504, {"error": f"abandoned: {exc}"})
                    except OSError as exc:
                        gate._refuse_note(f"upstream: {exc}")
                        return self._json(502, {"error": f"Ollama is unreachable: {exc}"})
                    self.send_response(status)
                    for name, value in headers.items():
                        self.send_header(name, value)
                    self.end_headers()
                    try:
                        for chunk in chunks:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except Abandoned as exc:
                        gate._abandon(str(exc))
                    except OSError as exc:
                        gate._abandon(f"the caller went away: {exc}")
                finally:
                    gate._serial.release()
                return None

            def do_GET(self) -> None:          # noqa: N802 - the http.server name
                self._handle("GET")

            def do_POST(self) -> None:         # noqa: N802
                self._handle("POST")

            def do_DELETE(self) -> None:       # noqa: N802
                self._refuse(f"DELETE {self.path[:120]} is not allowed")

            def do_PUT(self) -> None:          # noqa: N802
                self._refuse(f"PUT {self.path[:120]} is not allowed")

            def do_HEAD(self) -> None:         # noqa: N802
                self._refuse(f"HEAD {self.path[:120]} is not allowed")

            def do_CONNECT(self) -> None:      # noqa: N802 - HTTPS tunnelling: never
                self._refuse("CONNECT is not allowed; the gate is not a general proxy")

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]     # port 0 (tests): the one it got
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="pionir-ollama-gate", daemon=True)
        self._thread.start()
        if self.arbiter is not None:
            self._stop.clear()
            self._reaper = threading.Thread(target=self._reap_loop, name="pionir-gate-reaper",
                                            daemon=True)
            self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(self.reap_seconds):
            try:
                assert self.arbiter is not None
                if self._serial.acquire(blocking=False):
                    try:
                        self.arbiter.reap()
                    finally:
                        self._serial.release()
            except Exception as error:  # noqa: BLE001 - logged, the loop goes on
                _log.warning("ollama gate: reaping the lease failed: %s", error)

    def stop(self) -> None:
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self.arbiter is not None:
            self.arbiter.close()


def voice_probe(adapter: Any) -> VoiceProbe:
    """Moss's side of the handshake, read through Pionir's own Galatea adapter.

    True: her state says ``gpu.yielding`` (she has stood down for the lease) AND
    ``gpu.in_flight == 0`` (no model call of hers on the wire - "yielding" alone is set at
    the top of a tick that can still go on into a model call), or she is not running at all
    (connection refused). False: she is up and busy or not yielding yet. None: it cannot be
    told (an auth error, a timeout, a Galatea too old to report in_flight) - never idle."""
    from .errors import AdapterUnavailable

    def probe() -> bool | None:
        if adapter is None:
            return True
        try:
            state = adapter.status()
        except AdapterUnavailable as error:
            cause = error.__cause__
            reason = getattr(cause, "reason", cause)
            return True if isinstance(reason, ConnectionRefusedError) else None
        except Exception:  # noqa: BLE001 - unknown is not idle
            return None
        gpu = state.get("gpu") if isinstance(state, Mapping) else None
        if not isinstance(gpu, Mapping):
            return None
        flight = gpu.get("in_flight")
        if isinstance(flight, bool) or not isinstance(flight, int):
            return None                     # a Galatea that cannot say: never taken as idle
        return gpu.get("yielding") is True and flight == 0

    return probe


__all__ = [
    "Abandoned", "GATE_PORT", "GateError", "GpuArbiter", "OllamaGate", "PETER_MODEL", "Placement",
    "client_gone", "guarded_evictor",
    "on_cpu", "peter_requirement", "refusal", "target_path", "upstream_of", "voice_probe",
]
