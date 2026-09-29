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
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .cortex import Cortex

_log = logging.getLogger(__name__)

# Below this many un-consolidated turns, leave them in the window - there is not
# enough yet to be worth a summary, and they are still recalled as messages.
DEFAULT_MIN_TURNS = 6
# The server folds a namespace on its own traffic once this many raw turns wait
# (two per exchange of the voice's, so every six exchanges).
AUTO_CONSOLIDATE_AT = 12
# One fold sends at most this many tokens of turns (about 4 characters a token),
# oldest first; a backlog bigger than that is folded a chunk at a time. Sending the
# whole backlog could outgrow the distil model's context and then never succeed.
CHUNK_TOKENS = 1500
_CHARS_PER_TOKEN = 4
# A chunk that failed is not retried until this backoff has passed, doubling with
# each failure of the same chunk up to the cap; after POISON_AFTER failures its
# turns are poisoned (retired, logged, alarmed) instead of being retried forever.
BACKOFF_FIRST_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 6 * 3600.0
POISON_AFTER = 4


class DistillUnavailable(Exception):
    """The distil model could not be reached or did not answer in time (connection
    refused, timeout, HTTP 5xx). Transient: the chunk backs off and is retried,
    with a capped delay and a doctor alarm while it lasts - but it is never
    poisoned, since nothing is wrong with the turns themselves."""


