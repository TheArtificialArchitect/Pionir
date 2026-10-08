"""Icons, screenshots and promo tiles for a listing, drawn deterministically with Pillow.

The same spec always gives the same bytes (no clock, no randomness), in the products' cover
style (pionir/social/card.py's fonts and colours). Nothing here pretends to be a screenshot
of the running product: the "screenshot" is a plain feature card with the product's own
words, and the listing says so where the store asks for real screenshots (Chrome).
"""
from __future__ import annotations

import io

from ..builds.package import PackageError

BG, PANEL = (18, 26, 35), (27, 38, 50)
INK, MUTED = (242, 245, 247), (150, 166, 180)


def _style():
    try:
        from PIL import Image, ImageDraw

        from pionir.social import card as style
    except ImportError as exc:
        raise PackageError("Pillow is not installed: no listing image can be drawn") from exc
    return Image, ImageDraw, style


def _png(img) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _initials(name: str) -> str:
    words = [w for w in name.replace(":", " ").replace("-", " ").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def icon(name: str, size: int) -> bytes:
    """A square icon: the accent tile with the product's initials."""
    Image, ImageDraw, style = _style()
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    d.rounded_rectangle((pad, pad, size - pad, size - pad), radius=size // 5, fill=style.ACCENT)
    font = style._font(max(8, int(size * 0.42)), style.BOLD)
    text = _initials(name)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text, font=font, fill=BG)
    return _png(img)


def feature_card(name: str, summary: str, features: list, size: tuple, *,
                 label: str) -> bytes:
    """A listing image: a label, the name, the summary and up to four features."""
    Image, ImageDraw, style = _style()
    w, h = size
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    m = max(24, w // 16)
    d.rectangle((0, 0, max(4, w // 128), h), fill=style.ACCENT)
    small = h < 400
    d.text((m, m), label, font=style._font(16 if small else 24, style.SEMIBOLD),
           fill=style.ACCENT)
    title_font = style._font(30 if small else 64, style.BOLD)
    short = name.split(":", 1)[0].strip()
    while title_font.getlength(short) > w - 2 * m and title_font.size > 18:
        title_font = style._font(title_font.size - 4, style.BOLD)
    y = m + (28 if small else 48)
    d.text((m, y), short, font=title_font, fill=INK)
    y += title_font.size + (10 if small else 24)
    body = style._font(16 if small else 30, style.REGULAR)
    try:
        lines = style._wrap(summary, body, w - 2 * m)[:2 if small else 3]
        points = [] if small else [style._wrap(f"• {f}", body, w - 2 * m - 32)[0]
                                   for f in features[:4]]
    except ValueError as exc:
        raise PackageError(f"the listing image cannot be drawn: {exc}") from exc
    for line in lines:
        d.text((m, y), line, font=body, fill=MUTED)
        y += body.size + 10
    if points:
        y += 20
        d.rounded_rectangle((m, y, w - m, min(h - m, y + 60 * len(points) + 30)), radius=18,
                            fill=PANEL)
        y += 24
        for p in points:
            d.text((m + 24, y), p, font=body, fill=INK)
            y += 60
    return _png(img)
