"""Mending a posting worker's draft so it passes the content check - never loosening the check.

Measured on 2026-10-07 (five fresh blog drafts from gemma3:12b, exactly as the worker drafts):
5 of 5 were blocked, 29 of their 32 reasons "names X, which is not on the allowlist", and the
words were nearly all ordinary ones the model had written in Title Case inside a bold list
label (``- **Streamlined Onboarding:**``), a British spelling the en_US list does not hold
("Summarisation"), the field names of a non-JSON code block (a vCard: BEGIN, VERSION, FN,
TEL ...) or an HTML tag inside backticks (`` `<head>` ``) - plus, in the record's 23 blocked
drafts, invented example data (INV-2024-001, Acme Corp, 123 Main St, user@domain.com).

So a draft is mended in three steps, each one followed by the SAME fail-closed check
(contentcheck) on the exact text that would go out. Nothing here decides that a draft may be
published: it only changes the draft, and only ever toward fewer names and less data.

1. **Always, by fixed rules** (``mend_body``, run by the blog's ``repair`` before any check):
   - a fenced code block that is not one JSON value is removed (the prompt allows only a JSON
     request body; anything else - a vCard, HTML, a shell line - is where invented data and
     capitalised field names live); in a JSON block every string value becomes a placeholder
     named after its key (``"company": "Acme Corp"`` -> ``"company": "{company}"``) and a
     number of five or more digits becomes 100: example values are invented data by
     definition;
   - an HTML tag written as text (``<head>``, `` `</meta>` ``) loses its angle brackets;
   - in a heading or a list item's bold or italic label, a later word written in Title Case
     is put in lower case - ONLY when the check's own heading rule would vouch for it anyway
     (an ordinary dictionary word, or a word the post also writes in lower case). An
     allowlisted name is never touched, so a name the check would refuse stays capitalised
     and is still refused.
2. **After a block, from the check's own reasons** (``placeholders_from_reasons``): every
   email address, @-handle, invented id, phone number or IP address the check quoted, and
   every company it named, is replaced by a placeholder (``{email-address}``,
   ``{reference-number}``, ``{company-name}`` ...). The check found them; this only removes
   exactly what it found.
3. **After a block, by the model** (``flagged_units`` / ``apply_rewrites``): only the
   sentences holding a word or phrase the check quoted are sent back to the shared brain, with
   the reasons, to be rewritten; every other sentence is kept as it was.

Each step says what it changed (``done``), and the worker keeps that beside the draft.
"""
from __future__ import annotations

import json
import re

from . import words as _words

# ---- 1. always -------------------------------------------------------------------------------

_FENCE_OPEN = re.compile(r"^(\s{0,3})(```|~~~)\s*([A-Za-z0-9_+-]*)\s*$")
BIG_NUMBER = 10000


def _placeholder(key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-")
    return "{" + (slug or "value") + "}"


def _blank_values(value, key: str = "value"):
    """A JSON value with every example taken out: strings become ``{key}`` placeholders,
    big numbers become 100, keys are lower-cased."""
    if isinstance(value, dict):
        return {str(k).lower(): _blank_values(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_blank_values(v, key) for v in value[:5]]
    if isinstance(value, str):
        return _placeholder(key)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)) and abs(value) >= BIG_NUMBER:
        return 100
    return value


def scrub_code_blocks(field: str, text: str, done: list) -> str:
    """Fenced blocks: a JSON value is kept with its examples blanked; anything else goes."""
    lines = text.split("\n")
    out: list = []
    i = 0
    while i < len(lines):
        m = _FENCE_OPEN.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        fence = m.group(2)
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith(fence):
            j += 1
        body = "\n".join(lines[i + 1:j])
        try:
            value = json.loads(body)
        except ValueError:
            value = None
        if isinstance(value, (dict, list)):
            blank = json.dumps(_blank_values(value), indent=2, ensure_ascii=True)
            if blank != body.strip():
                done.append(f"{field}: example values in a JSON block made placeholders")
            out += [f"{m.group(1)}```json", *blank.split("\n"), f"{m.group(1)}```"]
        else:
            first = next((x.strip() for x in lines[i + 1:j] if x.strip()), "")
            done.append(f"{field}: removed a code block that is not JSON "
                        f"({first[:40]!r})")
            while out and not out[-1].strip():
                out.pop()
            if out and out[-1].rstrip().endswith(":"):
                # "For example:" with nothing after it any more
                out.pop()
            out.append("")
        i = j + 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


_TAG_TEXT = re.compile(r"</?([A-Za-z][A-Za-z0-9-]{0,20})\s*/?>")


def unbracket_tags(field: str, text: str, done: list) -> str:
    """``<head>`` -> ``head``: a tag named in prose or in backticks, never one with attributes
    (that stays, and the check refuses it)."""
    def strip(m: re.Match) -> str:
        done.append(f"{field}: {m.group(0)!r} -> {m.group(1)!r}")
        return m.group(1)
    return _TAG_TEXT.sub(strip, text)


