"""The script stage's gate: a script is a list of lines, and a line stands on a passage or it is out.

The writer (a local model, see writer.py) proposes; this module disposes. It is deterministic
and model-free, and it fails closed:

* every line that states anything must cite at least one passage id from the pack, and every
  id must exist (an id the model invented is an unsourced claim, not a typo to guess at),
* every number a line writes - a year, a count, a price - must be written in a passage it
  cites, and every capitalised name must appear in what it cites (crew/grounding.py: the same
  checks that stop a division leader reporting revenue nobody recorded),
* the only lines allowed to cite nothing are short connectives ("Now, the second reason."),
  at most a fifth of the script, and they may contain no digit and no name,
* a ``run`` scene (a tutorial's terminal screen) must name a run in the pack, and that run must
  have succeeded: a command that was never run, or ran and failed or timed out, blocks the
  script, as does any failed run left in the pack, and a tutorial niche must show at least one,
* a title, heading or summary is shown to viewers and is checked like a line against all the
  passages the script cites.

One bad line rejects the whole script. Dropping the line quietly would hand Ian a video that
reads differently from what was checked, and rewriting it is the model's job, not ours.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..crew import grounding
from .niche import SCENE_TYPES, Niche
from .passages import Pack, Passage

MAX_LINE_CHARS = 420
MAX_CONNECTIVE_WORDS = 14
MAX_CONNECTIVE_SHARE = 0.2
MAX_SCENES = 60
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_DIGIT = re.compile(r"\d")
_FIELDS = {"title", "summary", "scenes"}
_SCENE_FIELDS = {"type", "heading", "image", "run", "lines"}
_LINE_FIELDS = {"text", "sources", "connective"}


class ScriptRejected(ValueError):
    """A script that may not go further; ``problems`` lists every reason, in words."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        shown = "; ".join(self.problems[:6])
        more = f" (+{len(self.problems) - 6} more)" if len(self.problems) > 6 else ""
        super().__init__(f"script rejected: {shown}{more}")


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    sources: tuple[str, ...]
    connective: bool


@dataclass(frozen=True, slots=True)
class Scene:
    type: str
    heading: str
    image: str | None
    lines: tuple[Line, ...]
    run: str | None = None


@dataclass(frozen=True, slots=True)
class Script:
    title: str
    summary: str
    scenes: tuple[Scene, ...]
    passages: tuple[Passage, ...]      # the ones cited, in first-cited order

    @property
    def narration(self) -> list[str]:
        return [line.text for scene in self.scenes for line in scene.lines]


def _tokens(texts: list[str]) -> set[str]:
    return {t for text in texts for t in _WORD.findall(text)}


def _numbers(text: str) -> set[float]:
    return grounding.values_in(text)


def _check_shown(label: str, text: str, cited: list[Passage], extra_known: set[str],
                 problems: list[str]) -> None:
    texts = [p.text for p in cited] + [p.title for p in cited]
    backed = set().union(*(_numbers(t) for t in texts)) if texts else set()
    for value in sorted(_numbers(text) - backed):
        problems.append(f"{label}: the number {value:g} is not in any passage this script cites")
    known = _tokens(texts) | extra_known
    names = grounding.unknown_names(text, known, grounding.vocabulary_words(texts))
    for name in dict.fromkeys(names):
        problems.append(f"{label}: the name {name!r} is not in any passage this script cites")


