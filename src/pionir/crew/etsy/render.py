"""Pictures of the real product, drawn with Pillow and the bundled IBM Plex font - no model.

Nothing here invents what the buyer gets: every image is drawn FROM the files that ship.

- ``printable_pages`` draws each printable page from the workbook's Tracker sheet as read
  back from the .xlsx (its headline, headers and row labels), and ``pdf_bytes`` makes the
  PDF from exactly those page images;
- ``preview_images`` draws the listing photos: the Example sheet as read back from the .xlsx
  with its formulas computed (``sheets.read_sheet``), the first printable page itself
  (the same pixels the PDF holds, scaled), and the list of files with their real sizes,
  sheet names and the How to use lines read back from the workbook;
- ``design_png`` draws a print-on-demand design: the checked phrase, centred, at the print
  area's exact pixel size, on a transparent background.
"""
from __future__ import annotations

import io
import math

from pionir.social.card import BOLD, REGULAR, SEMIBOLD, _font

from . import sheets

INK = (22, 32, 42)
ACCENT = (108, 194, 166)
PAPER = (255, 255, 255)
SOFT = (242, 245, 247)
CALC = (232, 244, 239)
TOTAL = (211, 233, 224)
LINE = (159, 176, 190)
MUTED = (90, 104, 116)

PREVIEW_SIZE = (2000, 1500)          # 4:3; Etsy shows listing photos at 4:3 and wants 2000px
PAGE_DPI = 200
LETTER = (int(8.5 * PAGE_DPI), int(11 * PAGE_DPI))
PRINTABLE_ROWS = 14                  # labelled rows plus blank ones to write in


def _png(img) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _fit_text(draw, text: str, weight: int, max_width: int, start: int, low: int = 12):
    size = start
    while size > low:
        font = _font(size, weight)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(low, weight)


