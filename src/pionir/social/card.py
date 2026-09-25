"""The Instagram card: a 1080x1350 (4:5) JPEG drawn from a checked headline and points.

Deterministic by construction - a bundled font (IBM Plex Sans, OFL, fonts/), fixed layout,
fixed JPEG settings, no metadata - so the approval card and the published post are the same
bytes. ``CARD_VERSION`` names this layout; a change to how cards look must bump it, and a
post approved under one version is re-rendered and compared before it is published.

Text that will not fit is refused (``CardTooLong``), never shrunk past legibility or clipped.
"""
from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

CARD_VERSION = 1
WIDTH, HEIGHT = 1080, 1350          # 4:5, the tallest ratio Instagram takes without cropping
MARGIN = 96
TEXT_WIDTH = WIDTH - 2 * MARGIN
FOOTER_TOP = HEIGHT - 170            # nothing but the footer below this line

FONT_PATH = Path(__file__).with_name("fonts") / "IBMPlexSans-Variable.ttf"
REGULAR, SEMIBOLD, BOLD = 400, 600, 700

BACKGROUND = (22, 32, 42)            # #16202a  deep ink
ACCENT = (108, 194, 166)             # #6cc2a6  Dokaz teal
INK = (242, 245, 247)                # #f2f5f7
MUTED = (159, 176, 190)              # #9fb0be

LABEL = "DOKAZ"
FOOTER = "Full guide: link in bio"
HEADLINE_SIZES = (78, 70, 62)        # tried in order; the first that fits in HEADLINE_LINES wins
HEADLINE_LINES = 5
POINT_SIZE = 42
POINT_LINES = 3                      # per point


class CardTooLong(ValueError):
    """The text does not fit the card. The message says which part and why."""


@lru_cache(maxsize=16)
def _font(size: int, weight: int):
    from PIL import ImageFont
    font = ImageFont.truetype(str(FONT_PATH), size)
    font.set_variation_by_axes([weight, 100])      # axes: Weight, Width
    return font


def _wrap(text: str, font, width: int) -> list[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if font.getlength(trial) <= width:
            line = trial
            continue
        if not line:                                # one word wider than the card
            raise CardTooLong(f"the word {word[:40]!r} is too wide for the card")
        lines.append(line)
        line = word
        if font.getlength(line) > width:
            raise CardTooLong(f"the word {word[:40]!r} is too wide for the card")
    if line:
        lines.append(line)
    return lines


def layout(headline: str, points: Sequence[str]) -> dict:
    """Where everything goes, or CardTooLong. Separate from drawing so a check can ask
    "does this fit?" without encoding a JPEG."""
    head_font, head_lines = None, None
    for size in HEADLINE_SIZES:
        font = _font(size, BOLD)
        lines = _wrap(headline, font, TEXT_WIDTH)
        if len(lines) <= HEADLINE_LINES:
            head_font, head_lines = font, lines
            break
    if head_font is None:
        raise CardTooLong(f"the headline needs more than {HEADLINE_LINES} lines")
    point_font = _font(POINT_SIZE, REGULAR)
    bullet_indent = 44
    point_lines = []
    for i, point in enumerate(points, 1):
        lines = _wrap(point, point_font, TEXT_WIDTH - bullet_indent)
        if len(lines) > POINT_LINES:
            raise CardTooLong(f"point {i} needs more than {POINT_LINES} lines")
        point_lines.append(lines)

    y = MARGIN
    items = [("label", MARGIN, y, LABEL)]
    y += 90
    head_step = int(head_font.size * 1.18)
    for line in head_lines:
        items.append(("headline", MARGIN, y, line))
        y += head_step
    y += 56
    items.append(("rule", MARGIN, y, None))
    y += 56
    point_step = int(POINT_SIZE * 1.34)
    for lines in point_lines:
        items.append(("bullet", MARGIN, y, None))
        for line in lines:
            items.append(("point", MARGIN + bullet_indent, y, line))
            y += point_step
        y += 30
    if y > FOOTER_TOP:
        raise CardTooLong("the headline and points together are too long for the card")
    return {"items": items, "head_size": head_font.size}


def render_card(headline: str, points: Sequence[str]) -> bytes:
    """The card as JPEG bytes (sRGB, no metadata). Same text in, same bytes out."""
    from PIL import Image, ImageDraw
    plan = layout(headline, points)
    img = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(img)
    head_font = _font(plan["head_size"], BOLD)
    point_font = _font(POINT_SIZE, REGULAR)
    label_font = _font(30, SEMIBOLD)
    for kind, x, y, text in plan["items"]:
        if kind == "label":
            spaced = x
            for ch in text:                                  # letter-spaced wordmark
                draw.text((spaced, y), ch, font=label_font, fill=ACCENT)
                spaced += label_font.getlength(ch) + 7
        elif kind == "headline":
            draw.text((x, y), text, font=head_font, fill=INK)
        elif kind == "rule":
            draw.rectangle((x, y, x + 120, y + 6), fill=ACCENT)
        elif kind == "bullet":
            top = y + POINT_SIZE // 2 + 6
            draw.rectangle((x, top, x + 18, top + 8), fill=ACCENT)
        elif kind == "point":
            draw.text((x, y), text, font=point_font, fill=INK)
    footer_font = _font(34, SEMIBOLD)
    draw.rectangle((MARGIN, HEIGHT - 128, MARGIN + 6, HEIGHT - 84), fill=ACCENT)
    draw.text((MARGIN + 26, HEIGHT - 129), FOOTER, font=footer_font, fill=MUTED)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90, subsampling=0, optimize=False, progressive=False)
    return out.getvalue()


def card_sha(headline: str, points: Sequence[str]) -> str:
    return hashlib.sha256(render_card(headline, points)).hexdigest()