_HEADING_LINE = re.compile(r"^(\s{0,3}#{1,6}\s+)(.*)$")
_LABEL_LINE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(\*\*|__|\*|_)(.+?)\2")
_TITLE_WORD = re.compile(r"(?<![\w'’-])([A-Z][a-z]+(?:-[A-Za-z][a-z]+)*)(?![\w'’])")


def _lower_words(texts) -> set:
    return {t.lower() for text in texts for t in re.findall(r"[\w'’-]+", text)
            if t == t.lower() and any(c.isalpha() for c in t)}


def _protected(label: str, names) -> list:
    """Character spans of allowlisted names (as written) inside a label."""
    spans = []
    for name in names:
        for m in re.finditer(rf"(?<![\w-]){re.escape(name)}(?![\w-])", label):
            spans.append(m.span())
    return spans


def sentence_case(field: str, text: str, done: list, *, names=(), lower_words=None) -> str:
    """Headings and list-item labels in sentence case, where the check's heading rule would
    vouch for the word anyway (see the module's step 1)."""
    lower_words = _lower_words([text]) if lower_words is None else lower_words
    fixed = 0
    out, fenced = [], False
    for line in text.split("\n"):
        if _FENCE_OPEN.match(line) or line.strip().startswith(("```", "~~~")):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        m = _HEADING_LINE.match(line)
        if m:
            start, end = m.start(2), len(line)
        else:
            m = _LABEL_LINE.match(line)
            if not m:
                out.append(line)
                continue
            start, end = m.start(3), m.end(3)
        label = line[start:end]
        keep = _protected(label, names)
        words = list(_TITLE_WORD.finditer(label))
        chars = list(label)
        for w in words:
            if w.start() == len(label) - len(label.lstrip()):
                continue                          # the label's first word keeps its capital
            if any(a <= w.start() < b for a, b in keep):
                continue
            tok = w.group(1)
            if tok.lower() in lower_words or _words.ordinary_word(tok):
                chars[w.start()] = tok[0].lower()
                fixed += 1
        out.append(line[:start] + "".join(chars) + line[end:])
    if fixed:
        done.append(f"{field}: {fixed} Title Case word(s) in headings and labels made lower case")
    return "\n".join(out)


_SCHEME_WORD = re.compile(r"(?i)\b(?:data|java\s*script|vb\s*script)$")
_COLON_IN_LABEL = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)(\*\*|__|\*|_)([^\n]+?):\2",
                             re.MULTILINE)


def colon_outside_label(field: str, text: str, done: list) -> str:
    """``- **Prepare the data:** ...`` -> ``- **Prepare the data**: ...``. Inside the bold,
    "data:" is followed by ``**``, which reads as a ``data:`` URL (live: a vCard post blocked
    for exactly that); outside, it is just a colon."""
    n = 0

    def move(m: re.Match) -> str:
        nonlocal n
        if not _SCHEME_WORD.search(m.group(3)):
            return m.group(0)               # only where the colon would read as a scheme
        n += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(2)}:"
    text = _COLON_IN_LABEL.sub(move, text)
    if n:
        done.append(f"{field}: {n} label colon(s) moved outside the bold")
    return text


def mend_body(field: str, text: str, done: list, *, names=(), others=()) -> str:
    """Step 1 on a Markdown field (the blog's body)."""
    text = scrub_code_blocks(field, text, done)
    text = unbracket_tags(field, text, done)
    text = colon_outside_label(field, text, done)
    return sentence_case(field, text, done, names=names,
                         lower_words=_lower_words([text, *others]))


# ---- 2. from the check's reasons -------------------------------------------------------------

# reason pattern -> placeholder. Each matches contentcheck's own wording; the quoted literal is
# exactly what the check found in the text.
_PERSONAL = (
    (re.compile(r"has an email address \((['\"])(.+?)\1\)"), "{email-address}"),
    (re.compile(r"has an @-handle \((['\"])(.+?)\1\)"), "{handle}"),
    (re.compile(r"has an invented id or reference number \((['\"])(.+?)\1\)"),
     "{reference-number}"),
    (re.compile(r"has a phone number \((['\"])(.+?)\1\)"), "{phone-number}"),
    (re.compile(r"has an IP address \((['\"])(.+?)\1\)"), "{ip-address}"),
    (re.compile(r"names the company (['\"])(.+?)\1"), "{company-name}"),
)


def _map_text(value, fn):
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [fn(v) if isinstance(v, str) else v for v in value]
    return value


def placeholders_from_reasons(raw: dict, fields, reasons) -> tuple:
    """(raw with every literal the check quoted replaced by its placeholder, what was done)."""
    found: list = []
    for r in reasons:
        for rx, placeholder in _PERSONAL:
            m = rx.search(r)
            if m and len(m.group(2)) >= 2:
                found.append((m.group(2), placeholder))
    if not found:
        return raw, []
    out = dict(raw)
    done: list = []
    for literal, placeholder in sorted(set(found), key=lambda x: -len(x[0])):
        for f in fields:
            before = out.get(f)
            after = _map_text(before, lambda s, a=literal, b=placeholder: s.replace(a, b))
            if after != before:
                out[f] = after
                done.append(f"{f}: {literal[:40]!r} -> {placeholder}")
    return out, done


