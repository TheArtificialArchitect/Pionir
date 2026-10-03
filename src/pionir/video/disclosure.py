"""The disclosure every video carries, and the credits it owes its sources.

One definition, used by the renderer (burned into the end card), the description and the
page, so the three can never disagree about what the viewer was told. The wording is a
statement of fact about how the video is made, not a hedge: a viewer who is told plainly
trusts the channel more than one who finds out.
"""
from __future__ import annotations

from .passages import ImageRef, Passage

DISCLOSURE = (
    "This video was made with AI. A local AI model wrote the script from the sources listed, "
    "software checked every line against them, the voice is synthetic, and the visuals are "
    "drawn by software. A person approves each video before it is published."
)


def credit_lines(passages: list[Passage] | tuple[Passage, ...],
                 images: list[ImageRef] | tuple[ImageRef, ...]) -> list[str]:
    """One line per cited passage and per image used, each with where it came from."""
    out = [f"{p.title}: {p.credit}. {p.url}" for p in passages]
    out += [f"Image: {i.credit}. {i.url}" for i in images]
    return out


def description(summary: str, passages, images, series: str) -> str:
    """The YouTube description: the summary, the sources, then the disclosure line."""
    parts = [summary.strip(), "", f"Series: {series}", "", "Sources and credits:"]
    parts += [f"- {line}" for line in credit_lines(passages, images)]
    parts += ["", DISCLOSURE]
    return "\n".join(parts)
