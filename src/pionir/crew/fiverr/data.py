"""The data-cleanup worker: a buyer's CSV / JSON / Excel file, cleaned and converted.

Local code only, deterministic, no model: the same rules as our convert API (Scrooge
``products/convert.ts``: RFC 4180 CSV, a UTF-8 byte-order mark dropped, strings kept
exactly as sent - leading zeros stay, "=..." is text, never a formula), done on this
machine so the buyer's data goes nowhere.

The owner drops the buyer's file(s) in the order's ``input/`` folder (Pionir never touches
Fiverr, so it cannot download them). Each file is read (``read_table``), cleaned
(``clean``) and written in the formats the brief asks for (CSV, JSON, Excel - all three when
it names none), with ``cleanup-report.md`` saying exactly what was changed, in counts.

**The cleanup, and nothing more** - every step is counted in the report:

- whitespace trimmed from the ends of every cell (never inside it);
- header names trimmed; a blank one named ``column_N``; a repeated one numbered (``name_2``);
- rows that are entirely empty removed, and columns that are entirely empty (header too);
- ragged rows padded with empty cells to the header's width (a longer row keeps its extra
  cells under ``extra_N`` columns - nothing is dropped);
- exact duplicate rows removed (the first kept).

Values are never re-typed or "fixed": a date stays the text it was. Anything the brief asks
for beyond this is listed on the owner's card for him to do or to check.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

MAX_INPUT_BYTES = 20_000_000
MAX_ROWS = 200_000
MAX_COLS = 500
MAX_XLSX_XML_BYTES = 100_000_000
SUPPORTED = (".csv", ".tsv", ".txt", ".json", ".xlsx")
FORMULA_START = ("=", "+", "-", "@")
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


class DataProblem(ValueError):
    """Why a file cannot be cleaned, in words for the owner."""


@dataclass
class Table:
    name: str
    header: list
    rows: list                      # lists of strings (or numbers/bools/None from JSON)
    notes: list = field(default_factory=list)


# ---- reading ------------------------------------------------------------------------------
def _text(raw: bytes, name: str) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise DataProblem(f"{name} is not UTF-8 or Windows-1252 text") from exc


def _read_csv(text: str, name: str, delimiter: str | None) -> Table:
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter,
                               strict=True))
    except csv.Error as exc:
        raise DataProblem(f"{name} is not valid CSV ({exc})") from exc
    if len(rows) > MAX_ROWS + 1:
        raise DataProblem(f"{name} has more than {MAX_ROWS:,} rows")
    rows = [r for r in rows if r != []]
    if not rows:
        raise DataProblem(f"{name} is empty")
    shown = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}.get(delimiter,
                                                                          repr(delimiter))
    return Table(name, rows[0], rows[1:], [f"read as CSV separated by {shown}"])


def _read_json(text: str, name: str) -> Table:
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise DataProblem(f"{name} is not valid JSON ({exc})") from exc
    if isinstance(doc, dict):
        lists = [(k, v) for k, v in doc.items() if isinstance(v, list)]
        if len(lists) != 1:
            raise DataProblem(f"{name} is a JSON object without exactly one list of rows in it")
        doc = lists[0][1]
    if not isinstance(doc, list) or not doc:
        raise DataProblem(f"{name} has no rows (a JSON list of objects is expected)")
    if len(doc) > MAX_ROWS:
        raise DataProblem(f"{name} has more than {MAX_ROWS:,} rows")
    if all(isinstance(r, list) for r in doc):
        return Table(name, [str(c) for c in doc[0]], [list(r) for r in doc[1:]],
                     ["read as a JSON list of rows (the first row is the header)"])
    if not all(isinstance(r, dict) for r in doc):
        raise DataProblem(f"{name} mixes objects and other things in its list of rows")
    header: list = []
    for r in doc:
        for k in r:
            if k not in header:
                header.append(k)
    rows = []
    for r in doc:
        rows.append([r.get(k) if not isinstance(r.get(k), (dict, list))
                     else json.dumps(r.get(k), ensure_ascii=False) for k in header])
    return Table(name, header, rows, ["read as a JSON list of objects"])


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _read_xlsx(raw: bytes, name: str) -> Table:
    """The first worksheet of a workbook: shared and inline strings, numbers and booleans
    as their text. Bounded (an entry that unpacks too large is refused)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise DataProblem(f"{name} is not an Excel workbook") from exc
    with z:
        if sum(i.file_size for i in z.infolist()) > MAX_XLSX_XML_BYTES:
            raise DataProblem(f"{name} unpacks too large to read safely")

        def xml(path):
            try:
                return ElementTree.fromstring(z.read(path))
            except KeyError:
                return None
            except ElementTree.ParseError as exc:
                raise DataProblem(f"{name}: {path} cannot be read") from exc

        book = xml("xl/workbook.xml")
        rels = xml("xl/_rels/workbook.xml.rels")
        if book is None or rels is None:
            raise DataProblem(f"{name} is not an Excel workbook (no xl/workbook.xml)")
        first = book.find("m:sheets/m:sheet", _NS)
        if first is None:
            raise DataProblem(f"{name} has no worksheet")
        target = next((r.get("Target") for r in rels
                       if r.get("Id") == first.get(_REL)), None)
        if not target:
            raise DataProblem(f"{name}: its first worksheet cannot be found")
        target = target.lstrip("/")
        path = target if target.startswith("xl/") else f"xl/{target}"
        sheet = xml(path)
        if sheet is None:
            raise DataProblem(f"{name}: its first worksheet cannot be read")
        shared_doc = xml("xl/sharedStrings.xml")
        shared = ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t"))
                  for si in (shared_doc if shared_doc is not None else [])]
        grid: list = []
        for row in sheet.iter(f"{{{_NS['m']}}}row"):
            cells: dict = {}
            for c in row.findall("m:c", _NS):
                ref = c.get("r") or ""
                col = _col(ref) if re.match(r"[A-Z]+", ref) else len(cells)
                if col >= MAX_COLS:
                    raise DataProblem(f"{name} has more than {MAX_COLS} columns")
                kind = c.get("t")
                v = c.find("m:v", _NS)
                if kind == "s" and v is not None:
                    idx = int(v.text or 0)
                    value = shared[idx] if 0 <= idx < len(shared) else ""
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
                elif kind == "b" and v is not None:
                    value = "TRUE" if (v.text or "") == "1" else "FALSE"
                else:
                    value = v.text if v is not None and v.text is not None else ""
                cells[col] = value
            width = max(cells) + 1 if cells else 0
            grid.append([cells.get(i, "") for i in range(width)])
            if len(grid) > MAX_ROWS + 1:
                raise DataProblem(f"{name} has more than {MAX_ROWS:,} rows")
        grid = [r for r in grid if r]
        if not grid:
            raise DataProblem(f"{name}'s first worksheet is empty")
        return Table(name, grid[0], grid[1:],
                     [f"read the first worksheet ({first.get('name') or 'Sheet1'})"])


