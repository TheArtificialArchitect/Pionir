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
- NAMES. A customer, a platform, a product that never existed. ``unknown_names`` finds
  the capitalised words that are names rather than English (the shared dictionary in
  words.py) and keeps those nothing recorded - see the names section below.

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
from .words import COMPANY, DUAL, NAME, forms, word_kind


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


def unbacked_claims(text: str, recorded: Iterable[Figure], values: Iterable[float] = ()) -> list:
    """The claims in ``text`` nothing backs. ``values`` are numbers a real source wrote in
    a payload (not as figures, e.g. "topics_left": 13): they back a BARE number only - a
    claim with a unit ("$13", "13 replies") still needs a figure in that unit."""
    recorded, values = list(recorded), list(values)
    return [c for c in claims_in(text) if not any(backs(f, c) for f in recorded)
            and not (c.unit is None and any(same(v, c.value) for v in values))]


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
#
# A name is a capitalised word that is not just English. The first version of this rule
# treated EVERY capitalised word mid-sentence as a name, and the live crew's leaders were
# rejected for "Division", "Report", "Data", "Stale" and "Target" in Title-Case headlines
# ("Contracts Division Report: No New Paid Orders") - a false positive on nearly every
# report, so Moss saw "rejected" where there was a true report. The content check had
# already solved the same problem for blog posts with a dictionary (words.py), and this
# reads the same one:
#
# - an ordinary English word ("Division", "Unconfigured") is not a name;
# - a word the brief itself uses - a worker id, an output kind, what a figure measures, a
#   name a worker recorded, the catalogue's own vocabulary - is not a name;
# - an acronym ("API", "HTTP", "PDFs") is vocabulary, as before;
# - a word the dictionary knows as BOTH ("Target", "Mark", "May", "No") is a word when
#   context says so: it opens a sentence, it sits in a Title-Case run beside an ordinary
#   word ("Revenue Target Missed"), or the report or the brief uses it in lower case.
#   Alone mid-sentence and nowhere in lower case ("paid by Mark") it is a name;
# - anything else - a proper noun ("Kimberly") or a word no dictionary holds ("Etsy") -
#   is a name, anywhere in a sentence, and must have been recorded. Fail closed: the
#   dictionary not knowing a word makes it a name, never a pass.

_TOKEN = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")
# the end of a sentence (or a clause a headline opens after "Revenue: ...")
_BOUNDARY = re.compile(r"[.!?:;…][\"'”’)\]]*\s+$|^\s*$|[\n(\[\"“]\s*$"
                       r"|\s[-–—]\s+$")

ALWAYS_KNOWN = frozenset(words("""
I Ok Okay Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March
April May June July August September October November December
"""))


def _cap(tok: str) -> bool:
    """Written like a name: a capital and a lower-case letter ("Gumroad", "RapidAPI"), or a
    brand with its capital inside ("iPhone", "eBay"). All capitals is an acronym."""
    if tok[0].isupper():
        return any(c.islower() for c in tok)
    return tok[0].isalpha() and any(c.isupper() for c in tok[1:])


def _runs(text: str) -> list:
    """Capitalised tokens grouped into runs joined by single spaces (a Title-Case phrase),
    each as [(token, initial), ...]."""
    text = text or ""
    runs, run, last_end = [], [], None
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if not _cap(tok) and not (len(tok) > 1 and tok.isupper()):
            if run:
                runs.append(run)
            run, last_end = [], None
            continue
        if run and text[last_end:m.start()] != " ":
            runs.append(run)
            run = []
        run.append((tok, _BOUNDARY.search(text[:m.start()]) is not None))
        last_end = m.end()
    if run:
        runs.append(run)
    return runs


def lower_words(texts: Iterable[str]) -> set:
    """The words a text uses in lower case: what vouches for a dual-use word."""
    return {t.lower() for text in texts for t in _TOKEN.findall(text or "")
            if t == t.lower() and any(c.isalpha() for c in t)}


def vocabulary_words(texts: Iterable[str]) -> set:
    """Every word in some trusted text, lower-cased, split on punctuation, so a worker id
    ("posting.instagram"), an output kind ("order.new_paid"), a stream ("card-press") or a
    measure ("failing_products") vouches for the words it is made of."""
    out: set = set()
    for text in texts:
        for t in re.split(r"[^A-Za-z0-9'’-]+", str(text or "")):
            t = t.strip("-'’").lower()
            if t:
                out.add(t)
                out.update(p for p in t.split("-") if p)
    return out


