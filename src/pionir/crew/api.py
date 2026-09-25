"""The crew's loopback HTTP API: how Moss reads the crew and directs it, through Pionir.

Moss is a separate process and reaches everything through Pionir (``POST /api/task``).
Pionir's ``crew.*`` capabilities (``pionir.adapters.crew``) call THIS API, so every
direction change passes Pionir's gates and lands in its audit ledger like any other
action. The API is the Direction API (direction.py) over HTTP, nothing more:

    GET  /api/health                       is the crew up, paused, which divisions
    GET  /api/digest?max_chars=N           Direction.digest (bounded, most urgent first)
    GET  /api/divisions                    Direction.divisions
    GET  /api/compute                      Direction.compute
    POST /api/goal     {division, goal, priority?, by?}     Direction.set_goal
    POST /api/allocate {resource, shares, by?}              Direction.allocate

Every answer is a JSON object with ``ok``. Bad input is a 400 carrying the ValueError's
text, never a 500; a 500 is only ever a real fault, recorded as a lesion. There is no
money lever here either: ``allocate`` takes compute resources only, and the Direction
API refuses anything else.

Loopback only, three ways: the socket binds 127.0.0.1, a non-loopback peer is refused
anyway, and a request naming a non-local Host (DNS rebinding) is refused. A POST must be
declared JSON and carry at most ``MAX_BODY_BYTES``; a browser Origin must be local.

The validators (``parse_*``) are shared with Pionir's adapter so both sides refuse the
same malformed request with the same words.

Nothing here starts itself: the crew runtime starts the API in ``Crew.start`` and stops
it in ``Crew.stop``.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .direction import DIGEST_CHARS, MAX_GOAL_CHARS, _check_resource
from .log import lesion, log

DEFAULT_PORT = 8782
BIND_HOST = "127.0.0.1"
MAX_BODY_BYTES = 64 * 1024
MIN_DIGEST_CHARS = 200
MAX_DIGEST_CHARS = 20_000
MAX_BY_CHARS = 120
# Who asked, when a direct caller does not say. Pionir's adapter always says.
DEFAULT_BY = "crew-api"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

GET_ROUTES = ("/api/health", "/api/digest", "/api/divisions", "/api/compute")
POST_ROUTES = ("/api/goal", "/api/allocate")

Reply = tuple[int, dict]


# ---- validators, shared with pionir.adapters.crew -------------------------------
def parse_max_chars(raw: Any) -> int:
    """The digest's character budget: a whole number from MIN to MAX, default 4000."""
    if raw is None or raw == "":
        return DIGEST_CHARS
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if isinstance(raw, bool) or not isinstance(raw, int):
        # ValueError, not TypeError: every bad input is one kind of answer, a 400
        raise ValueError(f"max_chars is a whole number, not {raw!r}")  # noqa: TRY004
    if not MIN_DIGEST_CHARS <= raw <= MAX_DIGEST_CHARS:
        raise ValueError(f"max_chars is from {MIN_DIGEST_CHARS} to {MAX_DIGEST_CHARS}, "
                         f"not {raw}")
    return raw


def parse_by(raw: Any) -> str:
    """Who asked for a write: a short line of printable text."""
    if raw is None:
        return DEFAULT_BY
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("by names who asked, as text")
    by = raw.strip()
    if len(by) > MAX_BY_CHARS:
        raise ValueError(f"by is at most {MAX_BY_CHARS} characters")
    if not by.isprintable():
        raise ValueError("by must be printable text on one line")
    return by


def parse_goal(body: Mapping) -> dict:
    """``{division, goal, priority?, by?}`` -> the arguments of Direction.set_goal.
    Whether the division exists is the crew's to say (its registry)."""
    division = body.get("division")
    if not isinstance(division, str) or not division.strip():
        raise ValueError("division names one of the crew's divisions")
    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("a goal says something")
    if len(goal.strip()) > MAX_GOAL_CHARS:
        raise ValueError(f"a goal is at most {MAX_GOAL_CHARS} characters")
    priority = body.get("priority", 3)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
        raise ValueError("priority is a whole number from 1 (most) to 5 (least)")
    return {"division": division.strip(), "goal": goal.strip(), "priority": priority,
            "by": parse_by(body.get("by"))}


