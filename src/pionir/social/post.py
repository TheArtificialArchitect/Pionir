"""The checked shape of an Instagram post - Pionir's refusal list, run before a post is parked
for the owner and again before it is published. The crew runs it first too (and adds its own
names / business-figure / internal-name rules on top), so a draft that passes the crew can
never be refused here.

Instagram links are not clickable in captions, so a post carries NO links, URLs or domains at
all; the bio link does that job. No @ anywhere (no mentions: a post names nobody). Hashtags go
in their own field, lower case, and are appended by the publisher.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pionir.adapters.content import _contact_problem, _scrooge_problem
from pionir.social.card import CardTooLong, layout

POST_FIELDS = frozenset({"draft_id", "headline", "points", "caption", "hashtags"})
_DRAFT_ID = re.compile(r"[a-z0-9-]{1,64}")
_HASHTAG = re.compile(r"[a-z0-9_]{2,30}")
HEADLINE = (10, 90)
POINT = (10, 110)
MAX_POINTS = 3
CAPTION = (50, 1800)
MAX_HASHTAGS = 10
INSTAGRAM_CAPTION_LIMIT = 2200

# Printable ASCII (no < > to keep anything tag-shaped out) plus typographic quotes, dashes and
# the ellipsis: every character here is in the bundled font, so nothing renders as a box.
_ALLOWED_CHARS = re.compile(r"[ -;=?-~‘’“”–—…\n]*")
_URLISH = re.compile(r"(?i)[a-z][a-z0-9+.-]*://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|co|dev|app|"
                     r"ai|me|info|biz|us|uk|de|eu|xyz|site|online|shop|store|tech|ly|to|tv)\b")


def _text_problem(key: str, text: str) -> str | None:
    if not _ALLOWED_CHARS.fullmatch(text):
        bad = next(c for c in text if not _ALLOWED_CHARS.fullmatch(c))
        return f"has a character the card cannot use ({bad!r})"
    if "@" in text:
        return "has an @ (no mentions, no addresses)"
    if "#" in text:
        return "has a # (hashtags go in the hashtags field)"
    if _URLISH.search(text):
        return "has a link, URL or domain (Instagram captions do not link; the bio does)"
    return _contact_problem(text) or _scrooge_problem("body_md" if key == "caption" else key, text)


def _single_line(key: str, value: Any, bounds: tuple[int, int]) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key}: required, a string")  # noqa: TRY004 - one refusal type
    text = value.strip()
    if "\n" in text:
        raise ValueError(f"{key}: one line only")
    low, high = bounds
    if not low <= len(text) <= high:
        raise ValueError(f"{key}: {low}-{high} characters (this is {len(text)})")
    problem = _text_problem(key, text)
    if problem:
        raise ValueError(f"{key}: {problem}")
    return text


def full_caption(caption: str, hashtags: list[str]) -> str:
    """What Instagram receives: the caption, a blank line, then the hashtags."""
    tags = " ".join(f"#{t}" for t in hashtags)
    return f"{caption}\n\n{tags}" if tags else caption


def check_post(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The post exactly as it will be rendered and published, or ValueError("<field>: <why>").
    Includes the fit check: a headline or point the card cannot hold is refused here."""
    if not isinstance(payload, Mapping):
        raise ValueError("post: must be an object")  # noqa: TRY004
    unknown = sorted(set(payload) - POST_FIELDS)
    if unknown:
        raise ValueError(f"{unknown[0]}: not a post field (allowed: {', '.join(sorted(POST_FIELDS))})")
    draft_id = payload.get("draft_id")
    if not isinstance(draft_id, str) or not _DRAFT_ID.fullmatch(draft_id):
        raise ValueError("draft_id: 1-64 of a-z, 0-9 and '-'")
    headline = _single_line("headline", payload.get("headline"), HEADLINE)
    points = payload.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f"points: a list of 1-{MAX_POINTS}")
    points = [_single_line(f"points[{i}]", p, POINT) for i, p in enumerate(points)]
    caption = payload.get("caption")
    if not isinstance(caption, str):
        raise ValueError("caption: required, a string")  # noqa: TRY004 - one refusal type
    caption = caption.strip()
    if not CAPTION[0] <= len(caption) <= CAPTION[1]:
        raise ValueError(f"caption: {CAPTION[0]}-{CAPTION[1]} characters (this is {len(caption)})")
    problem = _text_problem("caption", caption)
    if problem:
        raise ValueError(f"caption: {problem}")
    hashtags = payload.get("hashtags", [])
    if hashtags is None:
        hashtags = []
    if not isinstance(hashtags, list) or len(hashtags) > MAX_HASHTAGS:
        raise ValueError(f"hashtags: a list of at most {MAX_HASHTAGS}")
    for tag in hashtags:
        if not isinstance(tag, str) or not _HASHTAG.fullmatch(tag):
            raise ValueError("hashtags: each 2-30 of a-z, 0-9 and _, without the #")
    if len(set(hashtags)) != len(hashtags):
        raise ValueError("hashtags: no repeats")
    if len(full_caption(caption, hashtags)) > INSTAGRAM_CAPTION_LIMIT:
        raise ValueError(f"caption: with its hashtags, over Instagram's {INSTAGRAM_CAPTION_LIMIT}")
    try:
        layout(headline, points)
    except CardTooLong as exc:
        raise ValueError(f"card: {exc}") from exc
    return {"draft_id": draft_id, "headline": headline, "points": points,
            "caption": caption, "hashtags": list(hashtags)}
