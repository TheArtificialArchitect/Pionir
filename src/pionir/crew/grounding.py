"""What a line may assert, and what an agent actually holds to back it.

Two things a business crew can confabulate that would hurt most:

- FIGURES. "We made $470", "sent 30 emails", "12 replies". ``figures_in`` finds the
  figures a sentence asserts; ``held_figures`` reads the figures an agent has actually
  been shown by a real source - ``seen`` episodes (a Pionir result, a report) and the
  structured ``detail["figures"]`` those episodes carry. The critic drops a line whose
  figure is not among them.
- NAMES. A customer, a platform, a product that never existed. ``proper_nouns`` finds
  the capitalised names mid-sentence; ``known_entities`` is everything capitalised the
  agent has actually encountered in its own store. The critic drops a name that is in
  neither.

Deterministic and model-free; the same functions serve the critic (conversation.py),
the contradiction thought (thinking.py) and the result recorder (agent.py), so they can
never disagree about what counts as a number or a name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


def words(block: str) -> list:
    """A whitespace-separated word list, written as a block for readability."""
    return block.split()

# A figure: optional currency, the number (1,200 / 470 / 3.5), an optional multiplier,
# an optional percent. Not glued to a word ("1st", "v2") and not half of a clock time.
_FIG = re.compile(
    r"(?<![\w.:/-])(?P<money>[$£€]\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<mult>[kKmM]\b|\s(?:thousand|million|grand)\b)?"
    r"(?P<pct>\s?%|\s?percent\b)?"
    r"(?![\w:])")

_WORDNUM = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
}
_WORDNUM_RX = re.compile(r"\b(" + "|".join(_WORDNUM) + r")\b", re.IGNORECASE)

# what a results figure counts; a number WORD is only a claim when it counts one of these
RESULT_NOUNS = frozenset(words("""
email emails message messages reply replies response responses lead leads prospect prospects
customer customers client clients sale sales order orders signup signups sign-ups subscriber
subscribers user users visitor visitors click clicks view views download downloads post posts
call calls meeting meetings deal deals dollar dollars buck bucks euro euros pound pounds
invoice invoices key keys subscription subscriptions purchase purchases conversion conversions
open opens bounce bounces contact contacts draft drafts listing listings product products
review reviews follower followers install installs request requests payment payments
"""))

# a number that measures time is not a result ("in 2 hours", "3 days ago", "at 9 am")
TIME_UNITS = frozenset(words("""
second seconds sec secs minute minutes min mins hour hours hr hrs day days week weeks
month months year years am pm a.m. p.m. o'clock
"""))

_MULT = {"k": 1e3, "m": 1e6, "thousand": 1e3, "million": 1e6, "grand": 1e3}
_NEXT = re.compile(r"\s*([A-Za-z][A-Za-z'.-]*)")


@dataclass(frozen=True)
class Figure:
    value: float
    money: bool
    noun: str          # the word it counts, if any ("emails"), lower-case
    text: str          # as written


def _value(m: re.Match) -> float:
    v = float(m.group("num").replace(",", ""))
    mult = (m.group("mult") or "").strip().lower()
    return v * _MULT.get(mult, 1.0)


def _next_words(text: str, pos: int, n: int = 2) -> list:
    out = []
    for _ in range(n):
        m = _NEXT.match(text, pos)
        if not m:
            break
        out.append(m.group(1).lower().rstrip("."))
        pos = m.end()
    return out


def _noun_of(words: list) -> str:
    for w in words[:2]:
        if w in RESULT_NOUNS:
            return w
    return words[0] if words else ""


def figures_in(text: str) -> list:
    """The figures a sentence asserts - the ones that must be backed. Clock times,
    durations and bare years are not claims about results and are left out."""
    out = []
    for m in _FIG.finditer(text or ""):
        value = _value(m)
        money = bool(m.group("money"))
        words = _next_words(text, m.end())
        if not money and words and words[0] in TIME_UNITS:
            continue
        if not money and not m.group("pct") and not m.group("mult") \
                and float(value).is_integer() and 1900 <= value <= 2100 \
                and not (words and words[0] in RESULT_NOUNS):
            continue                          # a year
        out.append(Figure(value, money, _noun_of(words), m.group(0).strip()))
    for m in _WORDNUM_RX.finditer(text or ""):
        words = _next_words(text, m.end())
        noun = next((w for w in words[:2] if w in RESULT_NOUNS), "")
        if noun:
            out.append(Figure(float(_WORDNUM[m.group(1).lower()]), False, noun, m.group(0)))
    return out


def values_in(text: str) -> set:
    """Every number written in a piece of text a real source produced - liberal, because
    here the source is trusted and the point is only to know what it said."""
    vals = {_value(m) for m in _FIG.finditer(text or "")}
    for m in _WORDNUM_RX.finditer(text or ""):
        vals.add(float(_WORDNUM[m.group(1).lower()]))
    return vals


def values_in_data(obj, depth: int = 0) -> set:
    """Numeric leaves of a JSON-ish result (a ledger row, a count), and numbers in its
    strings. Booleans are not figures."""
    out: set = set()
    if depth > 6:
        return out
    if isinstance(obj, bool):
        return out
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, str):
        out |= values_in(obj)
    elif isinstance(obj, dict):
        for v in list(obj.values())[:200]:
            out |= values_in_data(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in list(obj)[:200]:
            out |= values_in_data(v, depth + 1)
    return out


def same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def held_figures(mem, sources: tuple = ("seen",), limit: int = 3000) -> set:
    """Figures this agent holds from episodes of the given provenance: the text of each
    episode plus any structured ``detail["figures"]`` it carries."""
    out: set = set()
    for e in mem.recent(limit):
        if e["source"] not in sources:
            continue
        out |= values_in(e["text"])
        for v in (e["detail"] or {}).get("figures") or ():
            try:
                out.add(float(v))
            except (TypeError, ValueError):
                continue                      # a non-number in figures is simply not one
    return out


def holds(values: set, value: float) -> bool:
    return any(same(value, v) for v in values)


# ---- names ------------------------------------------------------------------------

# A proper noun: capitalised and containing a lower-case letter ("Gumroad", "RapidAPI",
# "Etsy"). All-caps tokens ("API", "SEO", "CRM") are vocabulary and pass freely.
_PROPER = r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*"
PROPER_RX = re.compile(rf"\b{_PROPER}\b")
# mid-sentence only, as Hearth did: a sentence-initial capital is grammar, not a name
_MID = re.compile(rf"(?<=[a-z0-9,;:)]\s)({_PROPER})\b")

ALWAYS_KNOWN = frozenset(words("""
I Ok Okay Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March
April May June July August September October November December
"""))


def proper_nouns(text: str) -> list:
    """Capitalised names used mid-sentence."""
    return [m.group(1) for m in _MID.finditer(text or "")]


def entities_in(text: str) -> set:
    return set(PROPER_RX.findall(text or ""))


# Episodes whose words the model wrote for this agent. A name that appears only in the
# agent's own speech, intentions or reflections was never ENCOUNTERED - letting it in
# would let one confabulation that slipped through vouch for itself ever after.
SELF_AUTHORED = frozenset({"said", "intended", "reflection"})


def known_entities(mem, limit: int = 3000) -> set:
    """Every name this agent has actually encountered: in anything it saw, did, was told
    or heard (its own store only), plus structured ``detail["entities"]``."""
    out: set = set()
    for e in mem.recent(limit):
        if e["kind"] in SELF_AUTHORED:
            continue
        out |= entities_in(e["text"])
        for name in (e["detail"] or {}).get("entities") or ():
            if isinstance(name, str) and name.strip():
                out |= entities_in(name) or {name.strip()}
    return out