def is_transient(error: BaseException) -> bool:
    """A failure of the model's availability, not of the turns or the output."""
    if isinstance(error, DistillUnavailable):
        return True
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


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

    def __init__(self, cortex: Cortex, distiller: Distiller, *,
                 chunk_tokens: int = CHUNK_TOKENS,
                 clock: Callable[[], float] = time.time) -> None:
        self.cortex = cortex
        self.distiller = distiller
        self.chunk_tokens = chunk_tokens
        self._clock = clock

    @staticmethod
    def backoff_seconds(failures: int) -> float:
        """How long after its last failure a chunk that failed ``failures`` times waits."""
        if failures <= 0:
            return 0.0
        return min(BACKOFF_MAX_SECONDS, BACKOFF_FIRST_SECONDS * 2 ** (failures - 1))

    def _chunk(self, turns: list) -> list:
        """The oldest turns that fit the token budget - always at least one."""
        budget = self.chunk_tokens * _CHARS_PER_TOKEN
        chunk, used = [], 0
        for m in turns:
            if chunk and used + len(m.text) + 1 > budget:
                break
            chunk.append(m)
            used += len(m.text) + 1
        return chunk

    def due(self, namespace: str, *, min_turns: int = DEFAULT_MIN_TURNS) -> bool:
        """Whether a fold would be attempted now: enough turns wait, and their
        oldest chunk is not backing off after a failure. Cheap - no model."""
        turns = self.cortex.memories(namespace, kind="message", limit=None)
        if len(turns) < min_turns:
            return False
        failures, last_at, _bad = self.cortex.chunk_failures(namespace, self._chunk(turns)[0].id)
        return not (failures and last_at is not None
                    and self._clock() < last_at + self.backoff_seconds(failures))

    def consolidate(
        self, namespace: str, *, min_turns: int = DEFAULT_MIN_TURNS
    ) -> Consolidation | None:
        """Fold the OLDEST chunk of a namespace's waiting turns (bounded by
        ``chunk_tokens``) into one episode + facts. None when there is not enough
        to fold, the chunk is backing off after a failure, or the distiller
        declined or failed (logged, recorded, turns kept; poisoned after
        POISON_AFTER failures of the same chunk). Call again for the next chunk."""
        turns = self.cortex.memories(namespace, kind="message", limit=None)
        if len(turns) < min_turns:
            return None
        chunk = self._chunk(turns)
        chunk_start = chunk[0].id
        failures, last_at, bad = self.cortex.chunk_failures(namespace, chunk_start)
        if failures and last_at is not None \
                and self._clock() < last_at + self.backoff_seconds(failures):
            return None
        texts = [m.text for m in chunk]
        try:
            distilled = self.distiller.distill(texts)
        except Exception as error:  # noqa: BLE001 - distilling is fail-open by contract
            # Fail-open, not fail-silent: logged and written to the attempt log.
            why = f"{type(error).__name__}: {error}"
            if is_transient(error):
                # The model is away (timeout, refused, 5xx): back off, alarm while it
                # lasts, never poison - the turns are fine.
                _log.warning("consolidating %s: the distil model is unavailable (%s); "
                             "turns kept, backing off", namespace, why)
                self.cortex.note_consolidation(namespace, "unavailable", detail=why,
                                               chunk_start=chunk_start)
                return None
            _log.warning("consolidating %s: the distiller failed (%s); turns kept", namespace, why)
            self._failed(namespace, chunk, "failed", why, bad + 1)
            return None
        # Unusable output (not the JSON asked for): a non-transient failure of this
        # chunk - retried with backoff, poisoned after POISON_AFTER.
        if distilled is None:
            reason = getattr(self.distiller, "last_error", None) or "no usable output"
            _log.warning("consolidating %s: the distiller declined (%s); turns kept",
                         namespace, reason)
            self._failed(namespace, chunk, "declined", reason, bad + 1)
            return None
        # An EMPTY summary is the distiller's valid answer "nothing worth keeping"
        # (the prompt asks for exactly that): a successful fold with no episode.
        # The turns are marked folded (and deleted at retention) - not a failure,
        # so it neither backs off the next chunk nor ever poisons this one.
        if not distilled.summary.strip():
            self.cortex.fold_nothing(namespace, [m.id for m in chunk])
            self.cortex.note_consolidation(namespace, "empty", folded=len(chunk),
                                           detail="nothing worth keeping",
                                           chunk_start=chunk_start)
            return Consolidation(0, (), len(chunk))

        # One transaction: the episode and facts are written and the raw turns
        # retired (soft-deleted, still recoverable) together, or not at all. An
        # episode inserted and then N separate forget() commits could be cut off
        # halfway, leaving the summary AND its turns both live in recall.
        episode_id, fact_ids = self.cortex.fold(
            namespace,
            [m.id for m in chunk],
            distilled.summary.strip(),
            [fact for fact in distilled.facts if fact.strip()],
            meta={"folded_turns": len(chunk),
                  "distiller": str(getattr(self.distiller, "model", "") or "")},
        )
        self.cortex.note_consolidation(
            namespace, "folded", episode_id=episode_id, folded=len(chunk),
            detail=f"{len(fact_ids)} facts", chunk_start=chunk_start,
        )
        return Consolidation(episode_id, tuple(fact_ids), len(chunk))

    def _failed(self, namespace: str, chunk: list, outcome: str, why: str,
                failures: int) -> None:
        self.cortex.note_consolidation(namespace, outcome, detail=why,
                                       chunk_start=chunk[0].id)
        if failures >= POISON_AFTER:
            ids = [m.id for m in chunk]
            self.cortex.poison(namespace, ids, f"{failures} failed folds: {why}")
            self.cortex.note_consolidation(
                namespace, "poisoned", folded=len(ids), chunk_start=chunk[0].id,
                detail=f"{len(ids)} turns after {failures} failed folds: {why}")
            _log.error("consolidating %s: chunk of %d turns from #%d failed %d times; "
                       "poisoned (retired, not retried): %s",
                       namespace, len(ids), chunk[0].id, failures, why)


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
        opener=None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # Why the last distill declined, for the Consolidator's attempt log.
        self.last_error: str | None = None

    @property
    def model(self) -> str:
        return self._model

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
                # num_gpu 0: Ollama places no layer on the card, so a fold can never
                # evict or crowd the voice's resident model. The lease Pionir takes
                # for it is bookkeeping; this is what actually keeps it off the GPU.
                "options": {"temperature": 0.2, "num_gpu": 0},
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
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            self.last_error = f"{self._model}: {type(error).__name__}: {error}"[:300]
            if is_transient(error):
                raise DistillUnavailable(self.last_error) from error
            return None  # a 4xx: the request itself was refused - not transient
        try:
            document = json.loads(raw.decode("utf-8"))
            content = document["message"]["content"]
            parsed = json.loads(content)
        except (ValueError, KeyError, TypeError) as error:
            self.last_error = f"{self._model}: {type(error).__name__}: {error}"[:300]
            return None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str):
            self.last_error = f"{self._model}: reply was not {{summary, facts}} JSON"
            return None
        self.last_error = None
        summary = parsed.get("summary")
        facts = parsed.get("facts", [])
        if not isinstance(facts, list):
            facts = []
        return Distilled(
            summary=summary,
            facts=tuple(f for f in facts if isinstance(f, str) and f.strip()),
        )
