"""Authenticated adapter for Theo's full-memory, no-tools voice endpoint.

Ian decided on 2026-08-30 that Theo is Pionir's voice, and the Theo pane built
`/voice/chat` for it. The three Theo endpoints are not interchangeable and the
choice between them is the whole of this adapter's design:

* `/peer/chat` opens each turn with "the current speaker is another local
  synthetic agent - not Ian", carries no briefing and no human model, and pins
  the peer name to `Atani`. Ian reaching Theo through it meets someone who
  thinks he is talking to Atani and has never met him. Atani itself still uses
  it, correctly, because Atani *is* a peer.
* `/chat/send` is full Theo including `execute_task`, which reaches Melete and
  Daedalus outside Pionir's permission gates and outside its audit ledger. It
  was offered to Ian and declined for exactly that reason.
* `/voice/chat` is the one this uses: the ordinary turn - persona, self spine,
  continuity briefing, recall, mood, the human model - with an empty tool list.
  Full memory, zero hands, and action authorization stays with Pionir.

The source runs it through the same code path as an ordinary turn with
`no_tools=True` rather than a parallel implementation, so there is exactly one
place the property can be broken, and a source-side selftest asserts the model
is offered no tools while asserting that the ordinary path still is.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

from pionir.contracts import AgentManifest, Capability, ModelRequirement, Task, TaskResult
from pionir.errors import (
    AdapterAuthenticationError,
    AdapterProtocolError,
    AdapterUnavailable,
)
from pionir.scheduler import kv_cache_vram_mb

MAX_RESPONSE_BYTES = 1_000_000

# The source caps content at this length and answers 400 above it. Checked here
# so an over-long request fails with Pionir's own error instead of a bare HTTP
# code from the far side of the bridge.
MAX_CONTENT_CHARS = 32_000


class JsonTransport(Protocol):
    def request(
        self,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TheoSettings:
    """Connection details for Tech-Support's authenticated local bridge."""

    base_url: str = "http://127.0.0.1:8765"
    token: str = field(default="", repr=False)
    # A voice turn is 5-40 seconds of real work on the 3060. This is generous
    # against that rather than tuned to it, because the turn does recall and a
    # cold model load lands inside the same request.
    timeout_seconds: int = 180
    # A last resort, used only when Theo is unreachable - and if he is
    # unreachable no turn can happen anyway, so its exact value barely matters.
    # The real one comes from `/health`, which reports the model the live client
    # actually resolved. Never read OLLAMA_MODEL: the source's own history is
    # that a persisted env var outvoting the launcher is how retrains v10 to v16
    # landed in Ollama and were never served, and a stale process-scope copy of
    # it was observed here on 2026-09-04 reading v17 while v25 was promoted.
    model_id: str = "theo-local-v25-q4:latest"
    # Measured resident on the target card: 4423 MB at 4096 context, and 4940 MB
    # in live use at its actual 16384 window (2026-08-30). Declared as 4500 plus
    # a computed KV cache, which totals 5190 and so stays above what was
    # observed. See docs/PHASE0_BENCHMARK.md.
    estimated_model_vram_mb: int = 4_500
    # Confirmed 16384, from two independent directions rather than assumed.
    # The bridge requests it: THEO_NUM_CTX is unset at both User and Machine
    # scope, and the default is hardcoded 16384 in agent/llm/ollama_client.py,
    # which is what reaches Ollama's options. And the daemon honours it: this
    # model measured 4423 MB resident at num_ctx 4096 and 4792 MB at 16384, so
    # the KV cache actually grew with the request. That second half matters,
    # because a requested context is not always a served one - moondream showed
    # no change at all across the same pair, its window capped below what was
    # asked for. See docs/PHASE0_BENCHMARK.md.
    context_length: int = 16_384

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Theo's voice bridge must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Theo's bridge URL cannot contain credentials or query data")
        if not self.token.strip():
            raise ValueError("Theo's bridge token is required")
        if self.timeout_seconds < 10:
            raise ValueError("Theo's timeout must be at least 10 seconds")