def parse_allocation(body: Mapping) -> dict:
    """``{resource, shares, by?}`` -> the arguments of Direction.allocate. Compute only:
    any other resource - money above all - is refused with the Direction API's words."""
    resource = body.get("resource")
    if not isinstance(resource, str) or not resource.strip():
        raise ValueError("resource names a compute resource")
    _check_resource(resource.strip())
    shares = body.get("shares")
    if not isinstance(shares, dict) or not shares:
        raise ValueError("shares is a non-empty {division: fraction} object")
    for division, share in shares.items():
        if not isinstance(division, str) or not division.strip():
            raise ValueError("every share is keyed by a division id")
        if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 <= share <= 1:
            raise ValueError(f"{division}'s share must be a fraction from 0 to 1, not {share!r}")
    return {"resource": resource.strip(), "shares": dict(shares), "by": parse_by(body.get("by"))}


def is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address((host or "").split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)


def _hostname(host_header: str | None) -> str | None:
    if host_header is None:
        return None
    host = host_header.strip().lower()
    if host.startswith("["):                       # [::1]:8782
        return host[1:host.find("]")] if "]" in host else None
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


# ---- the API ----------------------------------------------------------------------
class CrewApi:
    """The Direction API over loopback HTTP. ``handle`` is the whole API as a function of
    the request; the HTTP handler only moves bytes, so the rules are testable without a
    socket and the socket cannot add rules of its own."""

    def __init__(self, direction, *, health: Callable[[], dict],
                 port: int = DEFAULT_PORT, host: str = BIND_HOST) -> None:
        if not is_loopback(host):
            raise ValueError("the crew API binds loopback only")
        self.direction = direction
        self._health = health
        self._host = host
        self._port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        """The bound port while serving (the real one when configured as 0), else None."""
        return self._server.server_address[1] if self._server is not None else None

    @property
    def serving(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        """Bind and serve on a thread. A port already taken is a lesion, logged and
        counted, and the crew runs on without its API - it is never a silent success."""
        if self._server is not None:
            return True
        try:
            server = _Server((self._host, self._port), _make_handler(self))
        except OSError as exc:
            lesion("crew.api.bind", exc)
            log.error("crew API NOT serving: could not bind %s:%s (%s); Moss cannot reach "
                      "the crew until it restarts on a free port", self._host, self._port, exc)
            return False
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.2},
                                        name="pionir-crew-api", daemon=True)
        self._thread.start()
        log.info("crew API listening on http://%s:%s", self._host, self.port)
        return True

    def stop(self) -> None:
        server, thread = self._server, self._thread
        if server is None:
            return
        server.shutdown()                  # the serve loop ends ...
        server.server_close()              # ... and in-flight handlers are joined
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
        self._thread = None
        log.info("crew API stopped")

    # ---- the rules ---------------------------------------------------------------
    def handle(self, method: str, target: str, *, client_host: str,
               headers: Mapping[str, str] | None = None,
               read_body: Callable[[int], bytes] | None = None) -> Reply:
        """One request -> (status, JSON object). Never raises."""
        headers = headers or {}
        try:
            if not is_loopback(client_host):
                return 403, _err(f"the crew API answers loopback only, not {client_host}")
            host = _hostname(headers.get("Host"))
            if host is not None and host not in _LOCAL_HOSTS:
                return 403, _err("the Host header is not local")
            route = urlsplit(target)
            if route.path in GET_ROUTES:
                if method != "GET":
                    return 405, _err(f"{route.path} is read with GET")
                return self._get(route.path, parse_qs(route.query))
            if route.path in POST_ROUTES:
                if method != "POST":
                    return 405, _err(f"{route.path} is written with POST")
                body = _read_json(headers, read_body)
                if isinstance(body, tuple):
                    return body
                return self._post(route.path, body)
            return 404, _err(f"no such endpoint: {route.path}")
        except ValueError as exc:
            return 400, _err(str(exc))
        except Exception as exc:  # noqa: BLE001 - a real fault: counted, logged, answered
            lesion("crew.api", exc)
            return 500, _err(f"the crew failed internally ({type(exc).__name__}); "
                             "see crew.log")

    def _get(self, path: str, query: dict) -> Reply:
        if path == "/api/health":
            return 200, {"ok": True, **self._health()}
        if path == "/api/digest":
            raw = (query.get("max_chars") or [None])[-1]
            max_chars = parse_max_chars(raw)
            return 200, {"ok": True, "max_chars": max_chars,
                         **self.direction.digest(max_chars=max_chars)}
        if path == "/api/divisions":
            return 200, {"ok": True, "divisions": self.direction.divisions()}
        return 200, {"ok": True, "compute": self.direction.compute()}

    def _post(self, path: str, body: dict) -> Reply:
        if self._health().get("stopping"):
            return 503, _err("the crew is stopping; nothing was changed")
        if path == "/api/goal":
            args = parse_goal(body)
            row = self.direction.set_goal(args["division"], args["goal"],
                                          priority=args["priority"], by=args["by"])
            return 200, {"ok": True, "division": args["division"], "direction": row}
        args = parse_allocation(body)
        allocation = self.direction.allocate(args["resource"], args["shares"], by=args["by"])
        return 200, {"ok": True, "resource": args["resource"], "by": args["by"],
                     "allocation": allocation}