# ---- 3. by the model, sentence by sentence -------------------------------------------------------

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"rewrites": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["id", "text"]}}},
    "required": ["rewrites"],
}
MAX_UNITS = 12
_QUOTED = re.compile(r"'([^'\n]{1,60})'|\"([^\"\n]{1,60})\"")
_PREFIX = re.compile(r"^(\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)?)")
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z*_`(\"'])")


# reasons that quote nothing but name what to look for: "body_md has a data: URL"
_SCHEME_REASON = re.compile(r"has an? (javascript|vbscript|data): URL")


def quoted_terms(reasons) -> list:
    """The words and phrases the check quoted (names, data), longest first. A single letter
    counts only as a capital ("names 'L'": a QR error-correction level written as a name)."""
    terms = set()
    for r in reasons:
        for m in _QUOTED.finditer(r):
            t = (m.group(1) or m.group(2) or "").strip()
            if (len(t) >= 2 or t.isupper()) and not t.startswith(("http", "#")):
                terms.add(t)
        for m in _SCHEME_REASON.finditer(r):
            terms.add(m.group(1) + ":")
    return sorted(terms, key=lambda t: (-len(t), t))


def _contains(text: str, term: str) -> bool:
    if term.endswith(":"):           # a scheme, as the check reads it (case-insensitive)
        return re.search(rf"(?i)\b{re.escape(term)}(?!\s|$)", text) is not None
    return re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text) is not None


def flagged_units(raw: dict, fields, reasons) -> list:
    """``[{"id", "field", "index", "prefix", "text", "terms"}]``: each sentence (or heading,
    or list item) outside code that holds a term the check quoted. ``index`` is the list
    position for a field that is a list (Instagram's points), else None."""
    terms = quoted_terms(reasons)
    if not terms:
        return []
    units: list = []
    for f in fields:
        value = raw.get(f)
        items = [(None, value)] if isinstance(value, str) else (
            list(enumerate(value)) if isinstance(value, list) else [])
        for index, text in items:
            if not isinstance(text, str):
                continue
            fenced = False
            for line in text.split("\n"):
                if line.strip().startswith(("```", "~~~")):
                    fenced = not fenced
                    continue
                if fenced or not line.strip():
                    continue
                prefix = _PREFIX.match(line).group(1)
                for sentence in _SPLIT.split(line[len(prefix):]):
                    hit = [t for t in terms if _contains(sentence, t)]
                    if hit and len(units) < MAX_UNITS:
                        units.append({"id": len(units) + 1, "field": f, "index": index,
                                      "text": sentence, "terms": hit})
                    prefix = ""
    return units


def rewrite_prompt(units: list, reasons) -> str:
    lines = ["A content check refused a draft for these reasons:"]
    lines += [f"- {r}" for r in list(dict.fromkeys(reasons))[:12]]
    lines.append("")
    lines.append("Rewrite ONLY the numbered sentences below so that none of them breaks a "
                 "rule. Keep each one's meaning and its Markdown (bold, italics, code). Do "
                 "not use the quoted words with a capital letter: choose a different, plain "
                 "word, or write it in lower case when it is not the first word. Name no "
                 "product, tool, company, place or person. Use American spelling. Write no "
                 "example data: use a placeholder in curly braces instead. Answer with JSON: "
                 '{"rewrites": [{"id": <number>, "text": "<the rewritten sentence>"}]}.')
    lines.append("")
    for u in units:
        lines.append(f"{u['id']}. {u['text']}  (quoted: {', '.join(u['terms'][:4])})")
    return "\n".join(lines)


def apply_rewrites(raw: dict, units: list, answer) -> tuple:
    """(raw with each unit's sentence replaced by its rewrite, what was done). A rewrite that
    is not one plain line, is empty, or is far longer than the sentence is ignored."""
    by_id = {u["id"]: u for u in units}
    rewrites = answer.get("rewrites") if isinstance(answer, dict) else None
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in raw.items()}
    done: list = []
    for item in rewrites if isinstance(rewrites, list) else []:
        if not isinstance(item, dict):
            continue
        u = by_id.get(item.get("id"))
        new = item.get("text")
        if u is None or not isinstance(new, str):
            continue
        new = new.strip()
        if not new or "\n" in new or len(new) > 3 * len(u["text"]) + 40 or new == u["text"]:
            continue
        holder = out.get(u["field"])
        if u["index"] is None and isinstance(holder, str) and u["text"] in holder:
            out[u["field"]] = holder.replace(u["text"], new, 1)
        elif isinstance(holder, list) and u["index"] is not None \
                and u["index"] < len(holder) and isinstance(holder[u["index"]], str) \
                and u["text"] in holder[u["index"]]:
            holder[u["index"]] = holder[u["index"]].replace(u["text"], new, 1)
        else:
            continue
        done.append(f"{u['field']}: rewrote a sentence holding "
                    f"{', '.join(repr(t) for t in u['terms'][:3])}")
    return out, done