def unknown_names(text: str, known: Iterable[str], vocabulary: Iterable[str] = (), *,
                  also_lower: Iterable[str] = ()) -> list:
    """The capitalised words in ``text`` that are names nothing recorded, in order.

    ``known`` are names (any case) the catalogue or a worker vouches for; ``vocabulary``
    are lower-case words the brief is made of; ``also_lower`` are lower-case words other
    texts of the same report use (they vouch only for a dual-use word)."""
    names = {k.lower() for k in known}
    ok = names | {k.lower() for k in ALWAYS_KNOWN} | set(vocabulary)
    lower = lower_words([text]) | set(also_lower) | set(vocabulary)
    out = []
    # "Acme Corp": a company even when each word is English - only a recorded name vouches
    for m in COMPANY.finditer(text or ""):
        if m.group(0).lower() not in names and not all(
                w.lower() in names for w in m.group(1).split()):
            out.append(m.group(0))
    for run in _runs(text):
        kinds = [word_kind(tok) if _cap(tok) else "acronym" for tok, _i in run]
        anchored = len(run) > 1 and any(k not in (DUAL, NAME) for k in kinds)
        for (tok, initial), kind in zip(run, kinds):
            if not _cap(tok) or kind not in (DUAL, NAME):
                continue
            fs = [f.replace("’", "'").lower() for f in forms(tok)]
            if any(f in ok or all(p in ok for p in f.split("-")) for f in fs):
                continue
            if kind == DUAL and (initial or anchored or any(f in lower for f in fs)):
                continue
            out.append(tok)
    return out


def check_report(texts: Iterable[str], stated: Iterable[Figure], recorded: Iterable[Figure],
                 known: Iterable[str], vocabulary: Iterable[str] = (),
                 values: Iterable[float] = ()) -> list:
    """Every reason this report may not go up, in words; empty means it may.
    ``vocabulary`` is the lower-case words the brief is made of (Brief.vocabulary), and
    ``values`` the bare numbers its real-source payloads hold (Brief.recorded_values)."""
    recorded = list(recorded)
    values = list(values)
    known = set(known)
    vocabulary = set(vocabulary)
    texts = [t for t in texts if t]
    everywhere = lower_words(texts)
    problems = []
    for text in texts:
        for c in unbacked_claims(text, recorded, values):
            problems.append(f"states {c.text!r}, which no worker recorded")
        for n in unknown_names(text, known, vocabulary, also_lower=everywhere):
            problems.append(f"names {n!r}, which nothing recorded")
    for f in unbacked_figures(stated, recorded):
        problems.append(f"lists {f.display()}, which no worker recorded")
    return list(dict.fromkeys(problems))            # each reason once, in order


# ---- health claims ----------------------------------------------------------------
#
# Numbers and names are not the only thing a report can get wrong. On the live crew's
# copy a report passed every check above while saying "posting.blog and posting.devto have
# not succeeded" - both had just run fine. Moss steers from these reports, so a report
# may not call a worker failing that last ran ok, nor call things healthy while a worker
# is failing. Conservative on purpose, because a false alarm here costs a report:
#
# - a claim is read per clause, and only when it has a SUBJECT: a worker named by its id
#   ("posting.blog") or as "the <name> worker/desk", or the division as a whole ("the
#   workers", "all desks", "the division"). "Blog post generation failed" has neither and
#   is not judged;
# - negation-aware: "0 failures", "no products are failing", "has not failed" claim
#   nothing; a failure word tied to a counted thing ("2 drafts failed", "failed to send",
#   "failing products") is about that thing, not a worker;
# - a failure claim is false only against a worker that is clearly HEALTHY (last run ok,
#   fresh, no failures since, not silent); a health claim is false only against one that
#   is clearly UNHEALTHY (last run failed, never succeeded, not wired or configured,
#   stale). Anything in between is not judged. A claim about named workers is judged
#   against those workers; one about the division, against all of them.

HEALTHY, UNHEALTHY, UNCLEAR = "healthy", "unhealthy", "unclear"

_CLAUSE = re.compile(r"[.;!?](?:\s+|$)|,\s*|\n+|\s+(?:but|while|whereas|although|though|"
                     r"however|yet)\s+")
_NEGATORS = frozenset(words("no not 0 zero none without nothing never nor neither n't"))
_AUX = frozenset(words("that which who have has had were was are is been be also all both"))
_FAIL_PHRASE = re.compile(
    r"\b(?:(?:has|have|had)\s+(?:\w+\s+)?not\s+(?:yet\s+|ever\s+|once\s+)?succeeded"
    r"|(?:hasn't|haven't|hadn't)\s+(?:yet\s+|ever\s+|once\s+)?succeeded"
    r"|(?:did|does|do)\s+not\s+succeed|(?:didn't|doesn't|don't)\s+succeed"
    r"|never\s+(?:once\s+|yet\s+)?succeeded|not\s+(?:been\s+)?successful|unsuccessful"
    r"|no\s+success(?:es|ful)?"
    r"|not\s+(?:been\s+)?(?:working|running|functioning)"
    r"|(?:is|are|was|were|has\s+been|have\s+been)\s+(?:down|offline|dead))\b")
