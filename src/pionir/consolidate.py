"""Consolidation: folding a conversation into memory as it scrolls out.

The window forgets; the store must not. As raw turns pile up in a namespace they
are folded into a durable **episode** (a summary) plus any **facts** worth
keeping, and the raw turns are retired from recall. That is what lets a
conversation from weeks ago come back without ever having sat in the window.

The engine stays model-free. Distilling a pile of turns into a summary needs a
model, and that lives in an injected `Distiller` - a fake in tests, an Ollama
client in production - so `cortex.py` never imports a model and this module can
be tested without one. Distilling is fail-open: if the model is down or returns
nothing usable, the turns are left as they are and tried again later, never lost.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, Sequence

from .cortex import Cortex

# Below this many un-consolidated turns, leave them in the window - there is not
# enough yet to be worth a summary, and they are still recalled as messages.
DEFAULT_MIN_TURNS = 6


@dataclass(frozen=True, slots=True)
class Distilled:
    """What a distiller returns: one summary of the turns, and any durable facts
    pulled from them. Empty summary means the distiller declined (fail-open)."""

    summary: str
    facts: tuple[str, ...] = ()


class Distiller(Protocol):
    def distill(self, turns: Sequence[str]) -> Distilled | None: ...


@dataclass(frozen=True, slots=True)
class Consolidation:
    episode_id: int
    fact_ids: tuple[int, ...]
    folded_turns: int


class Consolidator:
    """Folds a namespace's raw turns into an episode + facts, then retires them."""

    def __init__(self, cortex: Cortex, distiller: Distiller) -> None:
        self.cortex = cortex
        self.distiller = distiller

    def consolidate(
        self, namespace: str, *, min_turns: int = DEFAULT_MIN_TURNS
    ) -> Consolidation | None:
        turns = self.cortex.memories(namespace, kind="message")
        if len(turns) < min_turns:
            return None
        texts = [m.text for m in turns]
        try:
            distilled = self.distiller.distill(texts)
        except Exception:  # noqa: BLE001 - distilling is fail-open by contract
            return None
        # No usable summary: leave the turns untouched to try again later. Losing
        # them because the model hiccuped would be the opposite of the point.
        if distilled is None or not distilled.summary.strip():
            return None

        episode_id = self.cortex.remember(
            "episode",
            distilled.summary.strip(),
            namespace=namespace,
            meta={"folded_turns": len(turns)},
        )
        fact_ids = tuple(
            self.cortex.remember("fact", fact.strip(), namespace=namespace)
            for fact in distilled.facts
            if fact.strip()
        )
        # Retire the raw turns: out of recall, still recoverable (soft delete).
        for m in turns:
            self.cortex.forget(m.id)
        return Consolidation(episode_id, fact_ids, len(turns))


class OllamaDistiller:
    """Distills turns into a summary + facts via a local chat model, stdlib only.

    Any failure returns None, which the Consolidator treats as "not now" - the
    turns are kept and folded on a later pass. The model is asked for strict JSON
    and anything that is not parseable JSON of the right shape is a decline, not a
    guess.
    """

    _PROMPT = (
        "You are condensing a conversation into memory. Reply with ONLY a JSON "
        'object: {"summary": "<2-4 sentences, third person, what happened and '
        'what matters>", "facts": ["<durable fact>", ...]}. No prose outside the '
        "JSON. If there is nothing worth keeping, use an empty summary."
    )

    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        timeout_seconds: int = 120,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def distill(self, turns: Sequence[str]) -> Distilled | None:
        transcript = "\n".join(turns)
        body = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": self._PROMPT},
                    {"role": "user", "content": transcript},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                document = json.loads(response.read().decode("utf-8"))
            content = document["message"]["content"]
            parsed = json.loads(content)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        summary = parsed.get("summary")
        facts = parsed.get("facts", [])
        if not isinstance(summary, str):
            return None
        if not isinstance(facts, list):
            facts = []
        return Distilled(
            summary=summary,
            facts=tuple(f for f in facts if isinstance(f, str) and f.strip()),
        )