def _clip(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def draw_table(draw, box, rows: list, *, header: int, total: int | None, roles: list,
               widths: list, font_size: int, blank_inputs: bool = False) -> None:
    """``rows``: the cell texts, ``header`` the index of the header row in ``rows`` and
    ``total`` of the total row; ``roles`` per column ("label" / "input" / "formula").
    With ``blank_inputs`` every data cell but the label is drawn empty (a printable)."""
    x0, y0, x1, y1 = box
    scale = (x1 - x0) / sum(widths)
    xs = [x0]
    for w in widths:
        xs.append(xs[-1] + w * scale)
    row_h = (y1 - y0) / max(len(rows), 1)
    font = _font(font_size, REGULAR)
    bold = _font(font_size, SEMIBOLD)
    for i, row in enumerate(rows):
        top, bottom = y0 + i * row_h, y0 + (i + 1) * row_h
        for c in range(len(widths)):
            left, right = xs[c], xs[c + 1]
            text = row[c] if c < len(row) else ""
            if i == header:
                fill, color, f = INK, PAPER, bold
            elif total is not None and i == total:
                fill, color, f = TOTAL, INK, bold
            elif roles[c] == "formula":
                fill, color, f = CALC, INK, font
            else:
                fill, color, f = PAPER, INK, font
            draw.rectangle((left, top, right, bottom), fill=fill, outline=LINE, width=2)
            if blank_inputs and i != header and c > 0 and (total is None or i != total):
                text = ""
            if blank_inputs and total is not None and i == total and c > 0:
                text = ""
            if not text:
                continue
            text = _clip(draw, text, f, int(right - left - 16))
            tw = draw.textlength(text, font=f)
            if c == 0 and i != header:
                tx = left + 10
            elif i == header:
                tx = left + (right - left - tw) / 2
            else:
                tx = right - 10 - tw
            draw.text((tx, top + (row_h - font_size) / 2 - font_size * 0.12), text,
                      font=f, fill=color)


def _table_parts(kind_name: str, grid: sheets.Grid, *, extra_rows: int = 0):
    kind = sheets.KINDS[kind_name]
    roles = [c.role for c in kind.columns]
    widths = [c.width for c in kind.columns]
    h = grid.header_row - 1
    t = grid.total_row - 1
    body = [list(r[:len(widths)]) for r in grid.rows[h:t + 1]]
    if extra_rows:
        body = body[:-1] + [[""] * len(widths) for _ in range(extra_rows)] + body[-1:]
    return roles, widths, body


def printable_pages(xlsx: bytes, kind_name: str) -> list:
    """One printable page per workbook (Letter, ``PAGE_DPI``), drawn from the Tracker sheet
    as read back from the .xlsx: its headline, its top lines, its headers and its labels,
    with every box empty to fill in by hand."""
    from PIL import Image, ImageDraw

    kind = sheets.KINDS[kind_name]
    grid = sheets.read_sheet(xlsx, "Tracker")
    size = (LETTER[1], LETTER[0]) if kind.landscape else LETTER
    page = Image.new("RGB", size, PAPER)
    draw = ImageDraw.Draw(page)
    margin = int(0.5 * PAGE_DPI)
    title = grid.rows[0][0]
    draw.text((margin, margin), title, font=_fit_text(draw, title, BOLD, size[0] - 2 * margin,
                                                       64), fill=INK)
    y = margin + 110
    for top in kind.top:
        draw.text((margin, y), top.label + ":", font=_font(34, SEMIBOLD), fill=INK)
        lx = margin + draw.textlength(top.label + ":  ", font=_font(34, SEMIBOLD))
        draw.line((lx, y + 40, lx + 360, y + 40), fill=LINE, width=3)
        y += 64
    n_labels = grid.total_row - grid.header_row - 1
    extra = max(0, PRINTABLE_ROWS - n_labels)
    roles, widths, body = _table_parts(kind_name, grid, extra_rows=extra)
    bottom = size[1] - margin - 60
    font_size = 30 if not kind.landscape else 24
    draw_table(draw, (margin, y + 20, size[0] - margin, bottom), body, header=0,
               total=len(body) - 1, roles=roles, widths=widths, font_size=font_size,
               blank_inputs=True)
    note = "Shaded boxes are worked out from the others."
    draw.text((margin, bottom + 18), note, font=_font(24, REGULAR), fill=MUTED)
    return [page]


def pdf_bytes(pages: list) -> bytes:
    out = io.BytesIO()
    first, rest = pages[0], pages[1:]
    first.save(out, format="PDF", resolution=float(PAGE_DPI), save_all=True,
               append_images=rest)
    return out.getvalue()


def _canvas(title: str, subtitle: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", PREVIEW_SIZE, SOFT)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, PREVIEW_SIZE[0], 300), fill=INK)
    draw.rectangle((80, 250, 220, 262), fill=ACCENT)
    draw.text((80, 70), title, font=_fit_text(draw, title, BOLD, PREVIEW_SIZE[0] - 160, 96),
              fill=PAPER)
    draw.text((80, 185), subtitle, font=_font(44, REGULAR), fill=(205, 216, 224))
    return img, draw


def preview_images(xlsx: bytes, pages: list, kind_name: str, headline: str,
                   files: list[tuple[str, int]]) -> list[tuple[str, bytes, str]]:
    """The listing photos as (file name, PNG bytes, alt text), every one drawn from the
    shipped files (see the module's docstring)."""
    from PIL import Image

    kind = sheets.KINDS[kind_name]
    out: list[tuple[str, bytes, str]] = []
    # 1. the Example sheet, computed
    grid = sheets.read_sheet(xlsx, "Example")
    roles, widths, body = _table_parts(kind_name, grid)
    img, draw = _canvas(headline, "Spreadsheet (.xlsx) with formulas + printable PDF")
    shown = body if len(body) <= 13 else body[:12] + [body[-1]]
    draw.text((80, 340), "The Example sheet, as it computes:", font=_font(40, SEMIBOLD),
              fill=INK)
    height = min(1050, 80 * len(shown))
    draw_table(draw, (80, 410, PREVIEW_SIZE[0] - 80, 410 + height), shown, header=0,
               total=len(shown) - 1, roles=roles, widths=widths,
               font_size=30 if not kind.landscape else 20)
    out.append(("1-example.png", _png(img),
                f"The example sheet of the {kind.noun} with sample numbers worked out"))
    # 2. the printable page itself (the same pixels as the PDF's page), scaled
    img, draw = _canvas(headline, "The printable page, as it prints")
    page = pages[0]
    room = (PREVIEW_SIZE[0] - 160, PREVIEW_SIZE[1] - 380)
    k = min(room[0] / page.width, room[1] / page.height)
    small = page.resize((max(1, int(page.width * k)), max(1, int(page.height * k))),
                        Image.LANCZOS)
    left = (PREVIEW_SIZE[0] - small.width) // 2
    draw.rectangle((left + 14, 344, left + small.width + 14, 344 + small.height),
                   fill=(200, 208, 214))
    img.paste(small, (left, 330))
    out.append(("2-printable.png", _png(img),
                f"The printable page of the {kind.noun}, blank to fill in by hand"))
    # 3. what is in the download, read back from the files
    img, draw = _canvas(headline, "What the download holds")
    y = 360
    draw.text((80, y), "Files", font=_font(46, BOLD), fill=INK)
    y += 80
    for name, size in files:
        draw.text((110, y), f"{name}  ({max(1, math.ceil(size / 1024))} KB)",
                  font=_font(38, REGULAR), fill=INK)
        y += 60
    y += 30
    draw.text((80, y), "Sheets in the workbook", font=_font(46, BOLD), fill=INK)
    y += 80
    draw.text((110, y), ", ".join(sheets.sheet_names(xlsx)), font=_font(38, REGULAR),
              fill=INK)
    y += 90
    draw.text((80, y), "How to use it", font=_font(46, BOLD), fill=INK)
    y += 80
    for line in sheets.howto_lines(xlsx)[1:]:
        if y > PREVIEW_SIZE[1] - 90:
            break
        draw.text((110, y), _clip(draw, line, _font(34, REGULAR), PREVIEW_SIZE[0] - 220),
                  font=_font(34, REGULAR), fill=INK)
        y += 56
    out.append(("3-included.png", _png(img),
                f"The files in the {kind.noun} download and how to use them"))
    return out


# ---- print on demand ---------------------------------------------------------------------
def _wrap(draw, words: list[str], font, width: int) -> list[str] | None:
    lines, line = [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            line = trial
            continue
        if not line:
            return None
        lines.append(line)
        line = word
        if draw.textlength(line, font=font) > width:
            return None
    if line:
        lines.append(line)
    return lines


def design_png(phrase: str, width: int, height: int, ink: tuple = INK) -> bytes:
    """The phrase, bold and centred, filling up to 86% of the print area's width, on a
    transparent background of exactly ``width`` x ``height`` pixels."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    words = phrase.split()
    usable = int(width * 0.86)
    size = max(24, int(height * 0.22))
    while size > 24:
        font = _font(size, BOLD)
        lines = _wrap(draw, words, font, usable)
        if lines is not None and len(lines) * size * 1.2 <= height * 0.8:
            break
        size = int(size * 0.92)
    font = _font(size, BOLD)
    lines = _wrap(draw, words, font, usable) or [phrase]
    step = size * 1.2
    y = (height - step * len(lines)) / 2
    for line in lines:
        tw = draw.textlength(line, font=font)
        draw.text(((width - tw) / 2, y), line, font=font, fill=(*ink, 255))
        y += step
    bar = int(width * 0.12)
    draw.rectangle(((width - bar) / 2, y + size * 0.15, (width + bar) / 2,
                    y + size * 0.15 + max(6, size // 10)), fill=(*ACCENT, 255))
    return _png(img)
