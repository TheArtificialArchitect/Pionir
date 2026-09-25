"""What a leader's report may assert, and what the workers actually recorded to back it.

Moss plans from the reports her division leaders send up. A leader distils with a local
model, and a model will happily write "revenue is up to $470" over a brief that never
said so. The two confabulations that would hurt most:

- FIGURES. "We made $470", "sent 30 emails", "12 replies". ``claims_in`` finds the
  figures a sentence asserts and types each one (money -> ``usd_cents``, a percentage,
  a latency, a count OF something). ``unbacked_claims`` keeps those that no figure a
  worker recorded FROM A REAL SOURCE backs - matched on value AND unit, so "$12" can
  never be backed by "12 replies". A structured figure in the report must match a
  recorded one on value, unit and what it measures (``unbacked_figures``).
- NAMES. A customer, a platform, a product that never existed. ``proper_nouns`` finds
  the capitalised names mid-sentence; ``unknown_names`` keeps those nothing recorded.

A figure the MODEL wrote (a worker's drafted post, say) is not a real source: the store
marks such outputs ``derived`` and they are never offered as backing, so one invented
number that slipped through cannot vouch for itself ever after.

Deterministic and model-free. ``values_in_data`` also serves hands.py, which lists the
numbers in a Pionir result, so the two never disagree about what counts as a number.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .figures import Figure


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
subscribers user users visitor visitors visit visits click clicks view views download downloads
post posts call calls meeting meetings deal deals dollar dollars buck bucks euro euros pound
pounds invoice invoices key keys subscription subscriptions purchase purchases conversion
conversions open opens bounce bounces contact contacts draft drafts listing listings product
products review reviews follower followers install installs request requests payment payments
"""))

MONEY_WORDS = frozenset(words("dollar dollars buck bucks usd"))
OTHER_MONEY_WORDS = frozenset(words("euro euros pound pounds"))
MS_WORDS = frozenset(words("ms millisecond milliseconds"))

# a number that measures time is not a result ("in 2 hours", "3 days ago", "at 9 am")
TIME_UNITS = frozenset(words("""
second seconds sec secs minute minutes min mins hour hours hr hrs day days week weeks
month months year years am pm a.m. p.m. o'clock
"""))

_MULT = {"k": 1e3, "m": 1e6, "thousand": 1e3, "million": 1e6, "grand": 1e3}
_NEXT = re.compile(r"\s*([A-Za-z][A-Za-z'.-]*)")

# a claim in a currency the ledger does not keep can never be backed
OTHER_CURRENCY = "other_currency"


@dataclass(frozen=True)
class Claim:
    """A figure a sentence asserts, typed as far as the words allow."""

    value: float          # as written, after any multiplier (dollars, not cents)
    unit: str | None      # a figures.UNITS name, OTHER_CURRENCY, or None for a bare number
    measures: str         # for a count, the singular noun it counts ("reply")
    text: str             # as written
    whole: bool = True    # written without a decimal point (money may then be rounded)

    @property
    def cents(self) -> float:
        return round(self.value * 100)


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


def singular(noun: str) -> str:
    noun = noun.lower().strip()
    if noun.endswith("ies") and len(noun) > 4:
        return noun[:-3] + "y"
    if noun.endswith("s") and not noun.endswith("ss") and len(noun) > 3:
        return noun[:-1]
    return noun


def _typed(money: str, pct: bool, following: list) -> tuple:
    """(unit, measures) for a written figure."""
    if money:
        return ("usd_cents" if money.strip() == "$" else OTHER_CURRENCY), ""
    if pct:
        return "percent", ""
    for w in following[:2]:
        if w in MONEY_WORDS:
            return "usd_cents", ""
        if w in OTHER_MONEY_WORDS:
            return OTHER_CURRENCY, ""
        if w in MS_WORDS:
            return "ms", ""
        if w in RESULT_NOUNS:
            return "count", singular(w)
    return None, ""


