"""Authenticated adapter for Theo's bounded, conversation-only peer endpoint."""

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


class JsonTransport(Protocol):
    def request(
        self,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class TheoPeerSettings:
    """Connection details for Tech-Support's authenticated local bridge."""

    base_url: str = "http://127.0.0.1:8765"
    token: str = field(default="", repr=False)
    timeout_seconds: int = 180
    model_id: str = "theo-local-v17-q4:latest"
    # Measured resident on the target card at 4096 context: 4423 MB. The margin
    # covers driver variance, not a guess. See docs/PHASE0_BENCHMARK.md.
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
            raise ValueError("Theo's peer bridge must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Theo's bridge URL cannot contain credentials or query data")
        if not self.token.strip():
            raise ValueError("Theo's bridge token is required")
        if self.timeout_seconds < 10:
            raise ValueError("Theo's timeout must be at least 10 seconds")


class AuthenticatedLoopbackTransport:
    """Small stdlib JSON client that deliberately ignores proxy configuration."""

    def __init__(self, settings: TheoPeerSettings) -> None:
        self._base_url = settings.base_url.rstrip("/")
        self._token = settings.token
        self._timeout = settings.timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

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
                f"Theo rejected the peer request with HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise AdapterUnavailable(
                f"Theo's peer bridge is unavailable at {self._base_url}"
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


class TheoPeerAdapter:
    """Expose Theo's existing Atani-only peer exchange as a Pionir capability.

    This adapter does not use Theo's ordinary ``/chat/send`` endpoint. The peer
    endpoint disables tools, recall, growth writes, transcript logging, and Ian's
    private-memory briefing in the Tech-Support runtime itself.
    """

    def __init__(
        self,
        settings: TheoPeerSettings,
        *,
        transport: JsonTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport or AuthenticatedLoopbackTransport(settings)
        self._manifest = AgentManifest(
            agent_id="theo-peer",
            version="tech-support/machine-learning",
            capabilities=(
                Capability(
                    name="conversation.theo_peer_reply",
                    description="A bounded, conversation-only Theo reply to Atani",
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
        document = self._transport.request("/health")
        capabilities = document.get("capabilities")
        if document.get("ok") is not True or not isinstance(capabilities, dict):
            raise AdapterProtocolError("Theo's health response is malformed")
        if capabilities.get("peer") is not True:
            raise AdapterProtocolError("Theo is running without the peer capability")
        return document

    def execute(self, task: Task) -> TaskResult:
        content = str(task.payload.get("content") or "").strip()
        if not content:
            raise AdapterProtocolError("conversation content is required")
        if len(content) > 8_000:
            raise AdapterProtocolError("conversation content exceeds Theo's 8000-character limit")
        conversation_id = str(
            task.payload.get("conversation_id") or f"atani:{task.task_id}"
        ).strip()

        self.status()
        document = self._transport.request(
            "/peer/chat",
            payload={
                "peer": "Atani",
                "conversation": conversation_id,
                "content": content,
            },
        )
        if document.get("ok") is not True:
            raise AdapterProtocolError(str(document.get("error") or "Theo did not answer"))
        reply = document.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            raise AdapterProtocolError("Theo returned an empty peer reply")
        return TaskResult(
            task_id=task.task_id,
            agent_id=self.manifest.agent_id,
            output={
                "reply": reply.strip(),
                "conversation_id": conversation_id,
            },
            evidence=("theo:authenticated-loopback", "theo:conversation-only-peer"),
        )
