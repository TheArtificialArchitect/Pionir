"""Adapter for Galatea, Pionir's conversational voice.

Ian decided on 2026-09-10 that Galatea is Pionir's voice, replacing Theo (who
was removed on 2026-09-07). Galatea is the "Brain": she has emotion, opinion and
will, forms intent, and speaks - deliberately the opposite of Atani the Manager,
who must not. Her own docs (``Galatea/docs/DESIGN.md``) put it plainly: "Galatea
is the Voice of Pionir."

The seam is not Theo's. Theo answered a turn synchronously on ``/voice/chat``;
Galatea's is **asynchronous**, and that asymmetry is the whole of this adapter's
design:

* ``POST /api/send {"text": ...}`` only *queues* the message. It returns the id
  of the stored ``ian`` turn and nothing else - her reply is generated on a
  background thread while she appraises, recalls, drafts candidates and picks
  one. There is no reply in the response body to read.
* Her reply is read by **polling** ``GET /api/messages?after=<id>``. The rows are
  her own store, ascending by id. A turn's reply is the run of ``role == "her"``
  rows with ``initiated`` false that land after the id we sent. ``initiated``
  true is her speaking first - a rumination, not an answer to us - and must not
  be mistaken for the reply. She emits one or more bubbles per turn, paced
  like typing (observed up to ~7s+ apart), so the adapter collects them while
  ``GET /api/state`` reports ``typing`` true and returns only once she has
  stopped typing and a grace window has passed with no new bubble. A fixed
  quiet window alone (the first version, 3s) cut multi-bubble replies short.

Continuity needs no conversation id. Galatea is one persistent being with one
ongoing relationship in one SQLite store; unlike Theo there are no threads to
open or carry. She is not loopback-only: she runs with ``--phone`` bound on
0.0.0.0 so her phone page can reach her. Pionir, though, only ever dials
127.0.0.1, which her server authorises without a token, so - unlike Theo's
bridge - this adapter carries no credential, and it refuses any non-loopback
base url so it can never be pointed at her over the LAN.

Action authorisation stays with Pionir. This capability is conversation only:
Galatea speaks, and any doing she wants done is a separate intent handed to the
Manager through Pionir's gates, which is not this change.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

from pionir.contracts import AgentManifest, Capability, ModelRequirement, Task, TaskResult
from pionir.errors import AdapterProtocolError, AdapterUnavailable

MAX_RESPONSE_BYTES = 4_000_000

# Galatea trims a message to 4000 characters and answers 400 to an empty one.
# Checked here so an over-long request fails with Pionir's own error rather than
# being silently truncated on the far side into a different question.
MAX_CONTENT_CHARS = 4_000


class JsonTransport(Protocol):
    def get(self, path: str) -> Mapping[str, Any]: ...

    def post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class GalateaSettings:
    """Connection details for Galatea's loopback HTTP server."""

    base_url: str = "http://127.0.0.1:8799"
    # A whole turn: send, then wait for her to appraise, recall, draft and pick.
    # Generous against a cold model load and a busy card rather than tuned to a
    # measured mean, because the tail is what strands a turn, not the median.
    # This is the deadline checked between polls, never a socket timeout.
    timeout_seconds: int = 240
    # One HTTP request: a send, a poll, a state read. Her server answers these
    # in milliseconds; a hung socket should fail fast, not eat the whole turn.
    request_timeout_seconds: int = 10
    # How often to poll for new bubbles. Her page polls several times a second;
    # once a second is ample for a single turn and keeps the loop cheap.
    poll_interval_seconds: float = 1.0
    # After her last bubble, once she is no longer typing, keep polling until
    # this long passes with no new bubble. She answers in more than one bubble,
    # up to ~7s+ apart, and returning on the first would hand back a sentence
    # fragment and call it her reply. The typing flag carries the gaps; the
    # grace covers the moment between her last bubble and the flag clearing.
    reply_grace_seconds: float = 6.0
    # A last resort, used only when Galatea is unreachable - and if she is
    # unreachable no turn can happen anyway. The real one comes from
    # /api/settings, which reports the model her client actually resolved. Her
    # own config resolves file, then env, then this default, and her README is
    # explicit that the file wins over the environment; never read GALATEA_MODEL.
    # It is observability only: her turn declares no VRAM (see the manifest), so
    # this id never drives an admission decision - it only lets doctor name the
    # brain she is on. She has been seen on gemma3:12b as well as this.
    model_id: str = "qwen2.5:7b-instruct"

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Galatea's voice server must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Galatea's URL cannot contain credentials or query data")
        if self.timeout_seconds < 30:
            raise ValueError("Galatea's timeout must be at least 30 seconds")
        if self.request_timeout_seconds < 1:
            raise ValueError("Galatea's per-request timeout must be at least 1 second")
        if self.poll_interval_seconds <= 0:
            raise ValueError("Galatea's poll interval must be positive")
        if self.reply_grace_seconds < 0:
            raise ValueError("Galatea's reply grace cannot be negative")