def read_table(path: Path) -> Table:
    """The buyer's file as a table, or DataProblem saying why not. ANY failure reading a
    buyer's file - a damaged workbook, a bad number in its XML, a broken deflate stream -
    is a DataProblem for that file: a buyer's file never raises anything else."""
    try:
        return _read_table(Path(path))
    except DataProblem:
        raise
    except Exception as exc:  # noqa: BLE001 - a buyer's file is untrusted input
        raise DataProblem(f"{Path(path).name} could not be read ({type(exc).__name__}); it "
                          "may be damaged or not the format its name says") from exc


def _read_table(path: Path) -> Table:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise DataProblem(f"{path.name} is not a regular file")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise DataProblem(f"{path.name} is over {MAX_INPUT_BYTES:,} bytes")
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return _read_xlsx(raw, path.name)
    text = _text(raw, path.name)
    if suffix == ".json":
        return _read_json(text, path.name)
    if suffix in (".csv", ".txt", ".tsv"):
        return _read_csv(text, path.name, "\t" if suffix == ".tsv" else None)
    raise DataProblem(f"{path.name}: only {', '.join(SUPPORTED)} files are read")


# ---- cleaning -----------------------------------------------------------------------------
def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def clean(table: Table) -> tuple:
    """``(cleaned Table, counts)``. Counts every change; changes nothing else."""
    counts = {"cells_trimmed": 0, "headers_renamed": 0, "empty_rows_removed": 0,
              "empty_columns_removed": 0, "rows_padded": 0, "extra_columns_added": 0,
              "duplicate_rows_removed": 0, "formula_like_cells": 0}
    header = []
    unnamed: set = set()            # columns whose header was blank (or added below)
    for i, h in enumerate(table.header):
        name = str(h if h is not None else "").strip()
        if not name:
            name = f"column_{i + 1}"
            unnamed.add(i)
            counts["headers_renamed"] += 1
        elif name != (h if isinstance(h, str) else str(h)):
            counts["headers_renamed"] += 1
        header.append(name)
    width = len(header)
    rows = []
    for r in table.rows:
        row = []
        for v in r:
            if isinstance(v, str) and v != v.strip():
                counts["cells_trimmed"] += 1
                v = v.strip()
            row.append(v)
        if all(_blank(v) for v in row):
            counts["empty_rows_removed"] += 1
            continue
        if len(row) < width:
            counts["rows_padded"] += 1
            row += [""] * (width - len(row))
        rows.append(row)
    longest = max([width, *(len(r) for r in rows)])
    for i in range(width, longest):
        header.append(f"extra_{i - width + 1}")
        unnamed.add(i)
        counts["extra_columns_added"] += 1
    rows = [r + [""] * (longest - len(r)) for r in rows]
    keep = [i for i in range(longest)
            if not (i in unnamed and all(_blank(r[i]) for r in rows))]
    counts["empty_columns_removed"] = longest - len(keep)
    header = [header[i] for i in keep]
    rows = [[r[i] for i in keep] for r in rows]
    seen_names: dict = {}
    for i, h in enumerate(header):
        low = h.lower()
        if low in seen_names:
            seen_names[low] += 1
            header[i] = f"{h}_{seen_names[low]}"
            counts["headers_renamed"] += 1
        else:
            seen_names[low] = 1
    unique, seen = [], set()
    for r in rows:
        key = json.dumps(r, ensure_ascii=False, default=str)
        if key in seen:
            counts["duplicate_rows_removed"] += 1
            continue
        seen.add(key)
        unique.append(r)
    counts["formula_like_cells"] = sum(1 for r in unique for v in r
                                       if isinstance(v, str) and v.startswith(FORMULA_START))
    return Table(table.name, header, unique, list(table.notes)), counts