def check_script(raw: Any, niche: Niche, pack: Pack) -> Script:
    """The checked Script for what the model returned, or ScriptRejected."""
    problems: list[str] = []
    if not isinstance(raw, Mapping):
        raise ScriptRejected(["the script is not an object"])
    stray = set(raw) - _FIELDS
    if stray:
        problems.append(f"unknown script fields {sorted(stray)}")
    title, summary = raw.get("title"), raw.get("summary")
    for label, value in (("title", title), ("summary", summary)):
        if not isinstance(value, str) or not value.strip():
            problems.append(f"the {label} is missing")
    scenes_raw = raw.get("scenes")
    if not isinstance(scenes_raw, list) or not scenes_raw:
        raise ScriptRejected(problems + ["the script has no scenes"])
    if len(scenes_raw) > MAX_SCENES:
        problems.append(f"{len(scenes_raw)} scenes is more than the {MAX_SCENES} allowed")

    for failed in pack.runs:
        if not failed.ok:
            how = "timed out" if failed.timed_out else f"exited {failed.exit_code}"
            if not failed.timed_out and failed.exit_code == 0:
                how = "printed nothing"
            problems.append(f"run {failed.id!r} ({failed.command}) {how}: a script is not built "
                            "on a pack that holds a failed run")
    scenes: list[Scene] = []
    cited_order: list[str] = []
    line_checks: list[tuple[str, Line]] = []
    known_extra = _tokens([pack.topic, niche.title, *niche.series])
    for si, sraw in enumerate(scenes_raw[:MAX_SCENES], 1):
        where = f"scene {si}"
        if not isinstance(sraw, Mapping):
            problems.append(f"{where}: not an object")
            continue
        stray = set(sraw) - _SCENE_FIELDS
        if stray:
            problems.append(f"{where}: unknown fields {sorted(stray)}")
        kind = sraw.get("type")
        if kind not in SCENE_TYPES:
            problems.append(f"{where}: type {kind!r} is not one of {SCENE_TYPES}")
        heading = sraw.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            problems.append(f"{where}: no heading")
            heading = ""
        image = sraw.get("image")
        if kind == "image":
            if not isinstance(image, str) or pack.image(image) is None:
                problems.append(f"{where}: image {image!r} is not an image in the pack "
                                "(an image scene must name a credited image)")
                image = None
        elif image is not None:
            problems.append(f"{where}: only an image scene may name an image")
            image = None
        run_id = sraw.get("run")
        if kind == "run":
            if "run" not in niche.scene_mix:
                problems.append(f"{where}: niche {niche.id!r} does not use run scenes")
            run = pack.run(run_id) if isinstance(run_id, str) else None
            if run is None:
                problems.append(f"{where}: run {run_id!r} is not a run in the pack (a command "
                                "that was never run cannot be shown)")
                run_id = None
            elif not run.ok:
                problems.append(f"{where}: run {run_id!r} did not succeed, so it cannot be shown")
        elif run_id is not None:
            problems.append(f"{where}: only a run scene may name a run")
            run_id = None
        lines_raw = sraw.get("lines")
        if not isinstance(lines_raw, list) or not lines_raw:
            problems.append(f"{where}: no lines")
            continue
        lines: list[Line] = []
        for li, lraw in enumerate(lines_raw, 1):
            lwhere = f"{where} line {li}"
            if not isinstance(lraw, Mapping):
                problems.append(f"{lwhere}: not an object")
                continue
            stray = set(lraw) - _LINE_FIELDS
            if stray:
                problems.append(f"{lwhere}: unknown fields {sorted(stray)}")
            text = lraw.get("text")
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{lwhere}: no text")
                continue
            if len(text) > MAX_LINE_CHARS:
                problems.append(f"{lwhere}: longer than {MAX_LINE_CHARS} characters")
            ids = lraw.get("sources", [])
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                problems.append(f"{lwhere}: sources must be a list of passage ids")
                ids = []
            connective = lraw.get("connective", False)
            if not isinstance(connective, bool):
                problems.append(f"{lwhere}: connective must be true or false")
                connective = False
            line = Line(text.strip(), tuple(dict.fromkeys(ids)), connective)
            lines.append(line)
            line_checks.append((lwhere, line))
            for pid in line.sources:
                if pack.passage(pid) is None:
                    problems.append(f"{lwhere}: cites {pid!r}, which is not a passage in the pack")
                elif pid not in cited_order:
                    cited_order.append(pid)
        if kind == "run" and run_id and not any(
                pack.run(run_id).passage_id in line.sources for line in lines):
            problems.append(f"{where}: no line cites the run it shows ({pack.run(run_id).passage_id})")
        if isinstance(kind, str) and lines:
            scenes.append(Scene(kind, heading.strip(), image, tuple(lines),
                                run_id if kind == "run" else None))

    if niche.kind == "tutorial" and not any(s.type == "run" for s in scenes):
        problems.append("a tutorial must show at least one command that was run (no run scene)")
    connectives = 0
    for lwhere, line in line_checks:
        passages = [p for p in (pack.passage(i) for i in line.sources) if p is not None]
        if not line.sources:
            if not line.connective:
                problems.append(f"{lwhere}: states something but cites no source "
                                "(unsourced claim)")
                continue
            connectives += 1
            if len(line.text.split()) > MAX_CONNECTIVE_WORDS:
                problems.append(f"{lwhere}: a line with no source may be at most "
                                f"{MAX_CONNECTIVE_WORDS} words")
            if _DIGIT.search(line.text):
                problems.append(f"{lwhere}: a line with no source may not contain a number")
            for name in dict.fromkeys(grounding.unknown_names(line.text, known_extra)):
                problems.append(f"{lwhere}: a line with no source may not name {name!r}")
            continue
        if line.connective:
            problems.append(f"{lwhere}: marked connective but cites a source; pick one")
        if passages:
            _check_shown(lwhere, line.text, passages, known_extra, problems)
    if line_checks and connectives / len(line_checks) > MAX_CONNECTIVE_SHARE:
        problems.append(f"{connectives} of {len(line_checks)} lines cite nothing; "
                        f"at most {MAX_CONNECTIVE_SHARE:.0%} may")

    cited = [p for p in (pack.passage(i) for i in cited_order) if p is not None]
    if not cited:
        problems.append("the script cites no passage at all")
    if isinstance(title, str):
        _check_shown("title", title, cited, known_extra, problems)
    if isinstance(summary, str):
        _check_shown("summary", summary, cited, known_extra, problems)
    for si, scene in enumerate(scenes, 1):
        _check_shown(f"scene {si} heading", scene.heading, cited, known_extra, problems)
    if problems:
        raise ScriptRejected(problems)
    return Script(str(title).strip(), str(summary).strip(), tuple(scenes), tuple(cited))