_FAIL_WORD = re.compile(r"\b(fail(?:s|ed|ing|ures?)?|stalled|stalling|stuck|broken|erroring|"
                        r"crash(?:ed|es|ing)?)\b")
_OK_PHRASE = re.compile(
    r"\b(?:healthy|succeeded|succeeding|(?:operating|running|functioning|working)\s+normally"
    r"|(?:is|are)\s+(?:ok|okay|fine|up\s+and\s+running|working|running)"
    r"|no\s+(?:issues|problems|failures|errors))\b")
_DIVISION = re.compile(r"\b(?:workers|desks|division|every\s+worker|each\s+worker)\b")
_ALL = re.compile(r"\b(?:all|every|each|both|no\s+(?:issues|problems|failures|errors))\b")


def _aliases(worker_id: str) -> re.Pattern:
    short = worker_id.split(".", 1)[-1].lower()
    names = sorted({re.escape(short), re.escape(singular(short))}, key=len, reverse=True)
    return re.compile(rf"(?<![\w.]){re.escape(worker_id.lower())}(?!\w)"
                      rf"|\b(?:{'|'.join(names)})\s+(?:workers?|desks?)\b")


def _tokens_before(text: str, pos: int, n: int) -> list:
    return re.findall(r"[a-z0-9']+", text[:pos].replace("n't", " n't"))[-n:]


def _negated(clause: str, pos: int) -> bool:
    return bool(set(_tokens_before(clause, pos, 3)) & _NEGATORS)


def _item_bound(clause: str, m: re.Match, items: set) -> bool:
    """A failure word about a counted thing ("2 drafts failed", "failed to send",
    "failing products"), not about a worker."""
    after = re.findall(r"[a-z0-9']+", clause[m.end():])[:1]
    if after and (after[0] in ("the", "a", "an", "to", "its", "their")
                  or singular(after[0]) in items):
        return True
    before = [t for t in _tokens_before(clause, m.start(), 4) if t not in _AUX]
    return bool(before) and singular(before[-1]) in items


def health_claims(texts: Iterable[str], states: dict, items: Iterable[str] = ()) -> list:
    """The report's claims about worker health that the runs table contradicts, in words.

    ``states``: worker_id -> (HEALTHY | UNHEALTHY | UNCLEAR, the brief's words for it).
    ``items``: the nouns the division's figures count (drafts, deliveries...), which a
    failure word may be about instead of a worker."""
    items = {singular(i) for i in items} | {singular(n) for n in RESULT_NOUNS}
    items -= {w.split(".", 1)[-1].lower() for w in states}
    aliases = {w: _aliases(w) for w in states}
    problems: list = []
    for text in texts:
        for clause in _CLAUSE.split(text or ""):
            low = (clause or "").lower()
            named = [w for w, rx in aliases.items() if rx.search(low)]
            if not named and not _DIVISION.search(low):
                continue
            fails = [m.group(0) for m in _FAIL_PHRASE.finditer(low)]
            masked = _FAIL_PHRASE.sub(lambda m: " " * len(m.group(0)), low)
            fails += [m.group(0) for m in _FAIL_WORD.finditer(masked)
                      if not _negated(masked, m.start()) and not _item_bound(masked, m, items)]
            oks = []
            for m in _OK_PHRASE.finditer(masked):
                negated = _negated(masked, m.start()) and not m.group(0).startswith("no ")
                (fails if negated else oks).append(m.group(0))
            who = named or list(states)
            if fails:
                wrong = [w for w in who if states[w][0] == HEALTHY]
                if wrong and (named or len(wrong) == len(states)):
                    problems.append(_health_reason(fails[0], named, wrong, states))
            elif oks and (named or _ALL.search(low)):
                wrong = [w for w in who if states[w][0] == UNHEALTHY]
                if wrong:
                    problems.append(_health_reason(oks[0], named, wrong, states))
    return list(dict.fromkeys(problems))


def _health_reason(said: str, named: list, wrong: list, states: dict) -> str:
    return (f"says {said!r} about {', '.join(named) or 'the workers'}, but the runs show "
            + "; ".join(f"{w}: {states[w][1]}" for w in wrong))