# ---- writing ------------------------------------------------------------------------------
def to_csv(t: Table) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(t.header)
    for r in t.rows:
        w.writerow(["" if v is None else (json.dumps(v) if isinstance(v, bool) else v)
                    for v in r])
    return buf.getvalue()


def to_json(t: Table) -> str:
    return json.dumps([dict(zip(t.header, r, strict=True)) for r in t.rows],
                      ensure_ascii=False, indent=1) + "\n"


def _xml_text(s: str) -> str:
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _ref(col: int, row: int) -> str:
    letters = ""
    col += 1
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def _cell(v, col: int, row: int) -> str:
    ref = _ref(col, row)
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return f'<c r="{ref}" t="b"><v>{1 if v else 0}</v></c>'
    if isinstance(v, (int, float)):
        return f'<c r="{ref}"><v>{v!r}</v></c>'
    # every string is text, exactly as given: never a formula, never re-typed
    return (f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml_text(str(v))}'
            f'</t></is></c>')


def to_xlsx(t: Table) -> bytes:
    """A minimal, valid .xlsx: one sheet, a bold frozen header, text kept as text."""
    rows = ["<row r=\"1\">" + "".join(
        _cell(h, i, 1).replace("<c ", '<c s="1" ', 1) for i, h in enumerate(t.header))
        + "</row>"]
    for n, r in enumerate(t.rows, 2):
        rows.append(f'<row r="{n}">' + "".join(_cell(v, i, n) for i, v in enumerate(r))
                    + "</row>")
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             f'<worksheet xmlns="{_NS["m"]}"><sheetViews><sheetView workbookViewId="0">'
             '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
             "</sheetView></sheetViews><sheetData>" + "".join(rows)
             + "</sheetData></worksheet>")
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
            'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
            'org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_NS["m"]}" xmlns:r="{_NS["r"]}"><sheets>'
            '<sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
            'org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<styleSheet xmlns="{_NS["m"]}"><fonts count="2"><font><sz val="11"/></font>'
            '<font><b/><sz val="11"/></font></fonts><fills count="1"><fill><patternFill '
            'patternType="none"/></fill></fills><borders count="1"><border/></borders>'
            '<cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf/>'
            '<xf fontId="1" applyFont="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in parts:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, parts[name].encode("utf-8"))
    return buf.getvalue()