class LoopbackTransport:
    """Small stdlib JSON client that deliberately ignores proxy configuration."""

    def __init__(self, settings: GalateaSettings) -> None:
        self._base_url = settings.base_url.rstrip("/")
        # Per request, not per turn: the turn's deadline lives in the poll loop.
        self._timeout = settings.request_timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _reason(error: urllib.error.HTTPError) -> str:
        """Her own explanation of a failure, when she sent one.

        Her endpoints answer a refusal with a non-2xx carrying
        ``{"error": "..."}`` - "empty", "she's still choosing a name", "bad
        json". Reporting only the status code would throw that away and leave a
        bare number where a diagnosis was offered.
        """

        try:
            document = json.loads(error.read(MAX_RESPONSE_BYTES).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        reason = document.get("error") if isinstance(document, dict) else None
        return f": {reason}" if isinstance(reason, str) and reason.strip() else ""

    def _open(self, request: urllib.request.Request) -> Mapping[str, Any]:
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise AdapterProtocolError(
                f"Galatea answered HTTP {error.code}{self._reason(error)}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AdapterUnavailable(
                f"Galatea's server is unavailable at {self._base_url}"
            ) from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterProtocolError("Galatea's response exceeded the size limit")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterProtocolError("Galatea returned invalid JSON") from error
        if not isinstance(document, dict):
            raise AdapterProtocolError("Galatea returned a non-object response")
        return document

    def get(self, path: str) -> Mapping[str, Any]:
        return self._open(urllib.request.Request(f"{self._base_url}{path}", method="GET"))

    def post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._open(request)


def resolve_served_model(settings: GalateaSettings) -> str | None:
    """Ask Galatea which model she is actually serving, or None if she cannot say.

    This id drives Pionir's VRAM admission discount. Hardcoding it meant that
    every promotion silently stopped it matching, so Pionir charged full weights
    instead of a KV cache and began refusing turns that would have fitted - the
    exact stale-constant failure the Theo adapter carried a scar for.

    Failure is not an error. Galatea being down is the ordinary case at boot,
    and the declared fallback covers it; ``pionir doctor`` shows the declaration
    against what she reports either way.
    """

    try:
        document = LoopbackTransport(settings).get("/api/settings")
    except (AdapterUnavailable, AdapterProtocolError):
        return None
    model = document.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


class GalateaAdapter:
    """Galatea as Pionir's conversational voice: full memory, no tools, async."""

    def __init__(
        self,
        settings: GalateaSettings | None = None,
        *,
        transport: JsonTransport | None = None,
        sleep: Any = time.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.settings = settings or GalateaSettings()
        self._transport = transport or LoopbackTransport(self.settings)
        self._sleep = sleep
        self._monotonic = monotonic
        self._manifest = AgentManifest(
            agent_id="galatea",
            version="voice/psyche-memory",
            capabilities=(
                Capability(
                    name="conversation.galatea_reply",
                    description="Galatea speaking - Pionir's conversational voice",
                    # Galatea hosts her own model in her own process and holds it
                    # resident with a KV cache already allocated for her fixed
                    # context, so a turn Pionir routes to her loads nothing and
                    # adds nothing to the card. Declaring weights or a KV cache
                    # here made the voice - which must always answer - refuse a
                    # turn that costs the card nothing: seen 2026-09-10, a routed
                    # turn rejected with "gemma3:12b needs 345 MB, 253 MB free"
                    # while her model was already resident and the turn added
                    # zero. So the marginal cost is zero and, like the elastic
                    # depth tier, she needs no GPU lease from Pionir - she already
                    # holds the card. Her real footprint is not lost: it is in
                    # the observed free VRAM that prices any Pionir-direct load
                    # running alongside her. The model id stays for observability.
                    model=ModelRequirement(
                        model_id=self.settings.model_id,
                        estimated_vram_mb=0,
                        context_vram_mb=0,
                        requires_gpu=False,
                    ),
                    # "chat" is Galatea's alone now: Atani deliberately dropped it
                    # so plain conversation reaches the voice, not the reasoner.
                    routing_hints=frozenset(
                        {"galatea", "talk", "chat", "conversation", "reply", "speak"}
                    ),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Confirm she is reachable and ready to be spoken to.

        Readiness is not that the port answers - it is that she has chosen a
        name. Before she has, ``/api/send`` answers 409 and no turn is possible,
        so probing that here turns a mid-turn 409 into an honest up-front
        unavailability. A check that proves something is listening proves
        nothing about the thing being used.
        """

        document = self._transport.get("/api/state")
        if document.get("naming") is True or document.get("name") in (None, ""):
            raise AdapterUnavailable(
                "Galatea has not chosen a name yet; she cannot hold a conversation"
            )
        return document

    def execute(self, task: Task) -> TaskResult:
        content = str(task.payload.get("content") or "").strip()
        if not content:
            raise AdapterProtocolError("conversation content is required")
        if len(content) > MAX_CONTENT_CHARS:
            raise AdapterProtocolError(
                f"conversation content exceeds Galatea's {MAX_CONTENT_CHARS}-character limit"
            )

        self.status()
        sent = self._transport.post("/api/send", {"text": content})
        if sent.get("ok") is not True or not isinstance(sent.get("id"), int):
            raise AdapterProtocolError(str(sent.get("error") or "Galatea did not accept the message"))
        sent_id = int(sent["id"])

        reply = self._await_reply(sent_id)
        if not reply:
            raise AdapterUnavailable(
                f"Galatea did not reply within {self.settings.timeout_seconds} seconds"
            )
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output={"reply": reply},
            evidence=("galatea:loopback", "galatea:voice-async-poll"),
        )

    def _typing(self) -> bool:
        """Whether she is mid-reply right now, per ``/api/state``.

        Read fail-open: a malformed or missing flag counts as not typing, so a
        state read that breaks can only shorten the wait to the grace window,
        never hold a turn open until the deadline.
        """

        try:
            return self._transport.get("/api/state").get("typing") is True
        except (AdapterUnavailable, AdapterProtocolError):
            return False

    def _await_reply(self, sent_id: int) -> str:
        """Collect her reply bubbles by polling, or return "" if she never answers.

        A turn's reply is every ``her`` bubble with ``initiated`` false that lands
        after ``sent_id``. She answers in more than one, paced like typing, so
        once the first has landed the loop keeps going while ``/api/state``
        says she is still typing, and returns only when she is not and a full
        grace window has passed since her last bubble - the difference between
        her whole reply and its first fragment. An ``initiated`` bubble is her
        speaking first, not an answer, and is skipped so a rumination racing
        into the window is never read as the reply. The deadline is checked
        between polls; whatever has landed by then is returned.
        """

        deadline = self._monotonic() + self.settings.timeout_seconds
        after = sent_id
        bubbles: list[str] = []
        last_bubble_at: float | None = None
        first = True
        while self._monotonic() < deadline:
            if not first:
                self._sleep(self.settings.poll_interval_seconds)
            first = False
            document = self._transport.get(
                f"/api/messages?{urlencode({'after': after})}"
            )
            messages = document.get("messages")
            if not isinstance(messages, list):
                raise AdapterProtocolError("Galatea's messages response was malformed")
            for message in sorted(messages, key=lambda row: int(row.get("id", 0))):
                message_id = int(message.get("id", 0))
                if message_id <= after:
                    continue
                after = message_id
                if message.get("role") != "her" or message.get("initiated"):
                    continue
                text = str(message.get("text") or "").strip()
                if text:
                    bubbles.append(text)
                    last_bubble_at = self._monotonic()
            if (
                last_bubble_at is not None
                and self._monotonic() - last_bubble_at >= self.settings.reply_grace_seconds
                and not self._typing()
            ):
                break
        return "\n\n".join(bubbles)