class AuthenticatedLoopbackTransport:
    """Small stdlib JSON client that deliberately ignores proxy configuration."""

    def __init__(self, settings: TheoSettings) -> None:
        self._base_url = settings.base_url.rstrip("/")
        self._token = settings.token
        self._timeout = settings.timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @staticmethod
    def _reason(error: urllib.error.HTTPError) -> str:
        """The far side's own explanation, when it sent one.

        `/voice/chat` answers a failure with a non-200 carrying
        ``{"ok": false, "error": "..."}``. Reporting only the status code would
        throw that away and leave a bare number where a diagnosis was offered.
        """

        try:
            document = json.loads(error.read(MAX_RESPONSE_BYTES).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        reason = document.get("error") if isinstance(document, dict) else None
        return f": {reason}" if isinstance(reason, str) and reason.strip() else ""

    def request(
        self,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise AdapterAuthenticationError(
                    f"Theo rejected Pionir's bridge token ({error.code})"
                ) from error
            raise AdapterProtocolError(
                f"Theo answered HTTP {error.code}{self._reason(error)}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AdapterUnavailable(
                f"Theo's bridge is unavailable at {self._base_url}"
            ) from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise AdapterProtocolError("Theo's bridge response exceeded the size limit")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterProtocolError("Theo's bridge returned invalid JSON") from error
        if not isinstance(document, dict):
            raise AdapterProtocolError("Theo's bridge returned a non-object response")
        return document


def resolve_served_model(settings: TheoSettings) -> str | None:
    """Ask Theo which brain he is actually serving, or None if he cannot say.

    This id drives Pionir's VRAM admission discount. Hardcoding it meant that
    every promotion silently stopped it matching, so Pionir charged full weights
    instead of a KV cache and began refusing turns that would have fitted - a
    correct-looking ResourceUnavailable naming a real shortfall, with nothing
    anywhere saying the constant had gone stale.

    Failure is not an error. Theo being down is the ordinary case at boot, and
    the declared fallback covers it; `pionir doctor` shows the declaration
    against what the daemon actually holds either way.
    """

    try:
        document = AuthenticatedLoopbackTransport(settings).request("/health")
    except (AdapterUnavailable, AdapterProtocolError, AdapterAuthenticationError):
        return None
    model = document.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


class TheoAdapter:
    """Theo as Pionir's conversational voice: full memory, no tools."""

    def __init__(
        self,
        settings: TheoSettings,
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport or AuthenticatedLoopbackTransport(settings)
        self._manifest = AgentManifest(
            agent_id="theo",
            version="tech-support/machine-learning",
            capabilities=(
                Capability(
                    name="conversation.theo_reply",
                    description="Theo speaking - Pionir's conversational voice",
                    model=ModelRequirement(
                        model_id=settings.model_id,
                        estimated_vram_mb=settings.estimated_model_vram_mb,
                        context_vram_mb=kv_cache_vram_mb(settings.context_length),
                    ),
                    routing_hints=frozenset(
                        {"theo", "talk", "chat", "conversation", "reply", "speak"}
                    ),
                    priority=100,
                ),
            ),
        )

    @property
    def manifest(self) -> AgentManifest:
        return self._manifest

    def status(self) -> Mapping[str, Any]:
        """Probe the capability this adapter actually calls.

        `voice_chat`, not `peer` and not `voice` - the source flags Piper's
        text-to-speech as `voice`, so probing that would report health for a
        different subsystem entirely. A check that proves something is
        listening proves nothing about the thing being used.
        """

        document = self._transport.request("/health")
        capabilities = document.get("capabilities")
        if document.get("ok") is not True or not isinstance(capabilities, dict):
            raise AdapterProtocolError("Theo's health response is malformed")
        if capabilities.get("voice_chat") is not True:
            raise AdapterProtocolError(
                "Theo is running without the voice_chat capability; his backend "
                "is up but the full-memory no-tools path is not attached"
            )
        return document

    def execute(self, task: Task) -> TaskResult:
        content = str(task.payload.get("content") or "").strip()
        if not content:
            raise AdapterProtocolError("conversation content is required")
        if len(content) > MAX_CONTENT_CHARS:
            raise AdapterProtocolError(
                f"conversation content exceeds Theo's {MAX_CONTENT_CHARS}-character limit"
            )
        # An empty conversation id asks Theo to open a thread and tell us which
        # one. Inventing an id here would 404: the source requires a thread that
        # already exists in his own store, and continuity across turns is most
        # of why this endpoint was built rather than the peer one.
        conversation_id = str(task.payload.get("conversation_id") or "").strip()

        self.status()
        document = self._transport.request(
            "/voice/chat",
            payload={"conv": conversation_id, "content": content},
        )
        if document.get("ok") is not True:
            raise AdapterProtocolError(str(document.get("error") or "Theo did not answer"))
        message = document.get("message")
        if not isinstance(message, dict):
            raise AdapterProtocolError("Theo's voice reply carried no message record")
        reply = message.get("content")
        if not isinstance(reply, str) or not reply.strip():
            raise AdapterProtocolError("Theo returned an empty voice reply")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output={
                "reply": reply.strip(),
                # Theo's, not ours. Returned so the caller can continue the
                # thread rather than starting a new one on every turn.
                "conversation_id": str(document.get("conv") or conversation_id),
            },
            evidence=("theo:authenticated-loopback", "theo:voice-full-memory-no-tools"),
        )
