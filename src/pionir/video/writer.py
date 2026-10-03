"""The script writer: a local model on the CPU, whose output is checked before it is believed.

Never the Anthropic API and never the GPU. Ollama is told ``num_gpu: 0``, so no layer lands on
the card and Moss's resident model is not crowded out; that is why this stage takes no GPU
lease (the lease is bookkeeping for models that DO use the card, and a stage that cannot use
the card has nothing to lease). Tests inject a fake writer, so the suite never needs Ollama.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Protocol

from .niche import Niche
from .passages import Pack
from .script import Script, ScriptRejected, check_script

DEFAULT_MODEL = "nemotron-3.5-lightning:30b-a3b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
ATTEMPTS = 3

_SCHEMA = """Reply with ONE JSON object and nothing else:
{"title": str, "summary": str,
 "scenes": [{"type": "card" | "image" | "timeline", "heading": str,
             "image": "<image id>" (only for type "image"),
             "lines": [{"text": str, "sources": ["<passage id>", ...]}]}]}"""
_RUN_SCHEMA = """A scene of type "run" shows a command that was really run: give it
"run": "<run id>" (the id after "run-" in the passage id) and cite that run passage in at
least one of its lines. State only numbers the run's output or measurements contain."""

_RULES = """Rules, all checked by a program that rejects the whole script on one breach:
- Use ONLY facts written in the passages below. Cite the passage id(s) a line rests on.
- Every number (year, count, price) and every proper name in a line must appear in a passage
  that line cites. Do not round, convert, or add context from memory.
- A line that cites nothing must be a short connective ("Here is why that mattered."): at
  most 14 words, no number, no name, and at most one line in five. Mark it "connective": true.
- Plain spoken sentences, one idea each, written to be read aloud. No hype, no clickbait, no
  claims about being first, best or secret."""


class ScriptWriter(Protocol):
    def write(self, niche: Niche, pack: Pack, feedback: list[str]) -> object: ...


def build_prompt(niche: Niche, pack: Pack, feedback: list[str]) -> str:
    parts = [f"You write the narration for one video in the series {niche.series[0]!r}: "
             f"{niche.title}. Topic: {pack.topic}.",
             f"Target length {niche.length_minutes[0]}-{niche.length_minutes[1]} minutes when "
             f"read aloud (about 150 words a minute), as scenes of 3-8 lines.",
             _SCHEMA, _RULES, "PASSAGES:"]
    if "run" in niche.scene_mix and pack.runs:
        parts.insert(-1, _RUN_SCHEMA)
    for p in pack.all_passages:
        parts.append(f"[{p.id}] ({p.source_id}) {p.title}\n{p.text}")
    if pack.images:
        parts.append("IMAGES you may use for an image scene (id: credit): " + "; ".join(
            f"{i.id}: {i.credit}" for i in pack.images))
    if feedback:
        parts.append("YOUR LAST ATTEMPT WAS REJECTED. Fix every one of these:\n- "
                     + "\n- ".join(feedback[:12]))
    return "\n\n".join(parts)


class OllamaWriter:
    def __init__(self, model: str | None = None, base_url: str | None = None, *,
                 timeout_seconds: float = 1800.0, opener=None) -> None:
        self.model = model or os.environ.get("PIONIR_VIDEO_MODEL") or DEFAULT_MODEL
        self._base = (base_url or os.environ.get("PIONIR_OLLAMA_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout_seconds
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def write(self, niche: Niche, pack: Pack, feedback: list[str]) -> object:
        body = json.dumps({
            "model": self.model, "stream": False, "format": "json",
            "messages": [{"role": "user", "content": build_prompt(niche, pack, feedback)}],
            "options": {"temperature": 0, "num_gpu": 0},
        }).encode("utf-8")
        request = urllib.request.Request(self._base + "/api/chat", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                reply = json.loads(response.read().decode("utf-8"))
            return json.loads(reply["message"]["content"])
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
            raise ScriptRejected([f"the local model gave no usable JSON: {error}"]) from error


def generate_script(writer: ScriptWriter, niche: Niche, pack: Pack, *,
                    attempts: int = ATTEMPTS,
                    on_attempt: Callable[[int, list[str]], None] | None = None) -> Script:
    """Ask the writer, check, and on a rejection ask again with the reasons; the last
    rejection stands. Nothing that failed a check is ever returned."""
    feedback: list[str] = []
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return check_script(writer.write(niche, pack, feedback), niche, pack)
        except ScriptRejected as rejected:
            feedback = rejected.problems
            if on_attempt:
                on_attempt(attempt, feedback)
            last = rejected
    raise last