def claims_in(text: str) -> list:
    """The figures a sentence asserts - the ones that must be backed. Clock times,
    durations and bare years are not claims about results and are left out."""
    out = []
    text = text or ""
    for m in _FIG.finditer(text):
        value = _value(m)
        money = m.group("money") or ""
        following = _next_words(text, m.end())
        if not money and following and following[0] in TIME_UNITS:
            continue
        if not money and not m.group("pct") and not m.group("mult") \
                and float(value).is_integer() and 1900 <= value <= 2100 \
                and not (following and following[0] in RESULT_NOUNS):
            continue                          # a year
        unit, measures = _typed(money, bool(m.group("pct")), following)
        out.append(Claim(value, unit, measures, m.group(0).strip(),
                         whole="." not in m.group("num") and not m.group("mult")))
    for m in _WORDNUM_RX.finditer(text):
        following = _next_words(text, m.end())
        unit, measures = _typed("", False, following)
        if unit is not None:
            out.append(Claim(float(_WORDNUM[m.group(1).lower()]), unit, measures, m.group(0)))
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


def _measure_words(measures: str) -> set:
    return {singular(w) for w in re.split(r"[_\s-]+", measures or "") if w}


def backs(fig: Figure, claim: Claim) -> bool:
    """Does this recorded figure back this written claim? Value AND unit."""
    if claim.unit is None:
        # a bare number: backed only by a recorded value that reads the same
        return same(fig.shown, claim.value) or same(float(fig.value), claim.value)
    if claim.unit != fig.unit:
        return False                          # "$12" is never backed by "12 replies"
    if claim.unit == "usd_cents":
        if same(float(fig.value), claim.cents):
            return True
        # "$470" for a recorded $469.80: honest rounding to the whole dollar, no further
        return claim.whole and abs(float(fig.value) - claim.cents) < 50
    if not same(float(fig.value), claim.value):
        return False
    if claim.unit == "count":
        return claim.measures in _measure_words(fig.measures)
    return True


def unbacked_claims(text: str, recorded: Iterable[Figure]) -> list:
    recorded = list(recorded)
    return [c for c in claims_in(text) if not any(backs(f, c) for f in recorded)]


def figure_backed(stated: Figure, recorded: Iterable[Figure]) -> bool:
    for f in recorded:
        if f.unit != stated.unit or not same(float(f.value), float(stated.value)):
            continue
        if f.measures.strip().lower() != stated.measures.strip().lower():
            continue
        if stated.stream and stated.stream != f.stream:
            continue
        if stated.window and stated.window != f.window:
            continue
        return True
    return False


def unbacked_figures(stated: Iterable[Figure], recorded: Iterable[Figure]) -> list:
    recorded = list(recorded)
    return [s for s in stated if not figure_backed(s, recorded)]


# ---- names ------------------------------------------------------------------------

# A proper noun: capitalised and containing a lower-case letter ("Gumroad", "RapidAPI",
# "Etsy"). All-caps tokens ("API", "SEO", "CRM") are vocabulary and pass freely.
_PROPER = r"[A-Z][A-Za-z0-9]*[a-z][A-Za-z0-9]*"
# mid-sentence only, as Hearth did: a sentence-initial capital is grammar, not a name
_MID = re.compile(rf"(?<=[a-z0-9,;:)]\s)({_PROPER})\b")

ALWAYS_KNOWN = frozenset(words("""
I Ok Okay Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March
April May June July August September October November December
"""))


def proper_nouns(text: str) -> list:
    """Capitalised names used mid-sentence."""
    return [m.group(1) for m in _MID.finditer(text or "")]


def unknown_names(text: str, known: Iterable[str]) -> list:
    ok = {k.lower() for k in known} | {k.lower() for k in ALWAYS_KNOWN}
    return [n for n in proper_nouns(text) if n.lower() not in ok]


def check_report(texts: Iterable[str], stated: Iterable[Figure], recorded: Iterable[Figure],
                 known: Iterable[str]) -> list:
    """Every reason this report may not go up, in words; empty means it may."""
    recorded = list(recorded)
    known = set(known)
    problems = []
    for text in texts:
        for c in unbacked_claims(text, recorded):
            problems.append(f"states {c.text!r}, which no worker recorded")
        for n in unknown_names(text, known):
            problems.append(f"names {n!r}, which nothing recorded")
    for f in unbacked_figures(stated, recorded):
        problems.append(f"lists {f.display()}, which no worker recorded")
    return problems