def _err(message: str) -> dict:
    return {"ok": False, "error": message}


def _read_json(headers: Mapping[str, str], read_body: Callable[[int], bytes] | None):
    """The POST body as a JSON object, or a (status, error) reply saying why not."""
    content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return 415, _err("a POST body is declared Content-Type: application/json")
    origin = headers.get("Origin")
    if origin is not None and _hostname(urlsplit(origin.strip()).netloc) not in _LOCAL_HOSTS:
        return 403, _err("a browser Origin must be local")
    try:
        length = int(headers.get("Content-Length") or 0)
    except ValueError:
        return 400, _err("Content-Length is not a number")
    if length <= 0:
        return 400, _err("a POST needs a JSON object body")
    if length > MAX_BODY_BYTES:
        return 413, _err(f"the body is over {MAX_BODY_BYTES} bytes")
    if read_body is None:
        return 400, _err("no body")
    raw = read_body(length)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, _err("the body is not valid UTF-8 JSON")
    if not isinstance(document, dict):
        return 400, _err("the body is a JSON object")
    return document


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a SECOND server bind a port already in use, and the
    # two then split the traffic. Never: take the port exclusively, or fail to bind.
    allow_reuse_address = False
    daemon_threads = False               # server_close() joins in-flight handlers
    block_on_close = True

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        super().server_bind()


def _make_handler(api: CrewApi):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PionirCrew/1"
        timeout = 10                     # a stalled client cannot hold a thread for long

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("crew api %s: " + fmt, self.client_address[0], *args)

        def _serve(self, method: str) -> None:
            # Nothing may escape into the server thread: every failure is a lesion and,
            # while the client is still there, a 500.
            try:
                status, payload = api.handle(method, self.path,
                                             client_host=self.client_address[0],
                                             headers=self.headers,
                                             read_body=self.rfile.read)
                self._reply(status, payload)
            except Exception as exc:  # noqa: BLE001
                lesion("crew.api.handler", exc)
                try:
                    self._reply(500, _err(f"the crew failed internally ({type(exc).__name__})"))
                except OSError as gone:
                    log.warning("crew api: could not answer a failed request: %s", gone)

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._serve("POST")

        def do_PUT(self) -> None:
            self._serve("PUT")

        def do_DELETE(self) -> None:
            self._serve("DELETE")

    return Handler