# ---- the brief: which formats ---------------------------------------------------------------
_FORMATS = (("xlsx", r"\b(?:excel|xlsx|xls|spreadsheet|workbook)\b"),
            ("json", r"\bjson\b"),
            ("csv", r"\bcsv\b"))


def wanted_formats(brief: str, inputs: list) -> list:
    """The output formats the brief names AFTER a "to"/"into"/"as" (or anywhere, if no
    such phrase), minus the inputs' own format when the brief asks to convert; all three
    when it names none."""
    low = (brief or "").lower()
    target = re.search(r"\b(?:to|into|as|in)\b(.{0,80})", low)
    zone = target.group(1) if target else low
    found = [fmt for fmt, rx in _FORMATS if re.search(rx, zone)]
    if not found:
        found = [fmt for fmt, rx in _FORMATS if re.search(rx, low)]
    return found or ["csv", "json", "xlsx"]


def report(tables: list, formats: list, skipped: list) -> str:
    lines = ["# Data cleanup report", "",
             "What was done to each file, in counts. Values were never re-typed or changed "
             "beyond trimming spaces at the ends of cells.", ""]
    for t, counts, outputs in tables:
        lines += [f"## {t.name}", ""]
        lines += [f"- {n}" for n in t.notes]
        lines += [f"- {len(t.rows):,} rows and {len(t.header)} columns after cleanup",
                  f"- cells with spaces trimmed at the ends: {counts['cells_trimmed']:,}",
                  f"- header names fixed (blank, spaces or repeated): "
                  f"{counts['headers_renamed']:,}",
                  f"- empty rows removed: {counts['empty_rows_removed']:,}",
                  f"- empty unnamed columns removed: {counts['empty_columns_removed']:,}",
                  f"- short rows padded with empty cells: {counts['rows_padded']:,}",
                  f"- columns added for rows longer than the header: "
                  f"{counts['extra_columns_added']:,}",
                  f"- exact duplicate rows removed: {counts['duplicate_rows_removed']:,}"]
        if counts["formula_like_cells"]:
            lines.append(f"- note: {counts['formula_like_cells']:,} cells start with =, +, - "
                         "or @; a spreadsheet app may read them as formulas when it opens the "
                         "CSV (the Excel file keeps them as plain text)")
        lines += [f"- written as: {', '.join(outputs)}", ""]
    for name, why in skipped:
        lines += [f"## {name} (not processed)", "", f"- {why}", ""]
    lines += [f"Formats delivered: {', '.join(formats)}.", ""]
    return "\n".join(lines)
